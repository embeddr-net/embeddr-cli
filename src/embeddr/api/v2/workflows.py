from typing import List, Optional, Any, Dict
from uuid import UUID
import copy
import os
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlmodel import Session, select
from pydantic import BaseModel

from embeddr.db.session import get_session
from embeddr_core.models.artifact import Artifact
from embeddr_core.models.workflow import WorkflowArtifactMetadata, WorkflowPort, WorkflowImplementation
from embeddr.services.comfy_parser import parse_comfy_graph
from embeddr.services.comfy import AsyncComfyClient
from embeddr.services.executors.native import native_executor
from embeddr.services.template_registry import get_template, list_templates

# Ensure defaults are loaded
import embeddr.defaults.transformations

router = APIRouter()


class WorkflowRunRequest(BaseModel):
    inputs: Dict[str, Any]


@router.get("/templates")
async def list_workflow_templates():
    """List available workflow templates."""
    return list_templates()


@router.get("")
async def list_workflows(
    session: Session = Depends(get_session),
    limit: int = 50,
    offset: int = 0
):
    """
    List workflow artifacts.
    """
    query = select(Artifact).where(Artifact.type_name ==
                                   "workflow").limit(limit).offset(offset)
    artifacts = session.exec(query).all()
    return artifacts


@router.post("")
async def create_workflow(
    payload: Dict[str, Any],
    session: Session = Depends(get_session)
):
    """
    Creates a new Workflow Artifact.
    Supports 'comfy-graph' import or 'core-transform' empty/template.
    """
    name = payload.get("name", "Untitled Workflow")
    description = payload.get("description")
    graph = payload.get("graph")
    template = payload.get("template")

    if graph:
        # Import ComfyUI
        metadata_obj = parse_comfy_graph(graph)
    elif template:
        # Load from defaults
        metadata_obj = get_template(template)
    else:
        # Empty Core Transform
        metadata_obj = get_template("empty")

    # Create Artifact
    artifact = Artifact(
        type_name="workflow",
        metadata_json={
            "name": name,
            "description": description,
            "workflow": metadata_obj.model_dump()
        }
    )

    session.add(artifact)
    session.commit()
    session.refresh(artifact)
    return artifact


@router.post("/{id}/duplicate")
async def duplicate_workflow(
    id: UUID,
    session: Session = Depends(get_session)
):
    """Duplicates an existing workflow."""
    original = session.get(Artifact, id)
    if not original or original.type_name != "workflow":
        raise HTTPException(status_code=404, detail="Workflow not found")

    new_meta = copy.deepcopy(original.metadata_json)
    new_meta["name"] = f"{new_meta.get('name')} (Copy)"

    new_artifact = Artifact(
        type_name="workflow",
        metadata_json=new_meta
    )
    # TODO: Add relation 'variant_of' -> original.id

    session.add(new_artifact)
    session.commit()
    session.refresh(new_artifact)
    return new_artifact


@router.post("/compose")
async def compose_workflows(
    workflow_ids: List[UUID],
    name: str = "Composed Workflow",
    session: Session = Depends(get_session)
):
    """
    Composes multiple workflows into one.
    Naively merges inputs and outputs for now.
    """
    if len(workflow_ids) < 2:
        raise HTTPException(
            status_code=400, detail="Need at least 2 workflows to compose")

    artifacts = []
    for wid in workflow_ids:
        a = session.get(Artifact, wid)
        if not a:
            raise HTTPException(
                status_code=404, detail=f"Workflow {wid} not found")
        artifacts.append(a)

    composed_inputs = {}
    composed_outputs = {}

    for i, a in enumerate(artifacts):
        wf = a.metadata_json.get("workflow", {})
        for k, v in wf.get("inputs", {}).items():
            composed_inputs[f"w{i}_{k}"] = v
        for k, v in wf.get("outputs", {}).items():
            composed_outputs[f"w{i}_{k}"] = v

    meta = WorkflowArtifactMetadata(
        inputs=composed_inputs,
        outputs=composed_outputs,
        implementation=WorkflowImplementation(
            type="composed",
            payload={"steps": [str(uid) for uid in workflow_ids]}
        )
    )

    new_artifact = Artifact(
        type_name="workflow",
        metadata_json={
            "name": name,
            "workflow": meta.model_dump()
        }
    )

    session.add(new_artifact)
    session.commit()
    session.refresh(new_artifact)
    return new_artifact


@router.get("/{id}")
async def get_workflow(id: UUID, session: Session = Depends(get_session)):
    artifact = session.get(Artifact, id)
    # Check type or base type
    if not artifact or artifact.type_name != "workflow":
        raise HTTPException(
            status_code=404, detail="Workflow artifact not found")
    return artifact


