from typing import List, Dict, Any, Optional
from uuid import UUID, uuid4
from datetime import UTC, datetime
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlmodel import Session, select
from embeddr.db.session import get_session
from embeddr_core.models.artifact import Artifact
from embeddr_core.models.action_graph import ActionGraph
from embeddr_core.models.artifact_execution import ArtifactExecution
from embeddr_core.services.action_runner import ActionRunner
from embeddr.core.execution_spine import ExecutionSpine
from embeddr.core.plugin_loader import get_all_plugin_instances, _EVENT_BUS

router = APIRouter()

# Simple simplified dependency to get runner


def get_action_runner(session: Session = Depends(get_session)):
    def resolve_plugin(name: str):
        plugins = get_all_plugin_instances()
        for p in plugins:
            if p.name == name:
                return p
        return None

    return ActionRunner(session, resolve_plugin, event_bus=_EVENT_BUS)


@router.post("/exec/plugin")
async def execute_plugin_action(
    payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
    runner: ActionRunner = Depends(get_action_runner),
    session: Session = Depends(get_session)
):
    """
    Directly execute a plugin action without a persisted graph artifact.
    Payload: { "plugin_name": "...", "action_name": "...", "inputs": {} }
    """
    plugin_name = payload.get("plugin_name")
    action_name = payload.get("action_name")
    inputs = payload.get("inputs", {})

    if not plugin_name or not action_name:
        raise HTTPException(
            status_code=400, detail="plugin_name and action_name required")

    execution_id = uuid4()

    # Create execution record
    execution = ArtifactExecution(
        id=execution_id,
        type="action.run",
        plugin_name=plugin_name,
        status="queued",
        priority=10,
        progress=0,
        inputs=inputs,
        created_at=datetime.now(UTC)
    )
    session.add(execution)
    session.commit()

    def _run_wrapper():
        try:
            # Manually resolve plugin specifically for this simple call
            from embeddr.db.session import get_engine
            with Session(get_engine()) as bg_sess:
                bg_exec = bg_sess.get(ArtifactExecution, execution_id)
                if not bg_exec:
                    return

                bg_exec.status = "running"
                bg_exec.started_at = datetime.now(UTC)
                bg_sess.add(bg_exec)
                bg_sess.commit()

                try:
                    # Resolve plugin
                    plugin = None
                    for p in get_all_plugin_instances():
                        if p.name == plugin_name:
                            plugin = p
                            break

                    if not plugin:
                        raise ValueError(f"Plugin {plugin_name} not found")

                    result = plugin.execute(action_name, execution_id, inputs)

                    bg_exec.status = "completed"
                    bg_exec.finished_at = datetime.now(UTC)
                    bg_exec.outputs = result
                    bg_exec.progress = 100
                    bg_sess.add(bg_exec)
                    bg_sess.commit()

                except Exception as e:
                    bg_exec.status = "failed"
                    bg_exec.error = str(e)
                    bg_exec.finished_at = datetime.now(UTC)
                    bg_sess.add(bg_exec)
                    bg_sess.commit()

        except Exception as e:
            pass  # logging needed

    background_tasks.add_task(_run_wrapper)

    return {"execution_id": str(execution_id), "status": "queued"}


@router.post("/run")
async def run_action(
    payload: Dict[str, Any],
    background_tasks: BackgroundTasks,
    runner: ActionRunner = Depends(get_action_runner),
    session: Session = Depends(get_session)
):
    """
    Run an action artifact.
    Payload: { "artifact_id": "...", "inputs": {...} }
    """
    artifact_id = payload.get("artifact_id")
    inputs = payload.get("inputs", {})

    if not artifact_id:
        raise HTTPException(status_code=400, detail="artifact_id required")

    try:
        artifact_uuid = UUID(artifact_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid artifact_id")

    artifact = session.get(Artifact, artifact_uuid)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    if artifact.type_name != "action:graph":
        raise HTTPException(
            status_code=400, detail="Artifact is not an action graph")

    try:
        graph = ActionGraph(**artifact.metadata_json["graph"])

        # Create execution record immediately so we can return ID
        execution_id = uuid4()
        execution = ArtifactExecution(
            id=execution_id,
            type="workflow.run",
            plugin_name="system",
            status="queued",
            priority=10,
            progress=0,
            primary_artifact_id=artifact.id,
            inputs=inputs,
            created_at=datetime.now(UTC)
        )
        session.add(execution)
        session.commit()

        # Run in background to avoid blocking API
        background_tasks.add_task(
            runner.run_graph,
            artifact.id,
            graph,
            inputs,
            None,  # parent_execution_id
            execution_id  # execution_id
        )

        return {"execution_id": str(execution_id), "status": "queued"}

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
