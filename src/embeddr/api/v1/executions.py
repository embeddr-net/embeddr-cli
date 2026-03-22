from typing import List, Optional, Dict, Any, Literal
from uuid import UUID
from datetime import datetime
import asyncio
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from sqlmodel import Session, select, desc
from embeddr.db.session import get_session, get_engine
from embeddr_core.models.artifact_execution import ArtifactExecution
from embeddr_core.models.artifact_execution_event import ArtifactExecutionEvent
from embeddr_core.models.execution_artifact_link import ExecutionArtifactLink
from embeddr.core.plugin_loader import get_plugin_instance, get_loaded_plugins
from embeddr.core.execution_spine import ExecutionSpine, _notifier
from embeddr.api.security import require_permission_for_request, get_auth_context
from embeddr.auth.permissions import Permissions
from pydantic import BaseModel
import logging

logger = logging.getLogger("embeddr.api.executions")

router = APIRouter(
    dependencies=[Depends(require_permission_for_request(
        Permissions.EXECUTIONS_READ, Permissions.EXECUTIONS_WRITE
    ))],
)


@router.get("/actions")
def list_actions():
    """List all available execution actions from loaded plugins."""
    plugins = get_loaded_plugins()
    actions = []
    for p in plugins:
        p_name = p["name"]
        p_actions = p.get("actions", [])
        for action in p_actions:
            # Clone and enrich with plugin name
            a_copy = action.copy()
            a_copy["plugin_name"] = p_name
            # Map action 'id' or 'name' to execution 'type'
            # Assuming action["id"] is the job type
            a_copy["job_type"] = action.get("id") or action.get("name")
            actions.append(a_copy)
    return actions


@router.get("/plugins")
def list_plugins_debug():
    """List all loaded plugins (Debug helper)."""
    return get_loaded_plugins()


class ExecutionCreate(BaseModel):
    plugin_name: str
    job_type: str        # Renamed from action_name
    inputs: Dict[str, Any]
    primary_artifact_id: Optional[UUID] = None


