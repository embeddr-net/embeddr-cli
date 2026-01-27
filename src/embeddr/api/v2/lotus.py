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
from embeddr.api.v2.lotus_service import lotus_dispatch_action, lotus_dispatch_nav
from embeddr_core.models.lotus import LotusResult as CoreLotusResult

from datetime import UTC, datetime
from typing import Any, Dict, List, Literal, Optional
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
import logging

logger = logging.getLogger("embeddr.api.lotus")

router = APIRouter()


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


class LotusCapabilityOut(BaseModel):
    id: str
    kind: LotusKind
    title: str
    description: Optional[str] = None
    plugin: Optional[str] = None
    version: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    data: Dict[str, Any] = Field(default_factory=dict)
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
def lotus_query(q: str = Query(""), limit: int = 20):
    reg = get_lotus_registry()
    core_results: List[CoreLotusResult] = reg.query(q, limit=limit)

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
    ]
    return LotusQueryResponse(query=q, results=results)


@router.get("/list", response_model=LotusCapabilityListResponse)
def lotus_list(
    kind: Optional[LotusKind] = Query(None),
    plugin: Optional[str] = Query(None),
    slot: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    reg = get_lotus_registry()
    items = reg.list(kind=kind, slot=slot, plugin=plugin)
    total = len(items)
    paged = items[offset: offset + limit]

    def to_out(cap: LotusCapability) -> LotusCapabilityOut:
        return LotusCapabilityOut(
            id=cap.id,
            kind=cap.kind,
            title=cap.title,
            description=cap.description,
            plugin=cap.plugin,
            version=cap.version,
            tags=list(cap.tags or []),
            data=cap.data or {},
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

    # Support BOTH legacy and new keys:
    # - plugin_name / action_name
    # - plugin / action
    plugin_name = payload.data.get("plugin_name") or payload.data.get("plugin")
    action_name = payload.data.get("action_name") or payload.data.get("action")

    # inputs may arrive as "inputs" (legacy) or "input" (new-ish)
    inputs = payload.data.get("inputs")
    if inputs is None:
        inputs = payload.data.get("input")
    if inputs is None:
        inputs = {}
    if not isinstance(inputs, dict):
        raise HTTPException(
            status_code=400, detail="data.inputs (or data.input) must be an object")

    try:
        out = lotus_dispatch_action(
            result_id=payload.result_id,
            plugin_name=str(plugin_name),
            action_name=str(action_name),
            inputs=inputs,
            session=session,
            background_tasks=background_tasks,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

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
