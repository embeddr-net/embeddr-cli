import asyncio
import logging
import threading
import traceback
from datetime import UTC, datetime
from typing import Dict, Any, Optional, Callable, List
from uuid import UUID

from sqlmodel import Session, select, col
from embeddr.db.session import get_engine, get_engine_isolated
from embeddr_core.models.artifact_execution import ArtifactExecution
from embeddr_core.models.artifact_execution_event import ArtifactExecutionEvent
from embeddr_core.models.execution_artifact_link import ExecutionArtifactLink
from embeddr_core.execution import JobContext
from embeddr.core.event_bus import _EVENT_BUS
from embeddr_core.plugin_interface import EmbeddrEvent

logger = logging.getLogger("embeddr.spine")


# ---------------------------------------------------------------------------
# Reactive Job Completion Notifier
# ---------------------------------------------------------------------------
# Instead of polling the DB every 0.5-1s to check if a job finished, callers
# register a future/event here. The notifier subscribes to the event bus and
# resolves them instantly when the spine publishes completion events.
# ---------------------------------------------------------------------------

_TERMINAL_STATUSES = frozenset({"completed", "failed", "canceled", "waiting"})


class _JobCompletionNotifier:
    """Reactive bridge: event-bus → per-job futures / threading.Events."""

    def __init__(self) -> None:
        # Async callers (wait_for_job_async)
        self._async_waiters: Dict[UUID, asyncio.Future] = {}
        # Sync callers (wait_for_job)
        self._sync_waiters: Dict[UUID, threading.Event] = {}
        self._sync_results: Dict[UUID, str] = {}  # job_id → terminal status
        self._lock = threading.Lock()
        self._subscribed = False

    def activate(self) -> None:
        """Subscribe to event bus. Must be called once at startup."""
        if self._subscribed:
            return
        self._subscribed = True
        for evt in ("execution.completed", "execution.failed",
                    "execution.canceled", "execution.waiting"):
            _EVENT_BUS.subscribe(evt, self._on_event)

    def _on_event(self, event: EmbeddrEvent) -> None:
        payload = event.payload or {}
        raw_id = payload.get("id")
        status = payload.get("status")
        if not raw_id or not status:
            return
        try:
            job_id = UUID(str(raw_id))
        except (ValueError, TypeError):
            return

        # --- resolve async futures ---
        future = self._async_waiters.pop(job_id, None)
        if future and not future.done():
            # We must use call_soon_threadsafe because publish() may run
            # from any thread (e.g. sync handler in to_thread).
            try:
                loop = future.get_loop()
                loop.call_soon_threadsafe(future.set_result, status)
            except RuntimeError:
                # Loop closed — best-effort
                pass

        # --- resolve sync threading.Events ---
        with self._lock:
            if job_id in self._sync_waiters:
                self._sync_results[job_id] = status
                self._sync_waiters[job_id].set()

    # -- Public API ---------------------------------------------------------

    def register_async(self, job_id: UUID, loop: asyncio.AbstractEventLoop) -> asyncio.Future:
        """Return an asyncio.Future that resolves when the job reaches a terminal state."""
        fut: asyncio.Future = loop.create_future()
        self._async_waiters[job_id] = fut
        return fut

    def unregister_async(self, job_id: UUID) -> None:
        self._async_waiters.pop(job_id, None)

    def register_sync(self, job_id: UUID) -> threading.Event:
        """Return a threading.Event that will be set when the job finishes."""
        evt = threading.Event()
        with self._lock:
            self._sync_waiters[job_id] = evt
        return evt

    def pop_sync_result(self, job_id: UUID) -> Optional[str]:
        with self._lock:
            self._sync_waiters.pop(job_id, None)
            return self._sync_results.pop(job_id, None)


_notifier = _JobCompletionNotifier()


class JobDeferred(Exception):
    """Signal a job should pause and wait for external resumption."""

    def __init__(self, message: str = "waiting", payload: Optional[Dict[str, Any]] = None):
        super().__init__(message)
        self.message = message
        self.payload = payload or {}


def _redact_inputs(value: Dict[str, Any] | None) -> Dict[str, Any]:
    redacted: Dict[str, Any] = {}
    for k, v in (value or {}).items():
        if str(k).lower() in {"api_key", "apikey", "apiKey", "token", "authorization"}:
            redacted[k] = "***"
        else:
            redacted[k] = v
    return redacted


