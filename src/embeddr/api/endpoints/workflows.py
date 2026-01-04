from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from embeddr.db.session import get_session
from embeddr.models.workflow import Workflow
from embeddr.services.comfy import AsyncComfyClient
from pydantic import BaseModel
from datetime import datetime
import copy

from embeddr.services.workflow_manager import WorkflowManager

router = APIRouter()


class WorkflowCreate(BaseModel):
    name: str
    description: Optional[str] = None
    data: Dict[str, Any]
    metadata: Optional[Dict[str, Any]] = {}
    is_active: bool = True


class WorkflowUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    data: Optional[Dict[str, Any]] = None
    metadata: Optional[Dict[str, Any]] = None
    is_active: Optional[bool] = None


class WorkflowRunRequest(BaseModel):
    inputs: Dict[str, Dict[str, Any]]


@router.post("/{workflow_id}/run")
async def run_workflow(
    workflow_id: int, req: WorkflowRunRequest, session: Session = Depends(get_session)
):
    workflow = session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # 1. Prepare the workflow data (graph)
    graph = copy.deepcopy(workflow.data)

    client = AsyncComfyClient()

    if not await client.is_available():
        raise HTTPException(status_code=503, detail="ComfyUI is not available")

    # 2. Patch the graph with inputs
    for node_id, node_inputs in req.inputs.items():
        if node_id in graph:
            if "inputs" not in graph[node_id]:
                graph[node_id]["inputs"] = {}
            for k, v in node_inputs.items():
                graph[node_id]["inputs"][k] = v

    # 3. Send to ComfyUI
    # client is already initialized
    try:
        prompt_id = await client.queue_prompt(graph)
        return {"prompt_id": prompt_id, "status": "queued"}

    finally:
        await client.close()


@router.get("/history/{prompt_id}")
async def get_workflow_history(prompt_id: str):
    client = AsyncComfyClient()
    try:
        if not await client.is_available():
            raise HTTPException(status_code=503, detail="ComfyUI is not available")

        history = await client.get_history(prompt_id)

        if prompt_id not in history:
            return {"status": "pending"}

        # Parse result similar to how run_workflow did
        output_data = history[prompt_id]
        outputs = output_data.get("outputs", {})
        results = []

        for node_id, node_output in outputs.items():
            if "images" in node_output:
                for img in node_output["images"]:
                    results.append(
                        {
                            "node_id": node_id,
                            "filename": img.get("filename"),
                            "subfolder": img.get("subfolder"),
                            "type": img.get("type"),
                        }
                    )

            if "embeddr_ids" in node_output:
                for eid in node_output["embeddr_ids"]:
                    results.append(
                        {"node_id": node_id, "type": "embeddr_id", "value": eid}
                    )

            if "text" in node_output:
                results.append(
                    {"node_id": node_id, "type": "text", "value": node_output["text"]}
                )

        return {"status": "completed", "outputs": results}
    finally:
        await client.close()


@router.post("", response_model=Workflow)
def create_workflow(
    workflow_in: WorkflowCreate, session: Session = Depends(get_session)
):
    workflow = Workflow(
        name=workflow_in.name,
        description=workflow_in.description,
        data=workflow_in.data,
        meta=workflow_in.metadata or {},
        is_active=workflow_in.is_active,
    )
    session.add(workflow)
    session.commit()
    session.refresh(workflow)

    # Save to disk
    manager = WorkflowManager(session)
    manager.save_to_disk(workflow)

    return workflow


@router.post("/sync")
def sync_workflows(session: Session = Depends(get_session)):
    """Sync workflows from disk to database."""
    manager = WorkflowManager(session)
    manager.sync_from_disk()
    return {"status": "synced"}


@router.get("", response_model=List[Workflow])
def list_workflows(
    skip: int = 0, limit: int = 100, session: Session = Depends(get_session)
):
    workflows = session.exec(select(Workflow).offset(skip).limit(limit)).all()
    return workflows


@router.get("/{workflow_id}", response_model=Workflow)
def get_workflow(workflow_id: int, session: Session = Depends(get_session)):
    workflow = session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")
    return workflow


@router.patch("/{workflow_id}", response_model=Workflow)
def update_workflow(
    workflow_id: int,
    workflow_in: WorkflowUpdate,
    session: Session = Depends(get_session),
):
    workflow = session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    workflow_data = workflow_in.model_dump(exclude_unset=True)
    for key, value in workflow_data.items():
        if key == "metadata":
            setattr(workflow, "meta", value)
        else:
            setattr(workflow, key, value)

    workflow.updated_at = datetime.utcnow()
    session.add(workflow)
    session.commit()
    session.refresh(workflow)

    # Save to disk
    manager = WorkflowManager(session)
    manager.save_to_disk(workflow)

    return workflow


@router.delete("/{workflow_id}")
def delete_workflow(workflow_id: int, session: Session = Depends(get_session)):
    workflow = session.get(Workflow, workflow_id)
    if not workflow:
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Delete from disk first (or after?)
    manager = WorkflowManager(session)
    manager.delete_from_disk(workflow)

    session.delete(workflow)
    session.commit()
    return {"ok": True}