@router.post("", response_model=ArtifactExecution)
async def create_execution(
    req: ExecutionCreate,
    request: Request,
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    """
    Creates and schedules an execution using the Spine.
    """
    # Verify plugin exists (optional, could be implicit)
    plugin = get_plugin_instance(req.plugin_name)
    if not plugin:
        raise HTTPException(
            status_code=404, detail=f"Plugin {req.plugin_name} not found or not loaded")

    # Extract auth context for execution ownership (enables WS event scoping)
    operator_id = getattr(auth, "operator_id", None) if auth else None
    api_key_id = str(getattr(auth, "api_key_id", None)) if getattr(auth, "api_key_id", None) else None
    client_id = (request.headers.get("x-embeddr-client-id") or "").strip() or None
    tags = {}
    if client_id:
        tags["target_client_id"] = client_id

    # Use ExecutionSpine to submit
    execution = ExecutionSpine.submit_job(
        job_type=req.job_type,
        inputs=req.inputs,
        plugin_name=req.plugin_name,
        resource_class="cpu",
        priority=0,
        operator_id=operator_id,
        api_key_id=api_key_id,
        tags=tags if tags else None,
    )

    # Determine if we need to link primary artifact (Spine submit_job might not support this arg yet,
    # we can update it manually or add to submit_job)
    if req.primary_artifact_id:
        # Re-attach to current session to update
        execution = session.merge(execution)
        execution.primary_artifact_id = req.primary_artifact_id
        session.add(execution)
        session.commit()
        session.refresh(execution)

    return execution


# Legacy/Background Task runner removed - handled by Spine Worker


@router.get("", response_model=List[ArtifactExecution])
def list_executions(
    plugin_name: Optional[str] = None,
    status: Optional[str] = None,
    type: Optional[str] = None,
    created_after: Optional[str] = None,
    created_before: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    session: Session = Depends(get_session)
):
    query = select(ArtifactExecution).order_by(
        desc(ArtifactExecution.created_at))
    if plugin_name:
        query = query.where(ArtifactExecution.plugin_name == plugin_name)
    if status:
        query = query.where(ArtifactExecution.status == status)
    if type:
        query = query.where(ArtifactExecution.type == type)
    if created_after:
        try:
            dt = datetime.fromisoformat(created_after)
            query = query.where(ArtifactExecution.created_at >= dt)
        except Exception:
            raise HTTPException(400, "Invalid created_after datetime")
    if created_before:
        try:
            dt = datetime.fromisoformat(created_before)
            query = query.where(ArtifactExecution.created_at <= dt)
        except Exception:
            raise HTTPException(400, "Invalid created_before datetime")
    if q:
        like = f"%{q}%"
        query = query.where(
            (ArtifactExecution.type.ilike(like))
            | (ArtifactExecution.plugin_name.ilike(like))
            | (ArtifactExecution.message.ilike(like))
        )

    query = query.limit(limit).offset(offset)
    return session.exec(query).all()


@router.get("/{execution_id}", response_model=ArtifactExecution)
def get_execution(execution_id: UUID, session: Session = Depends(get_session)):
    ex = session.get(ArtifactExecution, execution_id)
    if not ex:
        raise HTTPException(404, "Execution not found")
    return ex


@router.get("/{execution_id}/events", response_model=List[ArtifactExecutionEvent])
def list_execution_events(
    execution_id: UUID,
    limit: int = 200,
    offset: int = 0,
    session: Session = Depends(get_session),
):
    query = (select(ArtifactExecutionEvent)
             .where(ArtifactExecutionEvent.execution_id == execution_id)
             .order_by(ArtifactExecutionEvent.created_at.asc())
             .limit(limit)
             .offset(offset))
    return session.exec(query).all()


async def _fetch_execution(execution_id: UUID) -> Optional[ArtifactExecution]:
    def _load() -> Optional[ArtifactExecution]:
        with Session(get_engine()) as session:
            return session.get(ArtifactExecution, execution_id)

    return await asyncio.to_thread(_load)


@router.get("/{execution_id}/wait", response_model=ArtifactExecution)
async def wait_execution(
    execution_id: UUID,
    timeout_s: float = 30.0,
    poll_interval_s: float = 0.5,
):
    """
    Wait until an execution finishes or timeout is reached.
    Uses reactive event-bus notification — no DB polling.
    Returns the latest execution state.
    """
    _notifier.activate()

    # Check if already terminal
    ex = await _fetch_execution(execution_id)
    if not ex:
        raise HTTPException(404, "Execution not found")
    if ex.status in {"completed", "failed", "canceled"}:
        return ex

    # Register and await the event bus signal
    loop = asyncio.get_running_loop()
    future = _notifier.register_async(execution_id, loop)
    try:
        await asyncio.wait_for(future, timeout=max(0.0, timeout_s))
    except asyncio.TimeoutError:
        _notifier.unregister_async(execution_id)
        # Return current state on timeout (same as before)
        ex = await _fetch_execution(execution_id)
        if not ex:
            raise HTTPException(404, "Execution not found")
        return ex

    # Re-fetch to return the full object
    ex = await _fetch_execution(execution_id)
    if not ex:
        raise HTTPException(404, "Execution not found")
    return ex


class ExecutionResume(BaseModel):
    message: Optional[str] = None
    inputs: Optional[Dict[str, Any]] = None


@router.post("/{execution_id}/resume", response_model=ArtifactExecution)
def resume_execution(execution_id: UUID, req: ExecutionResume):
    job = ExecutionSpine.resume_job(
        execution_id,
        message=req.message or "resumed",
        inputs_update=req.inputs,
    )
    if not job:
        raise HTTPException(404, "Execution not found")
    return job


@router.post("/{execution_id}/cancel", response_model=ArtifactExecution)
def cancel_execution(
    execution_id: UUID,
    session: Session = Depends(get_session),
):
    """Cancel a pending or running execution."""
    job = ExecutionSpine.cancel_job(execution_id)
    if not job:
        raise HTTPException(404, "Execution not found or not cancelable")
    return job


class ExecutionNudge(BaseModel):
    """Inject a steering message into a running LLM execution."""
    message: str = ""
    mode: Literal["steer", "goal_replace"] = "steer"
    goal: Optional[str] = None


@router.post("/{execution_id}/nudge")
def nudge_execution(
    execution_id: UUID,
    req: ExecutionNudge,
    session: Session = Depends(get_session),
):
    """Inject a steering/nudge message into a running execution.

    The message is stored as an execution event with type 'llm.nudge'.
    The LLM agent loop drains these between turns and injects them as
    user interjections into the conversation context.
    """
    job = session.get(ArtifactExecution, execution_id)
    if not job:
        raise HTTPException(404, "Execution not found")
    if job.status != "running":
        raise HTTPException(400, f"Execution is {job.status}, not running. Cannot nudge.")

    message_text = str(req.message or "").strip()
    goal_text = str(req.goal or "").strip()
    if req.mode == "goal_replace":
        if not goal_text:
            goal_text = message_text
        if not goal_text:
            raise HTTPException(400, "Goal replacement nudges require a goal.")
    elif not message_text:
        raise HTTPException(400, "Nudge message cannot be empty.")

    payload = {
        "content": message_text,
        "mode": req.mode,
    }
    if goal_text:
        payload["goal"] = goal_text

    event = ArtifactExecutionEvent(
        execution_id=execution_id,
        event_type="llm.nudge",
        message=goal_text or message_text,
        payload=payload,
    )
    session.add(event)
    session.commit()

    from embeddr.core.event_bus import _EVENT_BUS
    from embeddr_core.plugin_interface import EmbeddrEvent
    _EVENT_BUS.publish(EmbeddrEvent(
        event_type="execution.nudge",
        source="api",
        payload={
            "id": str(execution_id),
            "message": message_text,
            "mode": req.mode,
            "goal": goal_text or None,
        },
    ))

    return {
        "ok": True,
        "message": "Nudge queued",
        "execution_id": str(execution_id),
        "mode": req.mode,
        "goal": goal_text or None,
    }


@router.get("/{execution_id}/artifacts", response_model=List[ExecutionArtifactLink])
def list_execution_artifacts(
    execution_id: UUID,
    action: Optional[str] = None,
    session: Session = Depends(get_session),
):
    """List artifacts linked to this execution."""
    query = (
        select(ExecutionArtifactLink)
        .where(ExecutionArtifactLink.execution_id == execution_id)
        .order_by(ExecutionArtifactLink.created_at.asc())
    )
    if action:
        query = query.where(ExecutionArtifactLink.action == action)
    return session.exec(query).all()


@router.get("/{execution_id}/children", response_model=List[ArtifactExecution])
def list_child_executions(
    execution_id: UUID,
    session: Session = Depends(get_session),
):
    """List child executions of a parent execution."""
    query = (
        select(ArtifactExecution)
        .where(ArtifactExecution.parent_execution_id == execution_id)
        .order_by(ArtifactExecution.created_at.asc())
    )
    return session.exec(query).all()
