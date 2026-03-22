# lotus_v2.py
# Drop-in router that provides:
#   GET  /api/v2/lotus/query?q=...&limit=...
#   POST /api/v2/lotus/dispatch   (action + nav)
#
# Assumes you already mount this router with:
#   app.include_router(lotus_v2.router, prefix="/api/v2/lotus", tags=["lotus"])
#
# Notes:
# - Uses your existing /exec/plugin execution pattern (ArtifactExecution + BackgroundTasks).
# - Keeps nav client-side by returning navigate_to.

from __future__ import annotations
from embeddr.api.v1.lotus_service import lotus_dispatch_action, lotus_dispatch_nav
from embeddr_core.models.lotus import LotusResult as CoreLotusResult

from datetime import UTC, datetime
from typing import Any, Dict, List, Literal, Optional
from uuid import UUID
from uuid import uuid4

from embeddr_core.models.lotus import LotusCapability, LotusKind
from embeddr_core.plugin_interface import PluginContext
from embeddr_core.services.lotus_registry import LotusRegistry
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session

from embeddr.db.session import get_session
from embeddr_core.models.artifact_execution import ArtifactExecution
from embeddr.core.plugin_loader import get_all_plugin_instances, get_lotus_registry
from embeddr.api.security import get_auth_context
from embeddr.services import auth_service
from embeddr.core.execution_spine import ExecutionSpine
import logging

logger = logging.getLogger("embeddr.api.lotus")

router = APIRouter()


def _resolve_wait_timeout_seconds(inputs: Dict[str, Any]) -> float:
    raw_timeout = (
        inputs.get("wait_timeout_seconds")
        or inputs.get("timeout_seconds")
        or inputs.get("timeout")
    )
    if raw_timeout is None:
        return 300.0
    try:
        timeout = float(raw_timeout)
    except (TypeError, ValueError):
        return 300.0
    return timeout if timeout > 0 else 300.0


# ----------------------------
# Models
# ----------------------------

class LotusResult(BaseModel):
    id: str
    kind: LotusKind
    title: str
    description: Optional[str] = None
    score: float = 1.0
    data: Dict[str, Any] = Field(default_factory=dict)


class LotusQueryResponse(BaseModel):
    query: str
    results: List[LotusResult]


class LotusDispatchRequest(BaseModel):
    result_id: str
    kind: LotusKind
    data: Dict[str, Any] = Field(default_factory=dict)


class LotusDispatchResponse(BaseModel):
    ok: bool = True
    kind: LotusKind
    execution_id: Optional[str] = None
    status: Optional[str] = None
    navigate_to: Optional[str] = None
    message: Optional[str] = None
    outputs: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class LotusCapabilityOut(BaseModel):
    id: str
    kind: LotusKind
    title: str
    description: Optional[str] = None
    plugin: Optional[str] = None
    version: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    data: Dict[str, Any] = Field(default_factory=dict)
    action: Optional[Dict[str, Any]] = None
    slot: Optional[str] = None


class LotusCapabilityListResponse(BaseModel):
    items: List[LotusCapabilityOut]
    total: int
    limit: int
    offset: int


class LotusDiagnosticsResponse(BaseModel):
    missing_requirements: Dict[str, List[str]]
    total_capabilities: int


# ----------------------------
# Query (seed happy path)
# ----------------------------


# Create once at import time
_registry = get_lotus_registry()

# Seed once TODO: REMOVE in prod, use plugin registration only
# _registry.register(LotusCapability(
#     id="core.thumbnail.generate",
#     kind=LotusKind.action,
#     title="Generate Thumbnail",
#     description="Create or refresh a thumbnail preview for selected artifacts.",
#     plugin="core",
#     data={"plugin_name": "core", "action_name": "thumbnail.generate", "inputs": {}},
# ))
# _registry.register(LotusCapability(
#     id="nav:/",
#     kind=LotusKind.nav,
#     title="Go Home",
#     description="Navigate to the Zen workspace.",
#     plugin="core",
#     data={"route": "/"},
# ))


@router.get("/query", response_model=LotusQueryResponse)
def lotus_query(
    q: str = Query(""),
    limit: int = 20,
    auth=Depends(get_auth_context),
):
    reg = get_lotus_registry()
    core_results: List[CoreLotusResult] = reg.query(q, limit=limit)

    def allowed(cap_id: str) -> bool:
        if auth.is_open or auth.is_admin or getattr(auth, "is_root", False):
            return True
        if auth.can("lotus:list") or auth.can("lotus:*"):
            return True
        return auth.can(auth_service.lotus_permission_for_capability(cap_id))

    results = [
        LotusResult(
            id=r.capability.id,
            kind=r.capability.kind,
            title=r.capability.title,
            description=r.capability.description,
            score=r.score,
            data=r.capability.data,
        )
        for r in core_results
        if allowed(r.capability.id)
    ]
    return LotusQueryResponse(query=q, results=results)