class DBJobContext:
    """Concrete implementation of JobContext backed by the DB."""

    def __init__(self, session: Session, execution: ArtifactExecution):
        self.session = session
        self.execution = execution
        self._execution_id = execution.id
        self._inputs = execution.inputs

    @property
    def execution_id(self) -> UUID:
        return self._execution_id

    @property
    def inputs(self) -> Dict[str, Any]:
        return self._inputs

    def set_progress(self, percent: int, message: Optional[str] = None) -> None:
        # Refresh to check cancellation handling if needed or minimize conflicts
        try:
            val = min(100, max(0, percent))
            self.execution.progress = val
            if message:
                self.execution.message = message
            self.session.add(self.execution)
            self._record_event(
                event_type="execution.updated",
                message=message or "",
                payload={"progress": val},
            )
            self.session.commit()

            # Broadcast update
            _EVENT_BUS.publish(EmbeddrEvent(
                event_type="execution.updated",
                source="spine",
                payload={
                    "id": str(self.execution_id),
                    "status": self.execution.status,
                    "progress": val,
                    "message": self.execution.message
                }
            ))
        except Exception as e:
            logger.error(
                f"Failed to update progress for {self.execution_id}: {e}")

    def log(self, message: str, level: str = "info") -> None:
        logger.info(f"[JOB {self.execution_id}] {message}")
        try:
            self._record_event(
                event_type="execution.log",
                level=level,
                message=message,
            )
            self.session.commit()
        except Exception:
            logger.debug("Failed to record execution log", exc_info=True)

    def defer(self, message: str = "waiting", payload: Optional[Dict[str, Any]] = None) -> None:
        """Mark job as waiting and yield control for external resume."""
        raise JobDeferred(message=message, payload=payload)

    def submit_child(
        self,
        *,
        job_type: str,
        inputs: Dict[str, Any],
        plugin_name: str,
        resource_class: str = "cpu",
        priority: int = 0,
        trigger: str = "runtime",
        primary_artifact_id: Optional[UUID] = None,
    ) -> ArtifactExecution:
        """Submit a child execution linked to this job."""
        return ExecutionSpine.submit_job(
            job_type=job_type,
            inputs=inputs,
            plugin_name=plugin_name,
            resource_class=resource_class,
            priority=priority,
            parent_execution_id=self.execution_id,
            trigger=trigger,
            primary_artifact_id=primary_artifact_id,
            operator_id=self.execution.operator_id,
            api_key_id=self.execution.api_key_id,
            tags=dict(self.execution.tags or {}),
            session=self.session,
        )

    def is_cancelled(self) -> bool:
        self.session.refresh(self.execution)
        return self.execution.status == "canceled"

    def link_artifact(self, artifact_id: UUID, action: str = "created", detail: Optional[str] = None) -> None:
        """
        Record that this execution touched an artifact.

        Actions: "created", "modified", "deleted", "read", "input"
        """
        link = ExecutionArtifactLink(
            execution_id=self._execution_id,
            artifact_id=artifact_id,
            action=action,
            detail=detail,
        )
        self.session.add(link)
        try:
            self.session.flush()
            self._record_event(
                event_type="artifact.linked",
                message=f"{action}: {artifact_id}",
                payload={"artifact_id": str(
                    artifact_id), "action": action, "detail": detail},
            )
        except Exception as e:
            logger.debug(
                f"Failed to link artifact {artifact_id} to execution {self._execution_id}: {e}")

    def link_artifacts(self, artifact_ids: List[UUID], action: str = "created", detail: Optional[str] = None) -> None:
        """Bulk link multiple artifacts to this execution."""
        for aid in artifact_ids:
            self.link_artifact(aid, action=action, detail=detail)

    def _record_event(
        self,
        *,
        event_type: str,
        message: str = "",
        level: str = "info",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        event = ArtifactExecutionEvent(
            execution_id=self.execution_id,
            event_type=event_type,
            level=level,
            message=message,
            payload=payload,
        )
        self.session.add(event)


class ExecutionSpine:
    """
    Central brain for job scheduling and execution.
    """
    _handlers: Dict[str, Callable] = {}
    _running: bool = False
    _pipelines: Dict[str, List[Dict[str, Any]]] = {}

    # Signaled by submit_job() so the worker loop wakes immediately
    # instead of sleeping for the poll interval.
    _job_submitted: asyncio.Event = asyncio.Event()

    # Resource Semaphores (Concurrency Limits)
    # Default limits, should be configurable
    _resources = {
        "cpu": asyncio.Semaphore(4),
        "io": asyncio.Semaphore(16),
        "gpu": asyncio.Semaphore(1),
        "network": asyncio.Semaphore(8)
    }

    @classmethod
    def register_pipeline_step(cls,
                               pipeline_name: str,
                               job_type: str,
                               priority: int = 0,
                               plugin_name: str = "core",
                               resource_class: str = "cpu",
                               inputs_transformer: Optional[Callable] = None):
        """
        Register a step for a specific pipeline (e.g. 'artifact.ingest').
        Plugins can use this to hook into workflows.
        """
        if pipeline_name not in cls._pipelines:
            cls._pipelines[pipeline_name] = []

        cls._pipelines[pipeline_name].append({
            "job_type": job_type,
            "priority": priority,
            "plugin_name": plugin_name,
            "resource_class": resource_class,
            "inputs_transformer": inputs_transformer
        })
        # Sort by priority desc
        cls._pipelines[pipeline_name].sort(
            key=lambda x: x["priority"], reverse=True)
        logger.debug(
            f"Registered pipeline step '{job_type}' for '{pipeline_name}' (P{priority})")

    @classmethod
    def get_pipeline_steps(cls, pipeline_name: str) -> List[Dict[str, Any]]:
        return cls._pipelines.get(pipeline_name, [])

    @classmethod
    def run_subtask_sync(cls,
                         context: JobContext,
                         job_type: str,
                         inputs: Dict[str, Any],
                         plugin_name: str = "core",
                         resource_class: str = "cpu",
                         priority: int = 0,
                         trigger: str = "subtask",
                         primary_artifact_id: Optional[UUID] = None):
        """
        Helper for plugins to run a single subtask and wait for it, 
        without handling raw DB sessions or polling loops.
        """
        # We need to map JobContext back to a concrete session...
        # But wait, JobContext might not expose the session if it's the interface.
        # DBJobContext does.

        # If we can't get the session easily, we use a new one or try to infer.
        # But wait, submitting a job creates a new transaction if we don't pass session.
        # We want to use the session if possible to prevent locks if inside a transaction.

        session = getattr(context, "session", None)
        execution_id = context.execution_id

        job = cls.submit_job(
            job_type=job_type,
            inputs=inputs,
            plugin_name=plugin_name,
            resource_class=resource_class,
            priority=priority,
            parent_execution_id=execution_id,
            trigger=trigger,
            primary_artifact_id=primary_artifact_id,
            session=session
        )

        return cls.wait_for_job(job.id)

    @staticmethod
    def wait_for_job(job_id: UUID, timeout: float = 300.0, check_interval: float = 0.5) -> ArtifactExecution:
        """
        Blocks until the job is completed/failed/canceled.
        Uses the reactive notifier — no DB polling loop.
        """
        _notifier.activate()
        engine = get_engine_isolated()

        # Register FIRST, then check — eliminates race where event fires
        # between the DB read and registration.
        event = _notifier.register_sync(job_id)

        # Check if already terminal
        with Session(engine) as session:
            job = session.get(ArtifactExecution, job_id)
            if job and job.status in _TERMINAL_STATUSES:
                _notifier.pop_sync_result(job_id)
                return job

        signaled = event.wait(timeout=timeout)
        _notifier.pop_sync_result(job_id)

        if not signaled:
            raise TimeoutError(f"Job {job_id} timed out after {timeout}s")

        # Re-fetch from DB to get the full object
        with Session(engine) as session:
            job = session.get(ArtifactExecution, job_id)
            if not job:
                raise RuntimeError(
                    f"Job {job_id} not found after completion signal")
            return job

    @staticmethod
    async def wait_for_job_async(job_id: UUID, timeout: float = 300.0, check_interval: float = 0.5) -> ArtifactExecution:
        """
        Async version which waits until the job is completed/failed/canceled/waiting.
        Uses the reactive notifier — no DB polling loop.
        """
        _notifier.activate()
        engine = get_engine_isolated()

        # Register the future FIRST, then check if already terminal.
        # This ordering eliminates the race where the event fires between
        # the DB check and future registration.
        loop = asyncio.get_running_loop()
        future = _notifier.register_async(job_id, loop)

        # Check if already terminal (job may have finished before we registered)
        def _check_initial():
            with Session(engine) as session:
                return session.get(ArtifactExecution, job_id)

        job = await asyncio.to_thread(_check_initial)
        if job and job.status in _TERMINAL_STATUSES:
            _notifier.unregister_async(job_id)
            return job

        # Await the event bus signal
        try:
            await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            _notifier.unregister_async(job_id)
            raise TimeoutError(f"Job {job_id} timed out after {timeout}s")

        # Re-fetch from DB to get the full detached object
        def _fetch():
            with Session(engine) as session:
                return session.get(ArtifactExecution, job_id)

        job = await asyncio.to_thread(_fetch)
        if not job:
            raise RuntimeError(
                f"Job {job_id} not found after completion signal")
        return job

    @classmethod
    def register_handler(cls, job_type: str, handler: Callable):
        """Register a python callable to handle a specific job type."""
        cls._handlers[job_type] = handler
        logger.debug(f"Registered job handler for '{job_type}'")

    @classmethod
    def submit_job(cls,
                   job_type: str,
                   inputs: Dict[str, Any],
                   plugin_name: str = "core",
                   resource_class: str = "cpu",
                   priority: int = 0,
                   parent_execution_id: Optional[UUID] = None,
                   trigger: str = "user",
                   primary_artifact_id: Optional[UUID] = None,
                   operator_id: Optional[UUID] = None,
                   api_key_id: Optional[str] = None,
                   tags: Optional[Dict[str, str]] = None,
                   session: Optional[Session] = None) -> ArtifactExecution:
        """
        Public API to queue work.
        """
        if session:
            # Use existing session
            return cls._create_job_in_session(
                session, job_type, inputs, plugin_name, resource_class,
                priority, parent_execution_id, trigger, primary_artifact_id,
                operator_id, api_key_id, tags,
            )
        else:
            # Create new session
            engine = get_engine()
            with Session(engine) as session:
                return cls._create_job_in_session(
                    session, job_type, inputs, plugin_name, resource_class,
                    priority, parent_execution_id, trigger, primary_artifact_id,
                    operator_id, api_key_id, tags,
                )

    @classmethod
    def resume_job(
        cls,
        job_id: UUID,
        message: str = "resumed",
        inputs_update: Optional[Dict[str, Any]] = None,
    ) -> Optional[ArtifactExecution]:
        """Move a waiting job back to pending for re-dispatch."""
        engine = get_engine()
        with Session(engine) as session:
            job = session.get(ArtifactExecution, job_id)
            if not job:
                return None
            if inputs_update:
                merged_inputs = dict(job.inputs or {})
                merged_inputs.update(inputs_update)
                job.inputs = merged_inputs

            job.status = "pending"
            job.message = message
            session.add(job)
            session.commit()
            session.refresh(job)

            _EVENT_BUS.publish(EmbeddrEvent(
                event_type="execution.resumed",
                source="spine",
                payload={
                    "id": str(job.id),
                    "status": job.status,
                    "message": job.message,
                    "parent_execution_id": str(job.parent_execution_id) if job.parent_execution_id else None,
                }
            ))
            session.add(ArtifactExecutionEvent(
                execution_id=job.id,
                event_type="execution.resumed",
                message=job.message,
                payload={
                    "parent_execution_id": str(job.parent_execution_id) if job.parent_execution_id else None,
                },
            ))
            session.commit()

            # Wake the worker loop to pick up the re-queued job
            cls._job_submitted.set()

            return job

    @classmethod
    def cancel_job(cls, job_id: UUID) -> Optional[ArtifactExecution]:
        """Cancel a pending or running execution. Returns None if not found or not cancelable."""
        engine = get_engine()
        with Session(engine) as session:
            job = session.get(ArtifactExecution, job_id)
            if not job:
                return None
            if job.status not in ("pending", "running", "waiting"):
                return None

            job.status = "canceled"
            job.finished_at = datetime.now(UTC)
            job.message = "Canceled by user"
            session.add(job)

            event = ArtifactExecutionEvent(
                execution_id=job.id,
                event_type="execution.canceled",
                message="Canceled by user",
            )
            session.add(event)
            session.commit()
            session.refresh(job)

            _EVENT_BUS.publish(EmbeddrEvent(
                event_type="execution.canceled",
                source="spine",
                payload={
                    "id": str(job.id),
                    "type": job.type,
                    "status": "canceled",
                    "plugin_name": job.plugin_name,
                    "parent_execution_id": str(job.parent_execution_id) if job.parent_execution_id else None,
                }
            ))

            return job

    @staticmethod
    def _create_job_in_session(
        session: Session,
        job_type: str,
        inputs: Dict[str, Any],
        plugin_name: str,
        resource_class: str,
        priority: int,
        parent_execution_id: Optional[UUID],
        trigger: str,
        primary_artifact_id: Optional[UUID],
        operator_id: Optional[UUID],
        api_key_id: Optional[str],
        tags: Optional[Dict[str, str]],
    ) -> ArtifactExecution:

        job = ArtifactExecution(
            type=job_type,
            plugin_name=plugin_name,
            inputs=inputs,
            resource_class=resource_class,
            priority=priority,
            parent_execution_id=parent_execution_id,
            trigger=trigger,
            primary_artifact_id=primary_artifact_id,
            status="pending",
            operator_id=operator_id,
            api_key_id=api_key_id,
            tags=dict(tags or {}),
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        logger.info(
            "Job submitted: id=%s type=%s plugin=%s resource=%s priority=%s trigger=%s parent=%s inputs=%s",
            job.id,
            job.type,
            job.plugin_name,
            job.resource_class,
            job.priority,
            job.trigger,
            str(job.parent_execution_id) if job.parent_execution_id else None,
            _redact_inputs(job.inputs),
        )

        # Events need their own session usually if we want them to survive rollback?
        # But here we want atomicity.
        # However, _record_event usually makes a new session to be safe.
        # But let's just use the bus here.

        # We manually emit the event here because we might be in a transaction
        # If we are in a transaction, the job isn't visible to others yet.
        # But the event bus is in-memory.

        # We can't really INSERT the event into DB if the main session is locked/busy?
        # Actually if we reuse the session, we can insert the event too.

        event = ArtifactExecutionEvent(
            execution_id=job.id,
            event_type="execution.created",
            message="queued",
            payload={
                "type": job.type,
                "plugin_name": job.plugin_name,
                "status": job.status,
            },
        )
        session.add(event)
        session.commit()

        _EVENT_BUS.publish(EmbeddrEvent(
            event_type="execution.created",
            source="spine",
            payload={
                "id": str(job.id),
                "type": job.type,
                "status": "pending",
                "plugin_name": job.plugin_name,
                "parent_execution_id": str(job.parent_execution_id) if job.parent_execution_id else None,
                "created_at": job.created_at.isoformat()
            }
        ))

        # Wake the worker loop immediately so it picks up this job
        # without waiting for the next periodic sweep.
        ExecutionSpine._job_submitted.set()

        return job

    @staticmethod
    def _record_event(
        *,
        execution_id: UUID,
        event_type: str,
        message: str = "",
        level: str = "info",
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        try:
            engine = get_engine_isolated()
            with Session(engine) as session:
                event = ArtifactExecutionEvent(
                    execution_id=execution_id,
                    event_type=event_type,
                    level=level,
                    message=message,
                    payload=payload,
                )
                session.add(event)
                session.commit()
        except Exception:
            logger.debug("Failed to record execution event", exc_info=True)

    async def start_worker(self):
        """Starts the main worker loop."""
        self._running = True
        logger.info("Execution Spine Worker Started")

        # Activate the reactive job-completion notifier
        _notifier.activate()

        # Fix orphans from previous crash/restart
        await self._recover_stale_jobs()

        while self._running:
            try:
                await self._process_tick()
            except Exception as e:
                logger.error(f"Error in spine tick: {e}")
                # Don't crash the loop
                await asyncio.sleep(5)

            # Wait for a new job submission signal OR a periodic sweep (10s).
            # This replaces the old 0.5s unconditional sleep — the worker
            # wakes *instantly* when submit_job() is called, and still does
            # background sweeps for any edge-case orphans.
            self._job_submitted.clear()
            try:
                await asyncio.wait_for(self._job_submitted.wait(), timeout=10.0)
            except asyncio.TimeoutError:
                pass  # Periodic sweep — check for orphan jobs

    async def _recover_stale_jobs(self):
        """
        Identifies jobs that were stuck in 'running' state during a restart and marks them as failed.
        This prevents 'ghost' jobs from clogging the queue and UI.
        """
        engine = get_engine_isolated()
        try:
            with Session(engine) as session:
                stale_jobs = session.exec(
                    select(ArtifactExecution).where(
                        ArtifactExecution.status == "running")
                ).all()

                if not stale_jobs:
                    return

                logger.warning(
                    f"⚠️  Found {len(stale_jobs)} stale 'running' jobs from previous session. Marking as failed.")

                for job in stale_jobs:
                    job.status = "failed"
                    job.error = "System restart detected - Job interrupted"
                    job.finished_at = datetime.now(UTC)
                    session.add(job)

                    # Notify UI
                    _EVENT_BUS.publish(EmbeddrEvent(
                        event_type="execution.failed",
                        source="spine",
                        payload={
                            "id": str(job.id),
                            "status": "failed",
                            "error": job.error
                        }
                    ))

                session.commit()
        except Exception as e:
            logger.error(f"Failed to recover stale jobs: {e}")

    async def _process_tick(self):
        """Single iteration of the scheduler."""
        # 1. Check available resources
        # Naive implementation: Check semaphores first?
        # Better: Query DB for pending jobs, then check if we can run them.

        # We process one job per tick per free resource slot to keep it simple
        for res_name, semaphore in self._resources.items():
            if not semaphore.locked():
                # We *might* have capacity. (locked() is not 100% reliable for "full", but good heuristic)
                # Actually, with asyncio.Semaphore, we acquire() to consume.
                # If we want to check without blocking, we check internal counter.
                if semaphore._value > 0:
                    # Try to fetch a job for this resource
                    await self._try_dispatch_job(res_name, semaphore)

    async def _try_dispatch_job(self, resource_class: str, semaphore: asyncio.Semaphore):
        """Attempts to pick a pending job and run it — either via a remote worker or locally."""

        # 1. Fetch pending job logic (Atomicity is tricky here without SELECT FOR UPDATE SKIP LOCKED)
        # SQLite doesn't support SKIP LOCKED.
        # We use a primitive "check and set" strategy.
        engine = get_engine_isolated()
        job_id = None
        job_type = None
        job_inputs = None

        with Session(engine) as session:
            # Get highest priority pending job for this resource
            stmt = (select(ArtifactExecution)
                    .where(ArtifactExecution.status == "pending")
                    .where(ArtifactExecution.resource_class == resource_class)
                    .order_by(col(ArtifactExecution.priority).desc(),
                              col(ArtifactExecution.created_at).asc())
                    .limit(1))

            job = session.exec(stmt).first()
            if not job:
                return

            job_type = job.type
            job_inputs = job.inputs or {}

            # ── Check for a connected worker with this capability ──
            try:
                from embeddr.services.worker_registry import worker_registry
                worker = worker_registry.find_worker_for_capability(job_type)
            except Exception:
                worker = None

            if worker:
                # Dispatch to remote worker instead of running locally
                logger.info(
                    "Dispatching job to worker: id=%s type=%s worker=%s (%s)",
                    job.id, job.type, worker.name, worker.worker_id,
                )
                job.status = "running"
                job.started_at = datetime.now(UTC)
                job.message = f"Dispatched to worker: {worker.name}"
                session.add(job)
                try:
                    session.commit()
                    job_id = job.id
                except Exception:
                    session.rollback()
                    return

                # Fire off worker dispatch (doesn't consume local semaphore)
                asyncio.create_task(
                    self._dispatch_to_worker(
                        job_id, job_type, job_inputs, worker)
                )

                _EVENT_BUS.publish(EmbeddrEvent(
                    event_type="execution.started",
                    source="spine",
                    payload={
                        "id": str(job_id),
                        "status": "running",
                        "worker": worker.name,
                        "parent_execution_id": str(job.parent_execution_id) if job.parent_execution_id else None,
                    }
                ))
                return

            # ── No worker available — run locally ──
            logger.info(
                "Dispatching job: id=%s type=%s plugin=%s resource=%s priority=%s parent=%s",
                job.id,
                job.type,
                job.plugin_name,
                job.resource_class,
                job.priority,
                str(job.parent_execution_id) if job.parent_execution_id else None,
            )

            # Optimistic locking attempt
            job.status = "running"
            job.started_at = datetime.now(UTC)
            session.add(job)
            try:
                session.commit()
                job_id = job.id
            except Exception:
                # Race condition, someone else grabbed it
                session.rollback()
                return

        if job_id:
            # Broadcast start
            _EVENT_BUS.publish(EmbeddrEvent(
                event_type="execution.started",
                source="spine",
                payload={
                    "id": str(job_id),
                    "status": "running",
                    "parent_execution_id": str(job.parent_execution_id) if job.parent_execution_id else None,
                }
            ))
            self._record_event(
                execution_id=job_id,
                event_type="execution.started",
                message="started",
                payload={
                    "status": "running",
                    "parent_execution_id": str(job.parent_execution_id) if job.parent_execution_id else None,
                },
            )
            # 2. Acquire resource and launch task
            await semaphore.acquire()
            asyncio.create_task(self._run_job_wrapper(job_id, resource_class))

    async def _dispatch_to_worker(
        self,
        job_id: UUID,
        job_type: str,
        inputs: Dict[str, Any],
        worker: Any,
    ) -> None:
        """
        Dispatch a job to a remote worker and write the result back to the
        DB execution record when the worker completes.
        """
        from embeddr.services.worker_registry import worker_registry

        engine = get_engine_isolated()
        try:
            # ── Try HTTP dispatch first ──
            # If the worker exposes an HTTP service endpoint for this job type,
            # dispatch via HTTP instead of WebSocket.  This avoids the WS frame
            # size limit which is easily exceeded by base64-encoded images.
            http_result = await self._try_http_dispatch(
                job_id, job_type, inputs, worker, engine)
            if http_result is not None:
                return

            # ── Fallback: WS dispatch ──
            # Hydrate inputs for stateless workers ──
            # Workers don't have DB access, so convert artifact_id references
            # into self-contained payloads with content included.
            worker_inputs = await self._hydrate_worker_inputs(
                job_type, inputs, engine)

            # Submit through the registry (handles WS dispatch + tracking)
            registry_job = await worker_registry.submit_job(
                capability=job_type,
                input_data=worker_inputs,
            )

            # Reactively wait for the worker to complete/fail via the
            # registry's asyncio.Future (resolved by WS messages from
            # the worker, no polling needed).
            timeout = 600  # 10 minute max
            try:
                rj = await worker_registry.wait_for_job(
                    registry_job.job_id, timeout=timeout)
            except asyncio.TimeoutError:
                error_msg = f"Worker job timed out after {timeout}s"
                with Session(engine) as session:
                    job = session.get(ArtifactExecution, job_id)
                    if job:
                        job.status = "failed"
                        job.error = error_msg
                        job.finished_at = datetime.now(UTC)
                        session.add(job)
                        session.commit()

                _EVENT_BUS.publish(EmbeddrEvent(
                    event_type="execution.failed",
                    source="spine",
                    payload={"id": str(job_id), "status": "failed",
                             "error": error_msg},
                ))
                logger.error("Worker dispatch timeout: execution=%s", job_id)
                return

            if rj.status == "complete":
                # Store domain-specific results (e.g. embeddings) to DB
                try:
                    self._store_worker_results(
                        job_type, inputs, rj.output_data, engine)
                except Exception as e:
                    logger.error(
                        "Failed to store worker results for %s: %s",
                        job_id, e,
                    )

                # Write success back to DB execution
                with Session(engine) as session:
                    job = session.get(ArtifactExecution, job_id)
                    if job:
                        job.status = "completed"
                        job.outputs = rj.output_data
                        job.finished_at = datetime.now(UTC)
                        job.progress = 100
                        session.add(job)
                        session.commit()

                _EVENT_BUS.publish(EmbeddrEvent(
                    event_type="execution.completed",
                    source="spine",
                    payload={"id": str(job_id), "status": "completed"},
                ))
                self._record_event(
                    execution_id=job_id,
                    event_type="execution.completed",
                    message=f"Completed by worker: {worker.name}",
                )
                logger.info(
                    "Worker job completed: execution=%s worker=%s",
                    job_id, worker.name,
                )
                return

            if rj.status == "failed":
                error = rj.error or "Worker execution failed"
                with Session(engine) as session:
                    job = session.get(ArtifactExecution, job_id)
                    if job:
                        job.status = "failed"
                        job.error = error
                        job.finished_at = datetime.now(UTC)
                        session.add(job)
                        session.commit()

                _EVENT_BUS.publish(EmbeddrEvent(
                    event_type="execution.failed",
                    source="spine",
                    payload={"id": str(job_id),
                             "status": "failed", "error": error},
                ))
                self._record_event(
                    execution_id=job_id,
                    event_type="execution.failed",
                    level="error",
                    message=error,
                )
                logger.error(
                    "Worker job failed: execution=%s error=%s",
                    job_id, error,
                )
                return

        except Exception as e:
            logger.error("Worker dispatch failed for execution %s: %s",
                         job_id, e, exc_info=True)
            # Mark as failed, fall back won't re-queue (job is already "running")
            try:
                with Session(engine) as session:
                    job = session.get(ArtifactExecution, job_id)
                    if job and job.status == "running":
                        job.status = "failed"
                        job.error = f"Worker dispatch error: {e}"
                        job.finished_at = datetime.now(UTC)
                        session.add(job)
                        session.commit()
            except Exception:
                pass

    # ── Worker input/output helpers ────────────────────────────────────

    async def _try_http_dispatch(
        self,
        job_id: UUID,
        job_type: str,
        inputs: Dict[str, Any],
        worker: Any,
        engine: Any,
    ) -> Optional[Dict[str, Any]]:
        """
        Attempt to dispatch a job via the worker's HTTP service endpoint.

        Returns the output dict on success, or ``None`` if no HTTP endpoint
        is available (caller should fall back to WS dispatch).

        This path avoids the WebSocket frame-size limit by sending the
        hydrated payload over a regular HTTP POST which has no such cap.
        """
        from embeddr.services.worker_registry import worker_registry

        endpoint = worker_registry.find_service_endpoint(job_type)
        if not endpoint:
            return None

        base_url, path = endpoint
        url = f"{base_url.rstrip('/')}{path}"

        # Hydrate inputs (resolve artifact IDs → inline content)
        hydrated = await self._hydrate_worker_inputs(job_type, inputs, engine)

        import httpx

        logger.info(
            "HTTP dispatch: job=%s type=%s url=%s items=%d",
            job_id, job_type, url,
            len(hydrated.get("items", [])),
        )

        try:
            async with httpx.AsyncClient(timeout=300) as client:
                resp = await client.post(
                    url,
                    json={
                        "model_name": hydrated.get("model_name", "Qwen/Qwen3-VL-Embedding-2B"),
                        "items": hydrated.get("items", []),
                    },
                )
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            logger.error(
                "HTTP dispatch failed for %s: %s — falling back to WS",
                job_id, e,
            )
            return None

        # Convert /embed response format → execution output format
        items = data.get("items", [])
        embeddings = [
            {"id": it.get("id"), "vector": it.get("vector")}
            for it in items if it.get("vector") is not None
        ]
        output_data = {
            "embeddings": embeddings,
            "embeddings_count": len(embeddings),
            "model_name": hydrated.get("model_name", "Qwen/Qwen3-VL-Embedding-2B"),
        }

        # Store results to DB (same as WS path)
        try:
            self._store_worker_results(job_type, inputs, output_data, engine)
        except Exception as e:
            logger.error(
                "Failed to store HTTP worker results for %s: %s",
                job_id, e,
            )

        # Mark execution as completed
        with Session(engine) as session:
            job = session.get(ArtifactExecution, job_id)
            if job:
                job.status = "completed"
                job.outputs = output_data
                job.finished_at = datetime.now(UTC)
                job.progress = 100
                session.add(job)
                session.commit()

        _EVENT_BUS.publish(EmbeddrEvent(
            event_type="execution.completed",
            source="spine",
            payload={"id": str(job_id), "status": "completed"},
        ))
        self._record_event(
            execution_id=job_id,
            event_type="execution.completed",
            message=f"Completed by worker (HTTP): {worker.name}",
        )
        logger.info(
            "Worker job completed (HTTP): execution=%s worker=%s embeddings=%d",
            job_id, worker.name, len(embeddings),
        )
        return output_data

    async def _hydrate_worker_inputs(
        self,
        job_type: str,
        inputs: Dict[str, Any],
        engine: Any,
    ) -> Dict[str, Any]:
        """
        Convert DB-dependent inputs into self-contained payloads for
        stateless workers.  Workers have no database access so anything
        like ``artifact_id`` must be resolved to inline content here.
        """
        if job_type != "features.embeddings.generate":
            return inputs

        # Already has pre-built items — nothing to hydrate
        if inputs.get("items"):
            return inputs

        import base64
        from embeddr_core.models.artifact import Artifact, ArtifactPreview
        from embeddr_core.models.artifact_blob import ArtifactBlob
        from embeddr.services.storage import storage_service

        artifact_ids = list(inputs.get("artifact_ids") or [])
        if inputs.get("artifact_id"):
            artifact_ids = [inputs["artifact_id"]] + artifact_ids

        if not artifact_ids:
            logger.warning(
                "Hydrate: no artifact_ids found in inputs keys=%s",
                list(inputs.keys()),
            )
            return inputs

        logger.info(
            "Hydrate: resolving %d artifact(s): %s",
            len(artifact_ids), artifact_ids,
        )
        items: list[Dict[str, Any]] = []

        with Session(engine) as session:
            for art_id_str in artifact_ids:
                try:
                    art_id = UUID(str(art_id_str))
                except (ValueError, TypeError):
                    logger.warning("Hydrate: invalid UUID %r", art_id_str)
                    continue

                artifact = session.get(Artifact, art_id)
                if not artifact:
                    logger.warning(
                        "Hydrate: artifact %s not found in DB", art_id_str)
                    continue

                type_name = (artifact.type_name or "").lower()
                base_type = (artifact.base_type_name or "").lower()
                logger.info(
                    "Hydrate: artifact %s type_name=%s base_type_name=%s uri=%s",
                    art_id_str, type_name, base_type, artifact.uri,
                )

                # Check both type_name and base_type_name — base_type_name
                # is often the generic "artifact" while type_name has the
                # actual media type like "image".
                type_check = f"{type_name} {base_type}"
                if "image" in type_check:
                    item_type = "image"
                elif "video" in type_check:
                    item_type = "video"
                else:
                    logger.warning(
                        "Hydrate: skipping artifact %s — unrecognised type: "
                        "type_name=%s base_type_name=%s",
                        art_id_str, type_name, base_type,
                    )
                    continue

                # Load content bytes (may do remote HTTP fetch, so run in
                # a thread to avoid blocking the event loop).
                content = await asyncio.to_thread(
                    self._load_artifact_bytes,
                    session, artifact, storage_service,
                )
                if not content:
                    logger.warning(
                        "Hydrate: no content for artifact %s", art_id_str)
                    continue

                items.append({
                    "id": str(art_id),
                    "type": item_type,
                    "content_b64": base64.b64encode(content).decode("ascii"),
                })

        hydrated = dict(inputs)
        hydrated["items"] = items
        # Remove raw IDs so the worker doesn't try the DB path
        hydrated.pop("artifact_id", None)
        hydrated.pop("artifact_ids", None)

        logger.info(
            "Hydrated %d artifact(s) into items for worker dispatch",
            len(items),
        )
        return hydrated

    @staticmethod
    def _load_artifact_bytes(
        session: Any, artifact: Any, storage: Any
    ) -> Optional[bytes]:
        """Load artifact binary content from blobs, previews, or storage URI."""
        import os as _os
        from sqlmodel import select as _sel
        from embeddr_core.models.artifact_blob import ArtifactBlob
        from embeddr_core.models.artifact import ArtifactPreview
        from embeddr.services.uri_resolver import resolve_uri

        def _try_read(raw_path: Optional[str]) -> Optional[bytes]:
            """Resolve a URI/path via uri_resolver and read the file."""
            if not raw_path:
                return None
            resolved = resolve_uri(raw_path)
            if resolved and _os.path.exists(resolved):
                try:
                    with open(resolved, "rb") as f:
                        return f.read()
                except Exception:
                    pass
            return None

        # ArtifactBlob stores a file *path*, not inline data
        blob = session.exec(
            _sel(ArtifactBlob).where(
                ArtifactBlob.artifact_id == artifact.id)
        ).first()
        data = _try_read(blob.path if blob else None)
        if data:
            return data

        # ArtifactPreview stores a file *uri*
        preview = session.exec(
            _sel(ArtifactPreview).where(
                ArtifactPreview.artifact_id == artifact.id)
        ).first()
        data = _try_read(preview.uri if preview else None)
        if data:
            return data

        uri = artifact.uri
        data = _try_read(uri)
        if data:
            return data

        # Fetch from remote URL (imported/reference artifacts)
        if uri and (uri.startswith("http://") or uri.startswith("https://")):
            try:
                import requests as _req
                logger.info(
                    "_load_artifact_bytes: fetching remote URL %s for artifact %s",
                    uri[:120], artifact.id,
                )
                # NOTE: This is synchronous I/O.  When called from an async
                # context (e.g. _hydrate_worker_inputs), the caller should
                # wrap in asyncio.to_thread() to avoid blocking the event
                # loop.  See _hydrate_worker_inputs for the wrapped call.
                resp = _req.get(uri, timeout=60, stream=True)
                resp.raise_for_status()
                return resp.content
            except Exception as e:
                logger.warning(
                    "_load_artifact_bytes: remote fetch failed for %s: %s",
                    artifact.id, e,
                )

        return None

    def _store_worker_results(
        self,
        job_type: str,
        original_inputs: Dict[str, Any],
        output_data: Optional[Dict[str, Any]],
        engine: Any,
    ) -> None:
        """
        Write domain-specific worker results back to the DB.

        For embedding jobs, this stores the returned vectors into
        ``ArtifactEmbedding`` rows so they're available for search.
        """
        if job_type != "features.embeddings.generate":
            return
        if not output_data:
            return

        embeddings = output_data.get("embeddings")
        if not embeddings:
            return

        model_name = output_data.get(
            "model_name",
            original_inputs.get("model_name", "unknown"),
        )

        from embeddr_core.models.artifact_embedding import ArtifactEmbedding
        from embeddr_core.services.feature_refs import (
            upsert_feature_ref,
            build_feature_name,
        )

        stored = 0
        with Session(engine) as session:
            for emb in embeddings:
                art_id_str = emb.get("id")
                vector = emb.get("vector")
                if not art_id_str or not vector:
                    continue

                try:
                    art_id = UUID(str(art_id_str))
                except (ValueError, TypeError):
                    continue

                # Upsert embedding row
                from sqlmodel import select as _sel
                existing = session.exec(
                    _sel(ArtifactEmbedding).where(
                        ArtifactEmbedding.artifact_id == art_id,
                        ArtifactEmbedding.model_name == model_name,
                        ArtifactEmbedding.space == "visual",
                    )
                ).first()

                if existing:
                    existing.vector_json = vector
                    existing.vector_dim = len(vector)
                    session.add(existing)
                else:
                    session.add(ArtifactEmbedding(
                        artifact_id=art_id,
                        model_name=model_name,
                        space="visual",
                        plugin_name="embeddr-service-embeddings",
                        vector_json=vector,
                        vector_dim=len(vector),
                    ))

                # Upsert feature ref
                feature_name = build_feature_name(model_name, "visual")
                upsert_feature_ref(
                    session=session,
                    artifact_id=art_id,
                    feature_type="embedding",
                    name=feature_name,
                    storage_kind="inline",
                    storage_ref={"space": "visual"},
                    producer_plugin="embeddr-service-embeddings",
                    producer_version=model_name,
                    model_name=model_name,
                    space="visual",
                    vector_dim=len(vector) if isinstance(
                        vector, list) else None,
                )
                stored += 1

            session.commit()

        logger.info(
            "Stored %d embedding(s) from worker results (model=%s)",
            stored, model_name,
        )

    async def _run_job_wrapper(self, job_id: UUID, resource_class: str):
        """Wrapper to run the job and release the semaphore."""
        try:
            await self._execute_job(job_id)
        finally:
            self._resources[resource_class].release()

    async def _execute_job(self, job_id: UUID):
        """Actual execution logic."""
        engine = get_engine_isolated()

        # We need to re-fetch to get inputs and ensure fresh session
        with Session(engine) as session:
            job = session.get(ArtifactExecution, job_id)
            if not job:
                return

            handler = self._handlers.get(job.type)
            if not handler:
                logger.error(
                    f"No handler for job type '{job.type}' (Job {job_id})")
                job.status = "failed"
                job.error = f"No handler registered for {job.type}"
                job.finished_at = datetime.now(UTC)
                session.add(job)
                session.add(ArtifactExecutionEvent(
                    execution_id=job_id,
                    event_type="execution.failed",
                    level="error",
                    message=job.error,
                ))
                session.commit()
                return

            # Execute
            ctx = DBJobContext(session, job)

            try:
                logger.info(
                    "Job starting: id=%s type=%s plugin=%s inputs=%s",
                    job.id,
                    job.type,
                    job.plugin_name,
                    _redact_inputs(job.inputs),
                )
                # Run the handler (support async or sync)
                if asyncio.iscoroutinefunction(handler):
                    outputs = await handler(ctx)
                else:
                    outputs = await asyncio.to_thread(handler, ctx)

                logger.info(
                    "Job completed: id=%s type=%s plugin=%s outputs=%s",
                    job.id,
                    job.type,
                    job.plugin_name,
                    list(outputs.keys()) if isinstance(
                        outputs, dict) else type(outputs).__name__,
                )

                # Success
                job.status = "completed"
                job.outputs = outputs
                job.finished_at = datetime.now(UTC)
                job.progress = 100
                session.add(job)
                session.commit()

                _EVENT_BUS.publish(EmbeddrEvent(
                    event_type="execution.completed",
                    source="spine",
                    payload={
                        "id": str(job_id),
                        "status": "completed",
                        "parent_execution_id": str(job.parent_execution_id) if job.parent_execution_id else None,
                    }
                ))
                session.add(ArtifactExecutionEvent(
                    execution_id=job_id,
                    event_type="execution.completed",
                    message="completed",
                    payload={
                        "parent_execution_id": str(job.parent_execution_id) if job.parent_execution_id else None,
                    },
                ))

            except JobDeferred as e:
                job.status = "waiting"
                job.message = e.message

                _EVENT_BUS.publish(EmbeddrEvent(
                    event_type="execution.waiting",
                    source="spine",
                    payload={
                        "id": str(job_id),
                        "status": job.status,
                        "message": job.message,
                        "parent_execution_id": str(job.parent_execution_id) if job.parent_execution_id else None,
                        "payload": e.payload,
                    }
                ))
                session.add(ArtifactExecutionEvent(
                    execution_id=job_id,
                    event_type="execution.waiting",
                    message=job.message,
                    payload={
                        "parent_execution_id": str(job.parent_execution_id) if job.parent_execution_id else None,
                        "payload": e.payload,
                    },
                ))
            except Exception as e:
                logger.error(f"Job {job_id} failed: {traceback.format_exc()}")
                job.status = "failed"
                job.error = str(e)
                job.finished_at = datetime.now(UTC)

                _EVENT_BUS.publish(EmbeddrEvent(
                    event_type="execution.failed",
                    source="spine",
                    payload={
                        "id": str(job_id),
                        "status": "failed",
                        "error": str(e),
                        "parent_execution_id": str(job.parent_execution_id) if job.parent_execution_id else None,
                    }
                ))
                session.add(ArtifactExecutionEvent(
                    execution_id=job_id,
                    event_type="execution.failed",
                    level="error",
                    message=str(e),
                    payload={
                        "parent_execution_id": str(job.parent_execution_id) if job.parent_execution_id else None,
                    },
                ))

            session.add(job)
            session.commit()
