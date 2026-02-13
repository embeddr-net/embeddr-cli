"""
Worker Registry — tracks connected worker nodes on the main instance.

Maintains a live map of connected workers, their capabilities, health metrics,
and provides job dispatch routing based on capability matching.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional
from uuid import uuid4

logger = logging.getLogger("embeddr.services.worker_registry")

# Worker is considered stale if no heartbeat in this many seconds
HEARTBEAT_TIMEOUT = 45


@dataclass
class RegisteredWorker:
    """A worker node that has connected and registered."""
    worker_id: str
    name: str
    tags: list[str]
    capabilities: list[str]
    plugins: list[str]
    max_concurrent_jobs: int
    system_info: dict[str, Any]
    connected_at: float
    last_heartbeat: float
    active_jobs: int = 0
    draining: bool = False
    ws: Any = None  # WebSocket connection reference
    http_url: str = ""  # Worker's HTTP base URL for direct service calls
    services: dict[str, dict[str, str]] = field(
        default_factory=dict)  # capability → {path, method, plugin}

    @property
    def is_stale(self) -> bool:
        return (time.time() - self.last_heartbeat) > HEARTBEAT_TIMEOUT

    @property
    def status(self) -> str:
        if self.draining:
            return "draining"
        if self.is_stale:
            return "stale"
        return "online"

    @property
    def available_capacity(self) -> int:
        return max(0, self.max_concurrent_jobs - self.active_jobs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "name": self.name,
            "tags": self.tags,
            "capabilities": self.capabilities,
            "plugins": self.plugins,
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "system_info": self.system_info,
            "status": self.status,
            "active_jobs": self.active_jobs,
            "draining": self.draining,
            "connected_at": self.connected_at,
            "last_heartbeat": self.last_heartbeat,
        }


@dataclass
class PendingJob:
    """A job waiting to be dispatched to a worker."""
    job_id: str
    capability: str
    input_data: dict[str, Any]
    created_at: float = field(default_factory=time.time)
    assigned_worker_id: Optional[str] = None
    status: str = "queued"  # queued, assigned, running, complete, failed
    progress: float = 0.0
    stage: str = ""
    output_data: Optional[dict[str, Any]] = None
    error: Optional[str] = None


class WorkerRegistry:
    """
    Manages connected worker nodes and job dispatch.

    This is a singleton service that runs on the main embeddr instance.
    Workers connect via WebSocket, register their capabilities, and
    receive job assignments based on capability matching.
    """

    def __init__(self):
        self._workers: dict[str, RegisteredWorker] = {}
        self._jobs: dict[str, PendingJob] = {}
        self._job_callbacks: dict[str, asyncio.Future] = {}

    # ── Worker Management ──────────────────────────────────────────────

    def register_worker(
        self,
        ws: Any,
        data: dict[str, Any],
    ) -> RegisteredWorker:
        """Register a new worker connection."""
        worker_id = str(uuid4())
        now = time.time()

        worker = RegisteredWorker(
            worker_id=worker_id,
            name=data.get("name", "unknown"),
            tags=data.get("tags", []),
            capabilities=data.get("capabilities", []),
            plugins=data.get("plugins", []),
            max_concurrent_jobs=data.get("max_concurrent_jobs", 4),
            system_info=data.get("system", {}),
            connected_at=now,
            last_heartbeat=now,
            ws=ws,
            http_url=data.get("http_url", ""),
            services=data.get("services", {}),
        )

        self._workers[worker_id] = worker
        logger.info(
            "Worker registered: %s (%s) — capabilities: %s",
            worker.name,
            worker_id,
            worker.capabilities,
        )
        return worker

    def unregister_worker(self, worker_id: str) -> None:
        """Remove a worker (e.g., on disconnect)."""
        worker = self._workers.pop(worker_id, None)
        if worker:
            logger.info("Worker unregistered: %s (%s)", worker.name, worker_id)
            # Fail any jobs assigned to this worker
            for job in self._jobs.values():
                if job.assigned_worker_id == worker_id and job.status in ("assigned", "running"):
                    job.status = "failed"
                    job.error = "Worker disconnected"
                    self._resolve_job_callback(
                        job.job_id, error="Worker disconnected")

    def update_heartbeat(self, worker_id: str, data: dict[str, Any]) -> None:
        """Update a worker's heartbeat and metrics."""
        worker = self._workers.get(worker_id)
        if worker:
            worker.last_heartbeat = time.time()
            worker.active_jobs = data.get("active_jobs", worker.active_jobs)

    def drain_worker(self, worker_id: str) -> bool:
        """Put a worker in drain mode (stop sending new jobs)."""
        worker = self._workers.get(worker_id)
        if worker:
            worker.draining = True
            return True
        return False

    def get_worker(self, worker_id: str) -> Optional[RegisteredWorker]:
        return self._workers.get(worker_id)

    def list_workers(self) -> list[RegisteredWorker]:
        return list(self._workers.values())

    def find_worker_for_capability(self, capability: str) -> Optional[RegisteredWorker]:
        """Find the best available worker for a given capability."""
        candidates = [
            w for w in self._workers.values()
            if capability in w.capabilities
            and not w.draining
            and not w.is_stale
            and w.available_capacity > 0
        ]
        if not candidates:
            return None

        # Sort by available capacity (most capacity first), then by active jobs (fewest first)
        candidates.sort(key=lambda w: (-w.available_capacity, w.active_jobs))
        return candidates[0]

    def find_worker_url_for_capability(self, capability: str) -> Optional[str]:
        """Return the HTTP base URL of a worker that can handle this capability, or None."""
        worker = self.find_worker_for_capability(capability)
        if worker and worker.http_url:
            return worker.http_url
        return None

    def find_service_endpoint(
        self, capability: str
    ) -> Optional[tuple[str, str]]:
        """Return ``(base_url, path)`` for a capability's HTTP service endpoint.

        Uses the services manifest advertised by workers so the main instance
        never needs to hardcode endpoint paths.
        Returns ``None`` if no worker with that capability is available.
        """
        worker = self.find_worker_for_capability(capability)
        if not worker or not worker.http_url:
            return None
        svc = worker.services.get(capability)
        if svc:
            return (worker.http_url, svc["path"])
        # Fallback: worker has the capability but no explicit service entry.
        # (Shouldn't happen with well-behaved plugins, but be defensive.)
        return None

    # ── Job Management ─────────────────────────────────────────────────

    async def submit_job(
        self,
        capability: str,
        input_data: dict[str, Any],
    ) -> PendingJob:
        """Submit a job for dispatch to an available worker."""
        job_id = str(uuid4())
        job = PendingJob(
            job_id=job_id,
            capability=capability,
            input_data=input_data,
        )
        self._jobs[job_id] = job

        # Try to dispatch immediately
        worker = self.find_worker_for_capability(capability)
        if worker:
            await self._dispatch_job(job, worker)
        else:
            logger.info(
                "No worker available for capability '%s', job %s queued",
                capability,
                job_id,
            )

        return job

    async def _dispatch_job(self, job: PendingJob, worker: RegisteredWorker) -> None:
        """Send a job to a specific worker."""
        job.assigned_worker_id = worker.worker_id
        job.status = "assigned"
        worker.active_jobs += 1

        msg = {
            "type": "worker:job",
            "data": {
                "job_id": job.job_id,
                "capability": job.capability,
                "input": job.input_data,
            },
        }

        try:
            await worker.ws.send_text(json.dumps(msg))
            logger.info(
                "Dispatched job %s to worker %s (%s)",
                job.job_id,
                worker.name,
                worker.worker_id,
            )
        except Exception as e:
            logger.error("Failed to dispatch job %s: %s", job.job_id, e)
            job.status = "queued"
            job.assigned_worker_id = None
            worker.active_jobs = max(0, worker.active_jobs - 1)

    def handle_job_progress(self, data: dict[str, Any]) -> None:
        """Handle a progress update from a worker."""
        job_id = data.get("job_id")
        job = self._jobs.get(job_id)
        if job:
            job.status = "running"
            job.progress = data.get("progress", job.progress)
            job.stage = data.get("stage", job.stage)

    def handle_job_complete(self, worker_id: str, data: dict[str, Any]) -> None:
        """Handle a job completion from a worker."""
        job_id = data.get("job_id")
        job = self._jobs.get(job_id)
        if job:
            job.status = "complete"
            job.progress = 1.0
            job.output_data = data.get("output")
            self._decrement_worker_jobs(worker_id)
            self._resolve_job_callback(job_id)
            logger.info("Job %s completed", job_id)

    def handle_job_error(self, worker_id: str, data: dict[str, Any]) -> None:
        """Handle a job failure from a worker."""
        job_id = data.get("job_id")
        job = self._jobs.get(job_id)
        if job:
            job.status = "failed"
            job.error = data.get("error", "Unknown error")
            self._decrement_worker_jobs(worker_id)
            self._resolve_job_callback(job_id, error=job.error)
            logger.error("Job %s failed: %s", job_id, job.error)

    def handle_job_reject(self, data: dict[str, Any]) -> None:
        """Handle a worker rejecting a job (at capacity or draining)."""
        job_id = data.get("job_id")
        job = self._jobs.get(job_id)
        if job:
            job.status = "queued"
            old_worker = job.assigned_worker_id
            job.assigned_worker_id = None
            if old_worker:
                self._decrement_worker_jobs(old_worker)
            logger.info(
                "Job %s rejected (reason: %s), re-queuing",
                job_id,
                data.get("reason", "unknown"),
            )

    def get_job(self, job_id: str) -> Optional[PendingJob]:
        return self._jobs.get(job_id)

    async def wait_for_job(self, job_id: str, timeout: float = 600.0) -> PendingJob:
        """
        Reactively wait for a worker job to reach a terminal state.
        Uses an asyncio.Future resolved by handle_job_complete/error.
        """
        job = self._jobs.get(job_id)
        if job and job.status in ("complete", "failed"):
            return job

        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._job_callbacks[job_id] = future

        try:
            await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._job_callbacks.pop(job_id, None)
            raise

        job = self._jobs.get(job_id)
        if not job:
            raise RuntimeError(f"Worker job {job_id} vanished after completion")
        return job

    def list_jobs(
        self,
        status: Optional[str] = None,
        capability: Optional[str] = None,
    ) -> list[PendingJob]:
        jobs = list(self._jobs.values())
        if status:
            jobs = [j for j in jobs if j.status == status]
        if capability:
            jobs = [j for j in jobs if j.capability == capability]
        return jobs

    async def cancel_job(self, job_id: str) -> bool:
        """Cancel a running or queued job."""
        job = self._jobs.get(job_id)
        if not job:
            return False

        if job.status in ("complete", "failed"):
            return False

        if job.assigned_worker_id:
            worker = self._workers.get(job.assigned_worker_id)
            if worker and worker.ws:
                try:
                    await worker.ws.send_text(json.dumps({
                        "type": "worker:cancel",
                        "data": {"job_id": job_id},
                    }))
                except Exception:
                    pass
            self._decrement_worker_jobs(job.assigned_worker_id)

        job.status = "failed"
        job.error = "Cancelled"
        self._resolve_job_callback(job_id, error="Cancelled")
        return True

    # ── Internal helpers ───────────────────────────────────────────────

    def _decrement_worker_jobs(self, worker_id: str) -> None:
        worker = self._workers.get(worker_id)
        if worker:
            worker.active_jobs = max(0, worker.active_jobs - 1)

    def _resolve_job_callback(self, job_id: str, error: Optional[str] = None) -> None:
        future = self._job_callbacks.pop(job_id, None)
        if future and not future.done():
            if error:
                future.set_exception(RuntimeError(error))
            else:
                future.set_result(self._jobs.get(job_id))

    # ── WebSocket Message Router ───────────────────────────────────────

    async def handle_worker_message(
        self,
        worker_id: str,
        msg_type: str,
        data: dict[str, Any],
    ) -> None:
        """Route an incoming worker WebSocket message to the appropriate handler."""
        if msg_type == "worker:heartbeat":
            self.update_heartbeat(worker_id, data)
        elif msg_type == "worker:progress":
            self.handle_job_progress(data)
        elif msg_type == "worker:complete":
            self.handle_job_complete(worker_id, data)
        elif msg_type == "worker:error":
            self.handle_job_error(worker_id, data)
        elif msg_type == "worker:reject":
            self.handle_job_reject(data)
        else:
            logger.debug("Unknown worker message type: %s", msg_type)


# Singleton registry instance
worker_registry = WorkerRegistry()