@router.get("/list", response_model=LotusCapabilityListResponse)
def lotus_list(
    kind: Optional[LotusKind] = Query(None),
    plugin: Optional[str] = Query(None),
    slot: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    auth=Depends(get_auth_context),
):
    reg = get_lotus_registry()
    items = reg.list(kind=kind, slot=slot, plugin=plugin)

    def allowed(cap_id: str) -> bool:
        if auth.is_open or auth.is_admin or getattr(auth, "is_root", False):
            return True
        if auth.can("lotus:list") or auth.can("lotus:*"):
            return True
        return auth.can(auth_service.lotus_permission_for_capability(cap_id))

    items = [cap for cap in items if allowed(cap.id)]
    total = len(items)
    paged = items[offset: offset + limit]

    def to_out(cap: LotusCapability) -> LotusCapabilityOut:
        action_dict = None
        if cap.action:
            action_dict = cap.action.model_dump(mode="python") if hasattr(cap.action, "model_dump") else {
                "action": cap.action.action,
                "job_type": cap.action.job_type,
                "exec": cap.action.exec or {},
                "expose": cap.action.expose or {},
                "input": cap.action.input,
                "output": cap.action.output,
            }
        return LotusCapabilityOut(
            id=cap.id,
            kind=cap.kind,
            title=cap.title,
            description=cap.description,
            plugin=cap.plugin,
            version=cap.version,
            tags=list(cap.tags or []),
            data=cap.data or {},
            action=action_dict,
            slot=getattr(cap, "slot", None),
        )

    return LotusCapabilityListResponse(
        items=[to_out(c) for c in paged],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/diagnostics", response_model=LotusDiagnosticsResponse)
def lotus_diagnostics():
    reg = get_lotus_registry()
    return LotusDiagnosticsResponse(
        missing_requirements=reg.get_missing_requirements(),
        total_capabilities=len(reg.list()),
    )


# ----------------------------
# Dispatch
# ----------------------------


def _resolve_plugin(plugin_name: str):
    for p in get_all_plugin_instances():
        if p.name == plugin_name:
            return p
    return None


@router.post("/dispatch", response_model=LotusDispatchResponse)
async def lotus_dispatch(
    payload: LotusDispatchRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    """
    Dispatch a Lotus result.
    - kind=nav: lotus_dispatch_nav(route=...)
    - kind=action: lotus_dispatch_action(...) -> queues ArtifactExecution via ExecutionSpine
    """
    if payload.kind == LotusKind.nav:
        route = payload.data.get("route")
        try:
            if not route or not isinstance(route, str):
                raise ValueError("nav requires data.route as a string")
            out = lotus_dispatch_nav(route=route)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
        return LotusDispatchResponse(**out)

    if payload.kind not in (LotusKind.action, LotusKind.feature):
        raise HTTPException(
            status_code=400, detail=f"Unsupported kind: {payload.kind}")

    cap = get_lotus_registry().get(payload.result_id)
    if cap:
        if not (auth.is_open or auth.is_admin or getattr(auth, "is_root", False)):
            allowed = auth.can("lotus:dispatch") or auth.can("lotus:*")
            allowed = allowed or auth.can(
                auth_service.lotus_permission_for_capability(cap.id))
            if not allowed:
                raise HTTPException(status_code=403, detail="Not authorized")

    plugin_name = payload.data.get("plugin_name") or payload.data.get("plugin")
    action_name = payload.data.get("action_name") or payload.data.get("action")

    # If we resolved the capability, prefer its authoritative action name
    # over whatever the caller sent — the capability's action field matches
    # the spine handler registration (e.g. "llm.respond" vs suffix "respond").
    if cap and hasattr(cap, "action") and cap.action:
        cap_action = getattr(cap.action, "action", None)
        if cap_action:
            action_name = cap_action
        if not plugin_name:
            cap_plugin = getattr(cap.action, "plugin",
                                 None) or getattr(cap, "plugin", None)
            if cap_plugin:
                plugin_name = cap_plugin

    if not plugin_name or not action_name:
        raise HTTPException(
            status_code=400,
            detail="data.plugin_name and data.action_name are required",
        )

    inputs = payload.data.get("inputs", {})
    if not isinstance(inputs, dict):
        raise HTTPException(
            status_code=400, detail="data.inputs must be an object")

    try:
        out = lotus_dispatch_action(
            result_id=payload.result_id,
            plugin_name=str(plugin_name),
            action_name=str(action_name),
            inputs=inputs,
            session=session,
            capability=cap,
            auth=auth,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    should_wait = bool(inputs.get("wait_for_completion"))
    execution_id = out.get("execution_id") if isinstance(out, dict) else None
    if should_wait and execution_id:
        timeout = _resolve_wait_timeout_seconds(inputs)
        try:
            result = await ExecutionSpine.wait_for_job_async(
                UUID(str(execution_id)), timeout=timeout,
            )
            final_status = result.status or "unknown"
            out = {
                "ok": final_status != "failed",
                "kind": LotusKind.action,
                "execution_id": str(execution_id),
                "status": final_status,
                "message": "completed" if final_status == "completed" else final_status,
                "outputs": result.outputs or {},
                "error": result.error,
            }
        except TimeoutError:
            out = {
                "ok": True,
                "kind": LotusKind.action,
                "execution_id": str(execution_id),
                "status": "timeout",
                "message": f"Timed out waiting for completion after {timeout:.1f}s",
            }
        except Exception as exc:
            logger.warning(
                "Failed waiting for completion result_id=%s execution_id=%s: %s",
                payload.result_id,
                execution_id,
                exc,
            )

    return LotusDispatchResponse(**out)

# @router.post("/dispatch", response_model=LotusDispatchResponse)
# async def lotus_dispatch(
#     payload: LotusDispatchRequest,
#     background_tasks: BackgroundTasks,
#     session: Session = Depends(get_session),
# ):
#     if payload.kind == LotusKind.nav:
#         route = payload.data.get("route")
#         try:
#             if not route or not isinstance(route, str):
#                 raise ValueError("nav requires data.route as a string")
#             out = lotus_dispatch_nav(route=route)
#         except Exception as e:
#             raise HTTPException(status_code=400, detail=str(e))
#         return LotusDispatchResponse(**out)

#     if payload.kind != LotusKind.action:
#         raise HTTPException(
#             status_code=400, detail=f"Unsupported kind: {payload.kind}")

#     plugin_name = payload.data.get("plugin_name")
#     action_name = payload.data.get("action_name")
#     inputs = payload.data.get("inputs", {}) or {}

#     try:
#         out = lotus_dispatch_action(
#             result_id=payload.result_id,
#             plugin_name=str(plugin_name),
#             action_name=str(action_name),
#             inputs=inputs,
#             session=session,
#             background_tasks=background_tasks,
#         )
#     except Exception as e:
#         raise HTTPException(status_code=400, detail=str(e))

#     return LotusDispatchResponse(**out)

# @router.post("/dispatch", response_model=LotusDispatchResponse)
# async def lotus_dispatch(
#     payload: LotusDispatchRequest,
#     background_tasks: BackgroundTasks,
#     session: Session = Depends(get_session),
# ):
#     """
#     Dispatch a Lotus result.
#     - kind=action: executes a plugin action via ArtifactExecution + background task
#       expects payload.data: { plugin_name: str, action_name: str, inputs?: dict }
#     - kind=nav: returns { navigate_to: route } (client performs navigation)
#       expects payload.data: { route: str }
#     """
#     if payload.kind == "nav":
#         route = payload.data.get("route")
#         if not route or not isinstance(route, str):
#             raise HTTPException(
#                 status_code=400, detail="nav requires data.route as a string")
#         return LotusDispatchResponse(kind="nav", navigate_to=route, message="navigate")

#     if payload.kind != "action":
#         # For now only action + nav are supported in the happy path.
#         raise HTTPException(
#             status_code=400, detail=f"Unsupported kind for dispatch: {payload.kind}")

#     plugin_name = payload.data.get("plugin_name")
#     action_name = payload.data.get("action_name")
#     inputs = payload.data.get("inputs", {}) or {}

#     if not plugin_name or not action_name:
#         raise HTTPException(
#             status_code=400, detail="action dispatch requires data.plugin_name and data.action_name")

#     if not isinstance(inputs, dict):
#         raise HTTPException(
#             status_code=400, detail="data.inputs must be an object/dict")

#     execution_id = uuid4()

#     # Create execution record (queued)
#     execution = ArtifactExecution(
#         id=execution_id,
#         type="action.run",
#         plugin_name=str(plugin_name),
#         status="queued",
#         priority=10,
#         progress=0,
#         inputs=inputs,
#         created_at=datetime.now(UTC),
#     )
#     session.add(execution)
#     session.commit()

#     def _run_wrapper():
#         """
#         Background execution wrapper for Lotus action dispatch.
#         - Updates ArtifactExecution status in DB
#         - Calls plugin.execute(...) WITH PluginContext
#         - Emits bus events for UI + other subscribers
#         """
#         from sqlmodel import Session as SQLSession  # local import for bg thread
#         from embeddr.db.session import get_engine
#         from embeddr.core.event_bus import _EVENT_BUS
#         from embeddr_core.plugin_interface import PluginContext
#         from embeddr_core.services.resource_manager import resource_manager
#         from embeddr.core.plugin_loader import _PLUGIN_CAPABILITY_REGISTRY

#         # Optional: log immediately so you know wrapper ran
#         logger.info(
#             f"[Lotus] Running action {plugin_name}.{action_name} execution_id={execution_id}"
#         )

#         # Build context that plugins can use to emit events, access resources, etc.
#         ctx = PluginContext(
#             bus=_EVENT_BUS,
#             capability_registry=_PLUGIN_CAPABILITY_REGISTRY,
#             resources=resource_manager,
#         )

#         # Emit queued→running lifecycle events (UI can reflect status)
#         _EVENT_BUS.emit(
#             "execution.started",
#             {
#                 "execution_id": str(execution_id),
#                 "plugin_name": str(plugin_name),
#                 "action_name": str(action_name),
#                 "inputs": inputs,
#             },
#             source="lotus",
#         )

#         # Also toast when action starts (optional)
#         # _EVENT_BUS.emit(
#         #     "ui:toast",
#         #     {
#         #         "title": "Lotus",
#         #         "description": f"Running {plugin_name}.{action_name}",
#         #         "variant": "default",
#         #     },
#         #     source="lotus",
#         # )

#         with SQLSession(get_engine()) as bg_sess:
#             bg_exec = bg_sess.get(ArtifactExecution, execution_id)
#             if not bg_exec:
#                 logger.warning(
#                     f"[Lotus] Missing ArtifactExecution row for {execution_id}"
#                 )
#                 return

#             bg_exec.status = "running"
#             bg_exec.started_at = datetime.now(UTC)
#             bg_sess.add(bg_exec)
#             bg_sess.commit()

#             try:
#                 plugin = _resolve_plugin(str(plugin_name))
#                 if not plugin:
#                     raise ValueError(f"Plugin {plugin_name} not found")

#                 # ✅ IMPORTANT: pass context
#                 result = plugin.execute(
#                     str(action_name),
#                     execution_id,
#                     inputs,
#                     context=ctx,
#                 )

#                 bg_exec.status = "completed"
#                 bg_exec.finished_at = datetime.now(UTC)
#                 bg_exec.outputs = result
#                 bg_exec.progress = 100
#                 bg_sess.add(bg_exec)
#                 bg_sess.commit()

#                 logger.info(
#                     f"[Lotus] Completed {plugin_name}.{action_name} execution_id={execution_id}"
#                 )

#                 _EVENT_BUS.emit(
#                     "execution.completed",
#                     {
#                         "execution_id": str(execution_id),
#                         "plugin_name": str(plugin_name),
#                         "action_name": str(action_name),
#                         "outputs": result,
#                     },
#                     source="lotus",
#                 )

#                 # ✅ toast on completion
#                 # _EVENT_BUS.emit(
#                 #     "ui:toast",
#                 #     {
#                 #         "title": "Done",
#                 #         "description": f"{plugin_name}.{action_name} completed",
#                 #         "variant": "success",
#                 #     },
#                 #     source="lotus",
#                 # )

#             except Exception as e:
#                 err = str(e)
#                 logger.exception(
#                     f"[Lotus] Failed {plugin_name}.{action_name} execution_id={execution_id}: {err}"
#                 )

#                 bg_exec.status = "failed"
#                 bg_exec.error = err
#                 bg_exec.finished_at = datetime.utcnow()
#                 bg_sess.add(bg_exec)
#                 bg_sess.commit()

#                 _EVENT_BUS.emit(
#                     "execution.failed",
#                     {
#                         "execution_id": str(execution_id),
#                         "plugin_name": str(plugin_name),
#                         "action_name": str(action_name),
#                         "error": err,
#                     },
#                     source="lotus",
#                 )

#                 _EVENT_BUS.emit(
#                     "ui:toast",
#                     {
#                         "title": "Error",
#                         "description": f"{plugin_name}.{action_name} failed: {err}",
#                         "variant": "destructive",
#                     },
#                     source="lotus",
#                 )

#     background_tasks.add_task(_run_wrapper)

#     return LotusDispatchResponse(
#         kind=LotusKind.action,
#         execution_id=str(execution_id),
#         status="queued",
#         message="queued",
#     )
