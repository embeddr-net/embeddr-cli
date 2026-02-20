from typing import List, Optional, Any, Dict
import re
from uuid import UUID
import copy
import os
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlmodel import Session, select
from sqlalchemy import or_
from pydantic import BaseModel

from embeddr.db.session import get_session
from embeddr_core.models.artifact import Artifact
from embeddr_core.models.workflow import WorkflowArtifactMetadata, WorkflowPort, WorkflowImplementation
from embeddr.core.execution_spine import ExecutionSpine
from embeddr_core.services.config_service import resolve_plugin_config
from embeddr.core.plugin_loader import get_all_plugin_instances, get_lotus_registry, _PLUGIN_CAPABILITY_REGISTRY
from embeddr.core.event_bus import _EVENT_BUS
from embeddr_core.plugin_interface import PluginContext
from embeddr_core.services.resource_manager import resource_manager
from embeddr.api.v1.lotus_service import lotus_prepare_inputs
from embeddr.api.security import get_auth_context, require_permission_for_request
from embeddr.auth.permissions import Permissions
from embeddr.services.template_registry import get_template, list_templates

# Ensure defaults are loaded
import embeddr.defaults.transformations

router = APIRouter(
    dependencies=[Depends(require_permission_for_request(
        Permissions.WORKFLOWS_READ, Permissions.WORKFLOWS_WRITE
    ))],
)

TYPE_WORKFLOW = "workflow"
LEGACY_WORKFLOW_TYPES = {"action:comfy.workflow"}


def _require_operator_scope(auth) -> Optional[UUID]:
    if not auth or auth.is_open or auth.is_admin or getattr(auth, "is_root", False):
        return None
    if not auth.operator_id:
        raise HTTPException(status_code=403, detail="Operator scope required")
    return auth.operator_id


def _apply_owner_filter(query, auth):
    operator_id = _require_operator_scope(auth)
    if operator_id:
        return query.where(Artifact.owner_operator_id == operator_id)
    return query


def _ensure_artifact_access(session: Session, artifact: Artifact, auth):
    operator_id = _require_operator_scope(auth)
    if operator_id and artifact.owner_operator_id != operator_id:
        raise HTTPException(status_code=404, detail="Workflow not found")


def _is_workflow_type(type_name: str) -> bool:
    return type_name == TYPE_WORKFLOW or type_name in LEGACY_WORKFLOW_TYPES


def _resolve_template(value: Any, inputs: Dict[str, Any], step_outputs: List[Dict[str, Any]]):
    if isinstance(value, str):
        pattern = re.compile(r"\$\{([^}]+)\}")

        def _replace(match: re.Match[str]) -> str:
            path = match.group(1)
            if path.startswith("inputs."):
                key = path.split(".", 1)[1]
                return str(inputs.get(key, ""))
            if path.startswith("steps."):
                parts = path.split(".")
                if len(parts) >= 3:
                    try:
                        idx = int(parts[1])
                    except ValueError:
                        return match.group(0)
                    if idx < 0 or idx >= len(step_outputs):
                        return match.group(0)
                    field = ".".join(parts[2:])
                    return str(step_outputs[idx].get(field, ""))
            return match.group(0)

        return pattern.sub(_replace, value)

    if isinstance(value, list):
        return [_resolve_template(v, inputs, step_outputs) for v in value]

    if isinstance(value, dict):
        return {
            k: _resolve_template(v, inputs, step_outputs)
            for k, v in value.items()
        }

    return value


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
    offset: int = 0,
    auth=Depends(get_auth_context),
):
    """
    List workflow artifacts.
    """
    query = _apply_owner_filter(
        select(Artifact)
        .where(
            or_(
                Artifact.type_name == TYPE_WORKFLOW,
                Artifact.type_name.in_(LEGACY_WORKFLOW_TYPES),
            )
        )
    )
    query = query.limit(limit).offset(offset)
    artifacts = session.exec(query).all()
    return artifacts


