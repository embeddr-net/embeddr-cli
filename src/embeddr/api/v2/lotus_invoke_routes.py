from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Type

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session

from embeddr.db.session import get_engine, get_session
from embeddr.core.plugin_loader import get_lotus_registry, get_all_plugin_instances, _PLUGIN_CAPABILITY_REGISTRY
from embeddr.api.v2.lotus_service import lotus_dispatch_action, lotus_prepare_inputs
from embeddr.api.security import get_auth_context
from embeddr.services import auth_service
from embeddr.core.event_bus import _EVENT_BUS
from embeddr_core.plugin_interface import PluginContext
from embeddr_core.services.resource_manager import resource_manager
from embeddr_core.models.lotus import LotusCapability, LotusKind
from embeddr_core.services.config_service import resolve_plugin_config
from embeddr.core.plugin_context_helpers import LotusContext

from embeddr.core.model_imports import import_model_with_fallbacks

logger = logging.getLogger("embeddr.api.lotus.invoke")

router = APIRouter()


def _log_lotus_call(
    *,
    cap_id: str,
    plugin_name: str,
    action_name: str,
    auth,
    transport: str,
    outcome: str = "start",
) -> None:
    if not logger.isEnabledFor(logging.INFO):
        return

    user_id = getattr(auth, "user_id", None)
    username = getattr(auth, "username", None)
    api_key_id = getattr(auth, "api_key_id", None)
    api_key_name = getattr(auth, "api_key_name", None)
    perms = getattr(auth, "permissions", None)

    logger.info(
        "[LotusCall] outcome=%s transport=%s cap_id=%s plugin=%s action=%s user_id=%s username=%s api_key_id=%s api_key_name=%s perms=%s",
        outcome,
        transport,
        cap_id,
        plugin_name,
        action_name,
        user_id,
        username,
        api_key_id,
        api_key_name,
        sorted(perms) if perms else None,
    )


def _resolve_plugin(plugin_name: str):
    for p in get_all_plugin_instances():
        if p.name == plugin_name:
            return p
    return None


def _cap_exposes_api(cap: LotusCapability) -> bool:
    return _cap_exposes_transport(cap, "lotus")


def _cap_exposes_transport(cap: LotusCapability, transport: str) -> bool:
    policy = _resolve_transport_policy()
    if policy is not None:
        if not policy.get("enabled", False):
            policy = None
        else:
            capability_allow = policy.get("capability_allow", {}) or {}
            plugin_allow = policy.get("plugin_allow", {}) or {}
            default_allow = policy.get("default_allow", {}) or {}

            if cap.id in capability_allow:
                return bool(capability_allow[cap.id].get(transport, False))
            if cap.plugin and cap.plugin in plugin_allow:
                return bool(plugin_allow[cap.plugin].get(transport, False))
            return bool(default_allow.get(transport, False))

    expose = {}
    if cap.action:
        expose = cap.action.expose or {}
    if not expose:
        data = cap.data or {}
        expose = data.get("expose") or {}
    return bool(expose.get(transport, False))


def _resolve_transport_policy() -> Optional[Dict[str, Any]]:
    try:
        with Session(get_engine()) as session:
            policy = resolve_plugin_config(
                session=session,
                plugin_name="embeddr-core",
                config_id="embeddr-core.transport.policy",
            )
            return policy if isinstance(policy, dict) and policy else None
    except Exception:
        return None


def _cap_input_model(cap: LotusCapability) -> Optional[Type[BaseModel]]:
    data = cap.data or {}
    input_block = None
    if cap.action and cap.action.input:
        input_block = cap.action.input
    if input_block is None:
        input_block = data.get("input") or {}
    model_path = input_block.get("model")

    plugin_name = str(data.get("plugin") or cap.plugin or "")
    plugin_module = data.get("plugin_module")

    if not isinstance(model_path, str) or not model_path.strip():
        return None

    return import_model_with_fallbacks(
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
    auth=Depends(get_auth_context),
):
    reg = get_lotus_registry()
    cap = reg.get(cap_id)
    if not cap:
        raise HTTPException(404, f"Capability not found: {cap_id}")
    if cap.kind != LotusKind.action:
        raise HTTPException(400, "Only action capabilities can be invoked")
    if not _cap_exposes_api(cap):
        raise HTTPException(403, f"Capability not exposed to API: {cap_id}")

    if not (auth.is_open or auth.is_admin):
        allowed = auth.can("lotus:dispatch") or auth.can("lotus:*")
        allowed = allowed or auth.can(
            auth_service.lotus_permission_for_capability(cap.id))
        if not allowed:
            raise HTTPException(status_code=403, detail="Not authorized")

    data = cap.data or {}
    plugin_name = str(data.get("plugin") or cap.plugin or "")
    action_name = str(data.get("action") or "")
    if cap.action:
        if cap.action.action:
            action_name = cap.action.action
        if cap.plugin:
            plugin_name = cap.plugin
    if not plugin_name or not action_name:
        raise HTTPException(500, f"Capability missing plugin/action: {cap_id}")

    _log_lotus_call(
        cap_id=cap_id,
        plugin_name=plugin_name,
        action_name=action_name,
        auth=auth,
        transport="lotus",
    )

    inputs = input or {}

    # Validate/normalize inputs via capability model (if provided)
    Model = _cap_input_model(cap)
    if Model is not None:
        obj = Model.model_validate(inputs)
        inputs = obj.model_dump()

    exec_cfg = (cap.action.exec if cap.action else None) or (
        data.get("exec") or {})
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
            config=resolve_plugin_config(
                session=session,
                plugin_name=plugin_name,
            ),
            lotus=LotusContext(session=session),
            user_id=getattr(auth, "user_id", None),
            operator_id=getattr(auth, "operator_id", None),
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

        _log_lotus_call(
            cap_id=cap_id,
            plugin_name=plugin_name,
            action_name=action_name,
            auth=auth,
            transport="lotus",
            outcome="ok",
        )
        return out

    # ✅ ASYNC path (existing behaviour)
    if background_tasks is None:
        raise HTTPException(500, "background_tasks missing for async invoke")

    response = lotus_dispatch_action(
        result_id=cap_id,
        plugin_name=plugin_name,
        action_name=action_name,
        inputs=inputs,
        session=session,
        background_tasks=background_tasks,
    )
    _log_lotus_call(
        cap_id=cap_id,
        plugin_name=plugin_name,
        action_name=action_name,
        auth=auth,
        transport="lotus",
        outcome="queued",
    )
    return response
