from typing import List, Optional, Dict, Any
from uuid import UUID
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlmodel import Session, select, desc
from embeddr.db.session import get_session
from embeddr_core.models.artifact_execution import ArtifactExecution
from embeddr.core.plugin_loader import get_plugin_instance, get_loaded_plugins
from embeddr.core.execution_spine import ExecutionSpine
from pydantic import BaseModel
import logging

logger = logging.getLogger("embeddr.api.executions")

router = APIRouter()


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
    session: Session = Depends(get_session)
):
    """
    Creates and schedules an execution using the Spine.
    """
    # Verify plugin exists (optional, could be implicit)
    plugin = get_plugin_instance(req.plugin_name)
    if not plugin:
        raise HTTPException(
            status_code=404, detail=f"Plugin {req.plugin_name} not found or not loaded")

    # Use ExecutionSpine to submit
    # Note: We don't use background_tasks anymore, strictly DB queue
    execution = ExecutionSpine.submit_job(
        job_type=req.job_type,
        inputs=req.inputs,
        plugin_name=req.plugin_name,
        # TODO: infer resource_class from Plugin Action definition
        resource_class="cpu",
        priority=0
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

    query = query.limit(limit).offset(offset)
    return session.exec(query).all()


@router.get("/{execution_id}", response_model=ArtifactExecution)
def get_execution(execution_id: UUID, session: Session = Depends(get_session)):
    ex = session.get(ArtifactExecution, execution_id)
    if not ex:
        raise HTTPException(404, "Execution not found")
    return ex
