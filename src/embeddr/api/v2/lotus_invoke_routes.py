from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Type

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from embeddr.db.session import get_session
from embeddr.core.plugin_loader import get_lotus_registry, get_all_plugin_instances, _PLUGIN_CAPABILITY_REGISTRY
from embeddr.api.v2.lotus_service import lotus_dispatch_action, lotus_prepare_inputs
from embeddr.core.event_bus import _EVENT_BUS
from embeddr_core.plugin_interface import PluginContext
from embeddr_core.services.resource_manager import resource_manager
from embeddr_core.models.lotus import LotusCapability, LotusKind

from embeddr.mcp.tools.lotus import _import_model_with_fallbacks  # ok for now

logger = logging.getLogger("embeddr.api.lotus.invoke")

router = APIRouter()


def _resolve_plugin(plugin_name: str):
    for p in get_all_plugin_instances():
        if p.name == plugin_name:
            return p
    return None


def _cap_exposes_api(cap: LotusCapability) -> bool:
    data = cap.data or {}
    expose = data.get("expose") or {}
    return bool(expose.get("api", False))


def _cap_input_model(cap: LotusCapability) -> Optional[Type[BaseModel]]:
    data = cap.data or {}
    input_block = data.get("input") or {}
    model_path = input_block.get("model")

    plugin_name = str(data.get("plugin") or cap.plugin or "")
    plugin_module = data.get("plugin_module")

    if not isinstance(model_path, str) or not model_path.strip():
        return None

    return _import_model_with_fallbacks(
        model_path,
        plugin_name=plugin_name,
        plugin_module=plugin_module,
    )


@router.post("/{cap_id}")
def lotus_invoke(
    cap_id: str,
    input: Dict[str, Any] = None,
    background_tasks: BackgroundTasks = None,
    session: Session = Depends(get_session),
):
    reg = get_lotus_registry()
    cap = reg.get(cap_id)
    if not cap:
        raise HTTPException(404, f"Capability not found: {cap_id}")
    if cap.kind != LotusKind.action:
        raise HTTPException(400, "Only action capabilities can be invoked")
    if not _cap_exposes_api(cap):
        raise HTTPException(403, f"Capability not exposed to API: {cap_id}")

    data = cap.data or {}
    plugin_name = str(data.get("plugin") or "")
    action_name = str(data.get("action") or "")
    if not plugin_name or not action_name:
        raise HTTPException(500, f"Capability missing plugin/action: {cap_id}")

    inputs = input or {}

    # Validate/normalize inputs via capability model (if provided)
    Model = _cap_input_model(cap)
    if Model is not None:
        obj = Model.model_validate(inputs)
        inputs = obj.model_dump()

    exec_cfg = (data.get("exec") or {})
    mode = exec_cfg.get("mode", "async")
    emit_events = bool(exec_cfg.get("emit_events", False))

    # ✅ SYNC path (returns outputs immediately)
    if mode == "sync":
        plugin = _resolve_plugin(plugin_name)
        if not plugin:
            raise HTTPException(400, f"Plugin not found: {plugin_name}")

        ctx = PluginContext(
            bus=_EVENT_BUS,
            capability_registry=_PLUGIN_CAPABILITY_REGISTRY,
            resources=resource_manager,
        )

        if emit_events:
            _EVENT_BUS.emit(
                "execution.started",
                {
                    "execution_id": None,
                    "result_id": cap_id,
                    "plugin_name": plugin_name,
                    "action_name": action_name,
                    "inputs": inputs,
                    "mode": "sync",
                },
                source="lotus",
            )

        try:
            inputs = lotus_prepare_inputs(
                plugin_name=plugin_name,
                inputs=inputs,
                session=session,
            )
            out = plugin.execute(action_name, None, inputs, context=ctx)
        except Exception as e:
            if emit_events:
                _EVENT_BUS.emit(
                    "execution.failed",
                    {
                        "execution_id": None,
                        "result_id": cap_id,
                        "plugin_name": plugin_name,
                        "action_name": action_name,
                        "error": str(e),
                        "mode": "sync",
                    },
                    source="lotus",
                )
            raise HTTPException(500, str(e))

        if emit_events:
            _EVENT_BUS.emit(
                "execution.completed",
                {
                    "execution_id": None,
                    "result_id": cap_id,
                    "plugin_name": plugin_name,
                    "action_name": action_name,
                    "outputs": out,
                    "mode": "sync",
                },
                source="lotus",
            )

        return out

    # ✅ ASYNC path (existing behaviour)
    if background_tasks is None:
        raise HTTPException(500, "background_tasks missing for async invoke")

    return lotus_dispatch_action(
        result_id=cap_id,
        plugin_name=plugin_name,
        action_name=action_name,
        inputs=inputs,
        session=session,
        background_tasks=background_tasks,
    )
