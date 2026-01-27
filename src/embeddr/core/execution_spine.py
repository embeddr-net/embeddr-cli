import asyncio
import logging
import traceback
from datetime import UTC, datetime
from typing import Dict, Any, Optional, Callable, List
from uuid import UUID
import time

from sqlmodel import Session, select, col
from embeddr.db.session import get_engine, get_engine_isolated
from embeddr_core.models.artifact_execution import ArtifactExecution
from embeddr_core.models.artifact_execution_event import ArtifactExecutionEvent
from embeddr_core.execution import JobContext
from embeddr.core.event_bus import _EVENT_BUS
from embeddr_core.plugin_interface import EmbeddrEvent

logger = logging.getLogger("embeddr.spine")


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

    def is_cancelled(self) -> bool:
        self.session.refresh(self.execution)
        return self.execution.status == "canceled"

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
        logger.info(
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
        """
        start_time = time.time()
        # Use isolated engine to avoid transaction visibility issues
        engine = get_engine_isolated()

        while True:
            if time.time() - start_time > timeout:
                raise TimeoutError(f"Job {job_id} timed out after {timeout}s")

            with Session(engine) as session:
                job = session.get(ArtifactExecution, job_id)
                if not job:
                    # Should unlikely happen unless deleted
                    time.sleep(check_interval)
                    continue

                if job.status in ["completed", "failed", "canceled"]:
                    return job

            time.sleep(check_interval)

    @staticmethod
    async def wait_for_job_async(job_id: UUID, timeout: float = 300.0, check_interval: float = 0.5) -> ArtifactExecution:
        """
        Async version which waits until the job is completed/failed/canceled.
        """
        start_time = time.time()
        # Use isolated engine to avoid transaction visibility issues
        engine = get_engine_isolated()

        while True:
            if time.time() - start_time > timeout:
                raise TimeoutError(f"Job {job_id} timed out after {timeout}s")

            # Blocking call in thread pool to keep loop free
            def _check():
                with Session(engine) as session:
                    return session.get(ArtifactExecution, job_id)

            job = await asyncio.to_thread(_check)

            if not job:
                await asyncio.sleep(check_interval)
                continue

            if job.status in ["completed", "failed", "canceled"]:
                return job

            await asyncio.sleep(check_interval)

    @classmethod
    def register_handler(cls, job_type: str, handler: Callable):
        """Register a python callable to handle a specific job type."""
        cls._handlers[job_type] = handler
        logger.info(f"Registered job handler for '{job_type}'")

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
                   session: Optional[Session] = None) -> ArtifactExecution:
        """
        Public API to queue work.
        """
        if session:
            # Use existing session
            return cls._create_job_in_session(
                session, job_type, inputs, plugin_name, resource_class,
                priority, parent_execution_id, trigger, primary_artifact_id
            )
        else:
            # Create new session
            engine = get_engine()
            with Session(engine) as session:
                return cls._create_job_in_session(
                    session, job_type, inputs, plugin_name, resource_class,
                    priority, parent_execution_id, trigger, primary_artifact_id
                )

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
        primary_artifact_id: Optional[UUID]
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
            status="pending"
        )
        session.add(job)
        session.commit()
        session.refresh(job)
        logger.warning(
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

        # Fix orphans from previous crash/restart
        await self._recover_stale_jobs()

        while self._running:
            try:
                await self._process_tick()
            except Exception as e:
                logger.error(f"Error in spine tick: {e}")
                # Don't crash the loop
                await asyncio.sleep(5)

            # Polling interval
            await asyncio.sleep(0.5)

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
        """Attempts to pick a pending job and run it."""

        # 1. Fetch pending job logic (Atomicity is tricky here without SELECT FOR UPDATE SKIP LOCKED)
        # SQLite doesn't support SKIP LOCKED.
        # We use a primitive "check and set" strategy.
        engine = get_engine_isolated()
        job_id = None

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

            logger.warning(
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
                logger.warning(
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

                logger.warning(
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
