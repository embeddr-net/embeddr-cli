"""
Worker API routes — admin endpoints for managing worker nodes and jobs.

These routes are mounted on the main instance to:
- List connected workers and their status
- Submit, list, and cancel jobs
- Drain or disconnect workers
- Handle worker WebSocket connections
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from embeddr.api.security import get_auth_context, require_admin
from embeddr.services.worker_registry import worker_registry
from embeddr.api.v1.system import _load_instance_profile, _get_backend_version
from embeddr_core.models.api_responses import OkResponse

logger = logging.getLogger("embeddr.api.v1.workers")

router = APIRouter()


# ── Pydantic Models ────────────────────────────────────────────────────

class JobSubmitRequest(BaseModel):
    capability: str
    input: dict = {}


class JobSubmitResponse(BaseModel):
    job_id: str
    status: str
    capability: str


class WorkerInfoResponse(BaseModel):
    """Single worker node info."""
    worker_id: str
    name: Optional[str] = None
    capabilities: List[str] = []
    status: str = "connected"
    current_job_id: Optional[str] = None
    connected_at: Optional[str] = None
    last_heartbeat: Optional[str] = None


class WorkerListResponse(BaseModel):
    items: List[Dict[str, Any]]
    total: int


class JobInfoResponse(BaseModel):
    job_id: str
    capability: str
    status: str
    progress: Optional[float] = None
    stage: Optional[str] = None
    assigned_worker_id: Optional[str] = None
    created_at: Optional[str] = None
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class JobListResponse(BaseModel):
    items: List[JobInfoResponse]
    total: int


# ── Worker List / Management ───────────────────────────────────────────

@router.get("", response_model=WorkerListResponse)
def list_workers(auth=Depends(require_admin)):
    """List all connected worker nodes."""
    workers = worker_registry.list_workers()
    return WorkerListResponse(
        items=[w.to_dict() for w in workers],
        total=len(workers),
    )


@router.get("/{worker_id}")
def get_worker(worker_id: str, auth=Depends(require_admin)):
    """Get details for a specific worker."""
    worker = worker_registry.get_worker(worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")
    return worker.to_dict()


@router.post("/{worker_id}/drain", response_model=OkResponse)
def drain_worker(worker_id: str, auth=Depends(require_admin)):
    """Put a worker in drain mode — it will finish current jobs but accept no new ones."""
    if not worker_registry.drain_worker(worker_id):
        raise HTTPException(status_code=404, detail="Worker not found")
    return OkResponse(message=f"Worker {worker_id} entering drain mode")


@router.delete("/{worker_id}")
async def disconnect_worker(worker_id: str, auth=Depends(require_admin)):
    """Force-disconnect a worker."""
    worker = worker_registry.get_worker(worker_id)
    if not worker:
        raise HTTPException(status_code=404, detail="Worker not found")

    # Close the WebSocket connection
    if worker.ws:
        try:
            await worker.ws.close()
        except Exception:
            pass

    worker_registry.unregister_worker(worker_id)
    return OkResponse(message=f"Worker {worker_id} disconnected")


# ── Job Management ─────────────────────────────────────────────────────

@router.get("/jobs", response_model=JobListResponse)
def list_jobs(
    status: Optional[str] = Query(None, description="Filter by status"),
    capability: Optional[str] = Query(
        None, description="Filter by capability"),
    auth=Depends(require_admin),
):
    """List all jobs."""
    jobs = worker_registry.list_jobs(status=status, capability=capability)
    return JobListResponse(
        items=[
            JobInfoResponse(
                job_id=j.job_id,
                capability=j.capability,
                status=j.status,
                progress=j.progress,
                stage=j.stage,
                assigned_worker_id=j.assigned_worker_id,
                created_at=j.created_at,
                error=j.error,
            )
            for j in jobs
        ],
        total=len(jobs),
    )


@router.post("/jobs")
async def submit_job(
    payload: JobSubmitRequest,
    auth=Depends(require_admin),
):
    """Submit a job for dispatch to an available worker."""
    job = await worker_registry.submit_job(
        capability=payload.capability,
        input_data=payload.input,
    )
    return JobSubmitResponse(
        job_id=job.job_id,
        status=job.status,
        capability=job.capability,
    )


@router.get("/jobs/{job_id}", response_model=JobInfoResponse)
def get_job(job_id: str, auth=Depends(require_admin)):
    """Get status of a specific job."""
    job = worker_registry.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return JobInfoResponse(
        job_id=job.job_id,
        capability=job.capability,
        status=job.status,
        progress=job.progress,
        stage=job.stage,
        assigned_worker_id=job.assigned_worker_id,
        created_at=job.created_at,
        output=job.output_data,
        error=job.error,
    )


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, auth=Depends(require_admin)):
    """Cancel a running or queued job."""
    if not await worker_registry.cancel_job(job_id):
        raise HTTPException(
            status_code=400,
            detail="Job not found or already completed/failed",
        )
    return OkResponse(message=f"Job {job_id} cancelled")


# ── Worker WebSocket ───────────────────────────────────────────────────

@router.websocket("/connect")
async def worker_websocket(ws: WebSocket):
    """
    WebSocket endpoint for worker nodes to connect to the main instance.

    Workers authenticate via X-API-Key header on the upgrade request,
    then send registration, heartbeats, and job results.

    The full endpoint path after mounting will be /ws/worker/connect
    or /api/v1/workers/connect depending on how it's mounted.
    """
    # Authenticate the worker connection
    api_key = ws.headers.get("x-api-key", "")
    if not api_key:
        api_key = ws.query_params.get("key", "")

    if not api_key:
        await ws.close(code=4001, reason="Missing API key")
        return

    # Validate the key has worker permissions
    try:
        from embeddr.api.security import check_auth_enabled
        from embeddr.services import auth_service
        from embeddr.db.session import get_engine
        from sqlmodel import Session

        if check_auth_enabled():
            with Session(get_engine()) as session:
                key_obj = auth_service.lookup_api_key(session, api_key)
                if not key_obj:
                    await ws.close(code=4003, reason="Invalid API key")
                    return
                # For now, any active key can be used as a worker key.
                # In the future, we'll check for worker-specific scopes.
                auth_service.touch_api_key(session, key_obj)
    except Exception as e:
        logger.error("Worker auth error: %s", e)
        await ws.close(code=4003, reason="Authentication error")
        return

    await ws.accept()

    worker_id: Optional[str] = None

    try:
        async for message in ws.iter_text():
            try:
                msg = json.loads(message)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from worker")
                continue

            msg_type = msg.get("type", "")
            data = msg.get("data", {})

            if msg_type == "worker:register":
                worker = worker_registry.register_worker(ws, data)
                worker_id = worker.worker_id

                # Build banner payload for the worker
                banner_data: dict = {"worker_id": worker_id}
                try:
                    from embeddr.db.session import get_engine as _get_engine
                    from sqlmodel import Session as _Session
                    with _Session(_get_engine()) as _sess:
                        profile = _load_instance_profile(_sess)
                    banner_data["instance"] = {
                        "name": profile.get("name", "Embeddr"),
                        "description": profile.get("description"),
                        "motd": profile.get("motd"),
                        "logo_url": profile.get("logo_url"),
                        "favicon_url": profile.get("favicon_url"),
                        "banner_text": profile.get("banner_text"),
                        "banner_type": profile.get("banner_type", "info"),
                        "banner_enabled": profile.get("banner_enabled", False),
                        "version": _get_backend_version(),
                    }
                except Exception as e:
                    logger.warning(
                        "Could not load instance banner for worker: %s", e)

                # Send registration confirmation
                await ws.send_text(json.dumps({
                    "type": "worker:registered",
                    "data": banner_data,
                }))

            elif worker_id:
                # Route all other messages through the registry
                await worker_registry.handle_worker_message(
                    worker_id, msg_type, data
                )
            else:
                logger.warning(
                    "Worker message before registration: %s", msg_type
                )

    except WebSocketDisconnect:
        logger.info("Worker %s disconnected", worker_id or "unknown")
    except Exception as e:
        logger.error("Worker WebSocket error: %s", e)
    finally:
        if worker_id:
            worker_registry.unregister_worker(worker_id)