@router.post("")
async def create_workflow(
    payload: Dict[str, Any],
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    """
    Creates a new Workflow Artifact.
    Supports 'comfy-graph' import or 'core-transform' empty/template.
    """
    name = payload.get("name", "Untitled Workflow")
    description = payload.get("description")
    graph = payload.get("graph")
    template = payload.get("template")

    # Direct payload passed?
    payload_data = payload.get("payload")

    if graph:
        raise HTTPException(
            status_code=400,
            detail="Direct ComfyUI graph import is deprecated. Use the workflow importer capabilities."
        )
    elif template:
        # Load from defaults
        metadata_obj = get_template(template)
        payload_data = metadata_obj.model_dump()
    elif not payload_data:
        # Empty Core Transform or just shell
        metadata_obj = get_template("empty")
        payload_data = metadata_obj.model_dump()

    workflow_meta = payload_data.get(
        "workflow") if isinstance(payload_data, dict) else None
    if not workflow_meta:
        workflow_meta = payload_data

    # Create Artifact
    artifact = Artifact(
        type_name=TYPE_WORKFLOW,
        metadata_json={
            "name": name,
            "description": description,
            "workflow": workflow_meta,
        },
        owner_user_id=auth.user_id,
        owner_operator_id=auth.operator_id,
    )

    session.add(artifact)
    session.commit()
    session.refresh(artifact)
    return artifact


@router.post("/{id}/duplicate")
async def duplicate_workflow(
    id: UUID,
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    """Duplicates an existing workflow."""
    original = session.get(Artifact, id)
    if not original or not _is_workflow_type(original.type_name):
        raise HTTPException(status_code=404, detail="Workflow not found")
    _ensure_artifact_access(session, original, auth)

    new_meta = copy.deepcopy(original.metadata_json)
    new_meta["name"] = f"{new_meta.get('name')} (Copy)"

    new_artifact = Artifact(
        type_name=original.type_name if _is_workflow_type(
            original.type_name) else TYPE_WORKFLOW,
        metadata_json=new_meta,
        owner_user_id=auth.user_id,
        owner_operator_id=auth.operator_id,
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
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
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
        _ensure_artifact_access(session, a, auth)
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
        type_name=TYPE_WORKFLOW,
        metadata_json={
            "name": name,
            "workflow": meta.model_dump(),
        },
    )

    session.add(new_artifact)
    session.commit()
    session.refresh(new_artifact)
    return new_artifact


@router.get("/{id}")
async def get_workflow(id: UUID, session: Session = Depends(get_session)):
    artifact = session.get(Artifact, id)
    # Check type or base type
    if not artifact or not _is_workflow_type(artifact.type_name):
        raise HTTPException(
            status_code=404, detail="Workflow artifact not found")
    return artifact


@router.put("/{id}")
async def update_workflow(
    id: UUID,
    metadata: Dict[str, Any] = Body(...),
    session: Session = Depends(get_session)
):
    """
    Updates the workflow definition (exposure, inputs, etc).
    Does NOT execute it.
    """
    artifact = session.get(Artifact, id)
    if not artifact or not _is_workflow_type(artifact.type_name):
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Update the workflow spec part of metadata
    current_meta = artifact.metadata_json

    # Handle top-level fields (V2 style or general)
    # matching the frontend sending the full metadata_json object
    for field in ["name", "description", "interface", "payload", "graph"]:
        if field in metadata:
            current_meta[field] = metadata[field]

    # Handle Legacy 'workflow' object if present
    if "workflow" in metadata:
        current_meta["workflow"] = metadata["workflow"]

    # Explicitly mark as modified for SQLAlchemy
    from sqlalchemy.orm.attributes import flag_modified
    flag_modified(artifact, "metadata_json")

    session.add(artifact)
    session.commit()
    session.refresh(artifact)

    # Disk sync deprecated in V2. Workflows are DB-first.

    return artifact


@router.delete("/{workflow_id}")
def delete_workflow(workflow_id: UUID, session: Session = Depends(get_session)):
    """Delete a workflow."""
    artifact = session.get(Artifact, workflow_id)
    if not artifact or not _is_workflow_type(artifact.type_name):
        raise HTTPException(status_code=404, detail="Workflow not found")

    # Disk sync deprecated in V2.

    session.delete(artifact)
    session.commit()
    return {"ok": True}


@router.post("/sync")
def sync_workflows(session: Session = Depends(get_session)):
    """Sync workflows from disk to database."""
    # Disk sync deprecated in V2.
    # Use POST /workflows/import for importing JSON files.
    return {"status": "synced (noop)"}


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
    # Relax check to allow all "action:" types, and legacy "workflow"
    if not artifact or (not artifact.type_name.startswith("action:") and artifact.type_name != "workflow"):
        raise HTTPException(
            status_code=404, detail="Workflow/Action not found")

    # 1. Prepare the workflow data (graph)
    # Extract from metadata: metadata -> workflow -> implementation -> payload
    try:
        workflow_meta = artifact.metadata_json.get("workflow", {})
        # Support new direct payload path
        payload = artifact.metadata_json.get("payload")

        # Dispatch based on implementation type
        if payload:
            graph = copy.deepcopy(payload)

        else:
            impl_type = workflow_meta.get("implementation", {}).get("type")
            if impl_type == "lotus-action":
                payload = workflow_meta.get(
                    "implementation", {}).get("payload", {})
                cap_id = payload.get("capability_id")
                plugin_name = payload.get("plugin")
                action_name = payload.get("action")

                if cap_id:
                    reg = get_lotus_registry()
                    cap = reg.get(cap_id)
                    if not cap:
                        raise HTTPException(
                            status_code=404, detail=f"Capability not found: {cap_id}")
                    data = cap.data or {}
                    plugin_name = plugin_name or data.get(
                        "plugin") or cap.plugin
                    action_name = action_name or data.get("action")

                if not plugin_name or not action_name:
                    raise HTTPException(
                        status_code=400, detail="Lotus action workflow missing plugin/action")

                plugin = next(
                    (p for p in get_all_plugin_instances() if p.name == plugin_name),
                    None,
                )
                if not plugin:
                    raise HTTPException(
                        status_code=404, detail=f"Plugin not found: {plugin_name}")

                merged_inputs = lotus_prepare_inputs(
                    plugin_name=plugin_name,
                    inputs=req.inputs,
                    session=session,
                )
                ctx = PluginContext(
                    bus=_EVENT_BUS,
                    capability_registry=_PLUGIN_CAPABILITY_REGISTRY,
                    resources=resource_manager,
                    session=session,
                )
                outputs = plugin.execute(
                    action_name, None, merged_inputs, context=ctx)

                return {
                    "status": "completed",
                    "outputs": outputs,
                    "prompt_id": str(UUID(int=0)),
                }

            if impl_type == "lotus-composed":
                payload = workflow_meta.get(
                    "implementation", {}).get("payload", {})
                steps = payload.get("steps") or []
                if not steps:
                    raise HTTPException(
                        status_code=400, detail="No steps defined in workflow")

                reg = get_lotus_registry()
                step_outputs: List[Dict[str, Any]] = []
                results: List[Dict[str, Any]] = []

                for idx, step in enumerate(steps):
                    cap_id = step.get("capability_id")
                    if not cap_id:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Step {idx + 1} missing capability_id",
                        )
                    cap = reg.get(cap_id)
                    if not cap:
                        raise HTTPException(
                            status_code=404,
                            detail=f"Capability not found: {cap_id}",
                        )

                    data = cap.data or {}
                    plugin_name = data.get("plugin") or cap.plugin
                    action_name = data.get("action")
                    if not plugin_name or not action_name:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Capability missing plugin/action: {cap_id}",
                        )

                    plugin = next(
                        (p for p in get_all_plugin_instances()
                         if p.name == plugin_name),
                        None,
                    )
                    if not plugin:
                        raise HTTPException(
                            status_code=404,
                            detail=f"Plugin not found: {plugin_name}",
                        )

                    raw_inputs = step.get("inputs") or {}
                    resolved_inputs = _resolve_template(
                        raw_inputs, req.inputs, step_outputs)
                    merged_inputs = lotus_prepare_inputs(
                        plugin_name=plugin_name,
                        inputs=resolved_inputs,
                        session=session,
                    )
                    ctx = PluginContext(
                        bus=_EVENT_BUS,
                        capability_registry=_PLUGIN_CAPABILITY_REGISTRY,
                        resources=resource_manager,
                        session=session,
                    )
                    outputs = plugin.execute(
                        action_name, None, merged_inputs, context=ctx)
                    step_outputs.append(outputs or {})
                    results.append({
                        "capability_id": cap_id,
                        "outputs": outputs,
                    })

                return {
                    "status": "completed",
                    "outputs": {"steps": results},
                    "prompt_id": str(UUID(int=0)),
                }

            if impl_type == "core-transform":
                raise HTTPException(
                    status_code=410,
                    detail="core-transform workflows are deprecated. Use a Lotus action instead.",
                )

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

    # 2. Patch the graph with inputs
    # Handle flat inputs via interface mapping
    interface = artifact.metadata_json.get(
        "interface") or artifact.metadata_json.get("graph", {}).get("interface", {})
    exposed_inputs = interface.get("exposed_inputs", [])

    for key, value in req.inputs.items():
        # Check if direct node override (legacy/advanced)
        # Assuming Node ID keys are usually distinct from label keys?
        # A dictionary value almost certainly means node Override.
        if isinstance(value, dict) and key in graph:
            if "inputs" not in graph[key]:
                graph[key]["inputs"] = {}
            for k, v in value.items():
                graph[key]["inputs"][k] = v
            continue

        # Try to resolve key from interface
        target_input = None
        # 1. Exact Label Match
        for inp in exposed_inputs:
            if inp.get("label") == key:
                target_input = inp
                break

        # 1b. Fuzzy/Strip Match
        if not target_input:
            for inp in exposed_inputs:
                lbl = inp.get("label")
                if lbl and str(lbl).strip() == str(key).strip():
                    target_input = inp
                    break

        # 2. Node_Port Match
        if not target_input:
            for inp in exposed_inputs:
                if f"{inp.get('node')}_{inp.get('port')}" == key:
                    target_input = inp
                    break

        # Apply if found
        if target_input:
            node_id = str(target_input.get("node"))
            port_name = target_input.get("port")
            if node_id in graph:
                if "inputs" not in graph[node_id]:
                    graph[node_id]["inputs"] = {}
                graph[node_id]["inputs"][port_name] = value
            # Case where graph has int keys (unlikely but possible if manually constructed)
            elif int(node_id) in graph:
                pass  # Cannot handle easily without normalizing graph keys, assuming strings

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

    endpoint_url = None
    try:
        cfg = resolve_plugin_config(
            session=session,
            plugin_name="embeddr-comfyui",
            scope="global",
            scope_id=None,
        )
        if isinstance(cfg, dict):
            endpoint_url = cfg.get("endpoint_url")
    except Exception:
        endpoint_url = None

    if not endpoint_url:
        endpoint_url = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")

    if not endpoint_url:
        raise HTTPException(
            status_code=503, detail="ComfyUI endpoint is not configured")

    job = ExecutionSpine.submit_job(
        job_type="comfy.generate",
        inputs={
            "workflow": graph,
            "inputs": req.inputs,
            "client_id": "embeddr-workflows",
            "endpoint_url": endpoint_url,
            "workflow_artifact_id": str(workflow_id),
        },
        plugin_name="embeddr-comfyui",
        resource_class="gpu",
        priority=0,
        parent_execution_id=None,
        trigger="user",
    )

    return {
        "status": "queued",
        "execution_id": str(job.id),
        "prompt_id": None,
    }
