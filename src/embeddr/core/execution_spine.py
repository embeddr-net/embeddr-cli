import asyncio
import logging
import traceback
from datetime import datetime
from typing import Dict, Any, Optional, Callable
from uuid import UUID

from sqlmodel import Session, select, col
from embeddr.db.session import get_engine, get_engine_isolated
from embeddr_core.models.artifact_execution import ArtifactExecution
from embeddr_core.execution import JobContext
from embeddr.core.event_bus import _EVENT_BUS
from embeddr_core.plugin_interface import EmbeddrEvent

logger = logging.getLogger("embeddr.spine")


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

    def is_cancelled(self) -> bool:
        self.session.refresh(self.execution)
        return self.execution.status == "canceled"


class ExecutionSpine:
    """
    Central brain for job scheduling and execution.
    """
    _handlers: Dict[str, Callable] = {}
    _running: bool = False

    # Resource Semaphores (Concurrency Limits)
    # Default limits, should be configurable
    _resources = {
        "cpu": asyncio.Semaphore(4),
        "io": asyncio.Semaphore(16),
        "gpu": asyncio.Semaphore(1),
        "network": asyncio.Semaphore(8)
    }

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
                   trigger: str = "user") -> ArtifactExecution:
        """
        Public API to queue work.
        """
        engine = get_engine()
        with Session(engine) as session:
            job = ArtifactExecution(
                type=job_type,
                plugin_name=plugin_name,
                inputs=inputs,
                resource_class=resource_class,
                priority=priority,
                trigger=trigger,
                status="pending"
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            logger.info(f"Job submitted: {job.id} ({job_type})")

            _EVENT_BUS.publish(EmbeddrEvent(
                event_type="execution.created",
                source="spine",
                payload={
                    "id": str(job.id),
                    "type": job.type,
                    "status": "pending",
                    "plugin_name": job.plugin_name,
                    "created_at": job.created_at.isoformat()
                }
            ))
            return job

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
                    job.finished_at = datetime.utcnow()
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

            # Optimistic locking attempt
            job.status = "running"
            job.started_at = datetime.utcnow()
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
                payload={"id": str(job_id), "status": "running"}
            ))
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
                job.finished_at = datetime.utcnow()
                session.add(job)
                session.commit()
                return

            # Execute
            ctx = DBJobContext(session, job)

            try:
                # Run the handler (support async or sync)
                if asyncio.iscoroutinefunction(handler):
                    outputs = await handler(ctx)
                else:
                    outputs = await asyncio.to_thread(handler, ctx)

                # Success
                job.status = "completed"
                job.outputs = outputs
                job.finished_at = datetime.utcnow()
                job.progress = 100

                _EVENT_BUS.publish(EmbeddrEvent(
                    event_type="execution.completed",
                    source="spine",
                    payload={"id": str(job_id), "status": "completed"}
                ))

            except Exception as e:
                logger.error(f"Job {job_id} failed: {traceback.format_exc()}")
                job.status = "failed"
                job.error = str(e)
                job.finished_at = datetime.utcnow()

                _EVENT_BUS.publish(EmbeddrEvent(
                    event_type="execution.failed",
                    source="spine",
                    payload={"id": str(job_id),
                             "status": "failed", "error": str(e)}
                ))

            session.add(job)
            session.commit()