@router.put("/{id}")
async def update_workflow(
    id: UUID,
    metadata: WorkflowArtifactMetadata,
    session: Session = Depends(get_session)
):
    """
    Updates the workflow definition (exposure, inputs, etc).
    Does NOT execute it.
    """
    artifact = session.get(Artifact, id)
    if not artifact or artifact.type_name != "workflow":
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Update the workflow spec part of metadata
    current_meta = artifact.metadata_json
    current_meta["workflow"] = metadata.model_dump()

    # Explicitly mark as modified for SQLAlchemy
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(artifact, "metadata_json")

    session.add(artifact)
    session.commit()
    session.refresh(artifact)
    return artifact

    # TODO: Port Disk Sync to V2
    # manager = WorkflowManager(session)
    # manager.save_to_disk(workflow)


@router.delete("/{workflow_id}")
def delete_workflow(workflow_id: UUID, session: Session = Depends(get_session)):
    """Delete a workflow."""
    artifact = session.get(Artifact, workflow_id)
    if not artifact or artifact.type_name != "workflow":
        raise HTTPException(status_code=404, detail="Workflow not found")

    # TODO: Port Disk Sync to V2
    # manager = WorkflowManager(session)
    # manager.delete_from_disk(workflow)

    session.delete(artifact)
    session.commit()
    return {"ok": True}


@router.post("/sync")
def sync_workflows(session: Session = Depends(get_session)):
    """Sync workflows from disk to database."""
    # TODO: Port Disk Sync to V2
    # manager = WorkflowManager(session)
    # manager.sync_from_disk()
    return {"status": "synced (stub)"}


@router.post("/{workflow_id}/run")
async def run_workflow(
    workflow_id: UUID,
    req: WorkflowRunRequest,
    session: Session = Depends(get_session)
):
    """
    Run a workflow. 
    Currently defaults to ComfyUI execution, but eventually this should be plugin-aware.
    """
    artifact = session.get(Artifact, workflow_id)
    if not artifact or artifact.type_name != "workflow":
        raise HTTPException(status_code=404, detail="Workflow not found")

    # 1. Prepare the workflow data (graph)
    # Extract from metadata: metadata -> workflow -> implementation -> payload
    try:
        workflow_meta = artifact.metadata_json.get("workflow", {})

        # Dispatch based on implementation type
        impl_type = workflow_meta.get("implementation", {}).get("type")
        if impl_type == "core-transform":
            # Native Execution (requires outputs dir config or temp)
            from embeddr.core.config import get_settings
            s = get_settings()  # ensure we have settings, though we likely need data dir
            output_dir = "/tmp/embeddr_outputs"  # Fallback
            if hasattr(s, 'data_dir'):
                output_dir = str(s.data_dir / "outputs")
            os.makedirs(output_dir, exist_ok=True)

            # Reconstruct model from dict for type safety if needed, or pass dict
            wm_obj = WorkflowArtifactMetadata(**workflow_meta)
            outputs = await native_executor.execute(wm_obj, req.inputs, output_dir, session=session)

            # Wrap response to match API contract roughly
            return {
                "status": "completed",
                "outputs": outputs,
                "prompt_id": str(UUID(int=0))  # dummy ID for sync execution
            }

        implementation = workflow_meta.get("implementation", {})
        graph = copy.deepcopy(implementation.get("payload", {}))
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Invalid workflow structure: {e}")

    if not graph:
        # For core-transform, graph might be empty but operation is in payload.
        # But for ComfyUI, empty graph is an error.
        if impl_type != "core-transform":
            raise HTTPException(
                status_code=400, detail="Workflow payload is empty")

    client = AsyncComfyClient()

    if not await client.is_available():
        raise HTTPException(
            status_code=503, detail="ComfyUI is not available")

    # 2. Patch the graph with inputs
    for node_id, node_inputs in req.inputs.items():
        if node_id in graph:
            if "inputs" not in graph[node_id]:
                graph[node_id]["inputs"] = {}
            for k, v in node_inputs.items():
                graph[node_id]["inputs"][k] = v

    # Fix defaults for Embeddr nodes
    for node_id, node in graph.items():
        class_type = node.get("class_type") or node.get("type")
        if class_type == "embeddr.SaveToFolder":
            node_inputs = node.get("inputs", {})
            if "library" not in node_inputs or not node_inputs["library"]:
                node_inputs["library"] = "Default"
            if "collection" not in node_inputs or not node_inputs["collection"]:
                node_inputs["collection"] = "None"
            if "caption" not in node_inputs:
                node_inputs["caption"] = ""
            node["inputs"] = node_inputs

    # 3. Send to ComfyUI
    try:
        prompt_id = await client.queue_prompt(graph)
        return {"prompt_id": prompt_id, "status": "queued"}

    finally:
        await client.close()
