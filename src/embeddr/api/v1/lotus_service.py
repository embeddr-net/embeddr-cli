import logging
import os
from typing import Any, Dict
from uuid import UUID

from embeddr_core.services.config_service import resolve_plugin_config
from embeddr_core.models.lotus import LotusKind
from sqlmodel import Session

from embeddr.core.execution_spine import ExecutionSpine
from embeddr.core.event_bus import _EVENT_BUS
from embeddr.core.plugin_loader import get_all_plugin_instances, get_lotus_registry, get_plugin_instance
from embeddr_core.models.artifact_execution import ArtifactExecution
from embeddr_core.models.artifact_execution_event import ArtifactExecutionEvent
from embeddr_core.plugin_interface import EmbeddrEvent
from embeddr.api.v1.lotus_middleware import (
    build_post_middleware_payload,
    evaluate_pre_dispatch_middlewares,
)

logger = logging.getLogger("embeddr.api.lotus")


def _materialize_wait_payload(value: Any, *, execution_id: str) -> Any:
    if isinstance(value, str):
        return value.replace("$execution_id", execution_id)
    if isinstance(value, list):
        return [_materialize_wait_payload(item, execution_id=execution_id) for item in value]
    if isinstance(value, dict):
        return {
            key: _materialize_wait_payload(item, execution_id=execution_id)
            for key, item in value.items()
        }
    return value


def _build_dispatch_auth_payload(
    auth: Any | None,
    caller_client_id: str | None = None,
) -> Dict[str, Any] | None:
    if auth is None:
        if caller_client_id:
            return {"client_id": caller_client_id}
        return None

    payload = {
        "mode": getattr(auth, "mode", None),
        "user_id": str(getattr(auth, "user_id", None)) if getattr(auth, "user_id", None) else None,
        "operator_id": str(getattr(auth, "operator_id", None)) if getattr(auth, "operator_id", None) else None,
        "api_key_id": str(getattr(auth, "api_key_id", None)) if getattr(auth, "api_key_id", None) else None,
        "api_key": getattr(auth, "raw_key", None),
        "is_admin": bool(getattr(auth, "is_admin", False)),
        "is_root": bool(getattr(auth, "is_root", False)),
        "is_open": bool(getattr(auth, "is_open", False)),
        "permissions": sorted(getattr(auth, "permissions", []) or []),
    }
    if caller_client_id:
        payload["client_id"] = caller_client_id
    return payload


def _should_trace() -> bool:
    return os.environ.get("EMBEDDR_LOTUS_TRACE") == "1"


def _redact(value: Dict[str, Any]) -> Dict[str, Any]:
    redacted = {}
    for k, v in (value or {}).items():
        if str(k).lower() in {"api_key", "apikey", "apiKey", "token", "authorization"}:
            redacted[k] = "***"
        else:
            redacted[k] = v
    return redacted


def lotus_prepare_inputs(
    *,
    plugin_name: str,
    inputs: Dict[str, Any] | None,
    session: Session,
) -> Dict[str, Any]:
    raw_inputs = dict(inputs or {})

    # Do NOT drop None inputs aggressively for ComfyUI or others that might need empty inputs
    # clean_inputs = {k: v for k, v in raw_inputs.items() if v is not None}

    # Just merge? Or maybe only drop top-level Nones if really needed?
    # The original code dropped None, which might have killed "inputs": None? No, "inputs": {} is empty dict.
    # If inputs={"inputs": None}, then "inputs" key is removed.

    clean_inputs = {k: v for k, v in raw_inputs.items() if v is not None}

    if len(clean_inputs) != len(raw_inputs):
        dropped = sorted(set(raw_inputs.keys()) - set(clean_inputs.keys()))
        logger.warning(
            "[Lotus] Dropping None inputs for %s: %s",
            plugin_name,
            dropped,
        )
    merged = dict(clean_inputs)

    try:
        config_id = None
        try:
            reg = get_lotus_registry()
            caps = reg.list(kind=LotusKind.config, plugin=str(plugin_name))
            if caps:
                config_id = caps[0].id
        except Exception:
            config_id = None

        cfg = resolve_plugin_config(
            session=session,
            plugin_name=str(plugin_name),
            scope="global",
            scope_id=None,
            config_id=config_id,
        )
        if isinstance(cfg, dict) and cfg:
            # cfg provides defaults, caller overrides
            merged = {**cfg, **merged}
            redacted = {
                k: ("***" if k in {"api_key", "apikey", "apiKey"} else v)
                for k, v in cfg.items()
            }
            logger.info("[Lotus] Loaded config for %s: %s",
                        plugin_name, redacted)
        else:
            logger.warning("[Lotus] No config found for %s", plugin_name)
    except Exception as e:
        logger.warning(
            "[Lotus] Failed to load config for %s: %s", plugin_name, e)

    return merged


def lotus_dispatch_nav(*, route: str) -> Dict[str, Any]:
    if not route or not isinstance(route, str):
        raise ValueError("nav requires route as a string")
    return {
        "ok": True,
        "kind": "nav",
        "navigate_to": route,
        "message": "navigate",
    }


def lotus_dispatch_action(
    *,
    result_id: str,
    plugin_name: str,
    action_name: str,
    inputs: Dict[str, Any],
    session: Session,
    parent_execution_id: str | None = None,
    capability: Any | None = None,
    auth: Any | None = None,
    caller_client_id: str | None = None,
    precheck_only: bool = False,
) -> Dict[str, Any]:
    if not plugin_name or not action_name:
        raise ValueError(
            "action dispatch requires plugin_name and action_name")

    plugin = get_plugin_instance(plugin_name)
    if not plugin:
        raise ValueError(f"Plugin not found: {plugin_name}")

    merged_inputs = lotus_prepare_inputs(
        plugin_name=plugin_name,
        inputs=inputs,
        session=session,
    )
    if not isinstance(merged_inputs, dict):
        merged_inputs = inputs or {}

    auth_payload = _build_dispatch_auth_payload(auth, caller_client_id)
    if auth_payload:
        merged_inputs["__embeddr_auth"] = auth_payload

    pre_result = evaluate_pre_dispatch_middlewares(
        capability=capability,
        result_id=result_id,
        plugin_name=str(plugin_name),
        action_name=str(action_name),
        inputs=merged_inputs,
        session=session,
        auth_payload=auth_payload,
    )

    logger.info(
        "[LotusMiddleware] pre_result result_id=%s plugin=%s action=%s status=%s middleware=%s message=%s",
        result_id,
        plugin_name,
        action_name,
        pre_result.get("status"),
        pre_result.get("middleware_cap_id"),
        pre_result.get("message"),
    )

    merged_inputs = dict(pre_result.get("inputs") or merged_inputs)
    if pre_result.get("status") == "deny":
        return {
            "ok": False,
            "kind": "action",
            "status": "denied",
            "message": pre_result.get("message") or "Action denied by middleware",
            "error": pre_result.get("message") or "Action denied by middleware",
            "middleware_cap_id": pre_result.get("middleware_cap_id"),
        }

    post_payload = build_post_middleware_payload(
        capability=capability,
        result_id=result_id,
        plugin_name=str(plugin_name),
        action_name=str(action_name),
    )
    if post_payload:
        merged_inputs["__lotus_middleware"] = post_payload

    # Infer resource_class from capability exec config, default to "cpu"
    resource_class = "cpu"
    if capability and hasattr(capability, "action") and capability.action:
        exec_config = getattr(capability.action, "exec", None) or {}
        resource_class = exec_config.get("resource_class", "cpu")

    # Validate resource_class
    if resource_class not in ("cpu", "gpu", "io", "network"):
        logger.warning(
            "[Lotus] Invalid resource_class '%s' for %s.%s, defaulting to 'cpu'",
            resource_class, plugin_name, action_name
        )
        resource_class = "cpu"

    if pre_result.get("status") == "defer":
        raw_wait_payload = pre_result.get("wait_payload") or {}
        waiting_execution = _create_waiting_execution(
            session=session,
            plugin_name=str(plugin_name),
            action_name=str(action_name),
            resource_class=resource_class,
            inputs=merged_inputs,
            message=pre_result.get("message") or "Deferred by middleware",
            wait_payload=raw_wait_payload,
            auth=auth,
            caller_client_id=caller_client_id,
        )
        wait_payload = _materialize_wait_payload(
            raw_wait_payload,
            execution_id=str(waiting_execution.id),
        )
        return {
            "ok": True,
            "kind": "action",
            "execution_id": str(waiting_execution.id),
            "status": "waiting",
            "message": waiting_execution.message,
            "middleware_cap_id": pre_result.get("middleware_cap_id"),
            "wait_payload": wait_payload,
            "renderable": wait_payload.get("renderable") if isinstance(wait_payload, dict) else None,
        }

    if precheck_only:
        return {
            "ok": True,
            "kind": "action",
            "status": "allow",
            "inputs": merged_inputs,
        }

    parent_uuid = None
    if parent_execution_id:
        try:
            parent_uuid = UUID(str(parent_execution_id))
        except Exception:
            logger.warning("Invalid parent_execution_id: %s",
                           parent_execution_id)

    logger.warning(
        "[Lotus] Dispatch action plugin=%s action=%s resource=%s inputs=%s merged=%s parent=%s",
        plugin_name,
        action_name,
        resource_class,
        _redact(inputs or {}),
        _redact(merged_inputs or {}),
        parent_execution_id,
    )

    execution = ExecutionSpine.submit_job(
        job_type=str(action_name),
        inputs=merged_inputs,
        plugin_name=str(plugin_name),
        resource_class=resource_class,
        priority=10,
        parent_execution_id=parent_uuid,
        trigger="lotus",
        session=session,
    )

    execution.operator_id = getattr(auth, "operator_id", None)
    execution.api_key_id = (
        str(getattr(auth, "api_key_id", None))
        if getattr(auth, "api_key_id", None)
        else None
    )
    if caller_client_id:
        tags = dict(execution.tags or {})
        tags["target_client_id"] = str(caller_client_id)
        execution.tags = tags
    session.add(execution)
    session.commit()
    session.refresh(execution)

    logger.warning(
        "[Lotus] Queued action result_id=%s plugin=%s action=%s execution_id=%s",
        result_id,
        plugin_name,
        action_name,
        execution.id,
    )

    return {
        "ok": True,
        "kind": "action",
        "execution_id": str(execution.id),
        "status": execution.status or "pending",
        "message": "queued",
        "parent_execution_id": parent_execution_id,
    }


def _create_waiting_execution(
    *,
    session: Session,
    plugin_name: str,
    action_name: str,
    resource_class: str,
    inputs: Dict[str, Any],
    message: str,
    wait_payload: Dict[str, Any],
    auth: Any | None,
    caller_client_id: str | None,
) -> ArtifactExecution:
    execution = ArtifactExecution(
        type=str(action_name),
        plugin_name=str(plugin_name),
        inputs=dict(inputs or {}),
        resource_class=resource_class,
        priority=10,
        trigger="lotus",
        status="waiting",
        message=message,
        operator_id=getattr(auth, "operator_id", None),
        api_key_id=(
            str(getattr(auth, "api_key_id", None))
            if getattr(auth, "api_key_id", None)
            else None
        ),
        tags={
            "target_client_id": str(caller_client_id)
        } if caller_client_id else {},
    )

    session.add(execution)
    session.commit()
    session.refresh(execution)

    session.add(ArtifactExecutionEvent(
        execution_id=execution.id,
        event_type="execution.waiting",
        message=message,
        payload={
            "wait_payload": dict(wait_payload or {}),
        },
    ))
    session.commit()

    _EVENT_BUS.publish(EmbeddrEvent(
        event_type="execution.waiting",
        source="lotus",
        payload={
            "id": str(execution.id),
            "status": "waiting",
            "message": execution.message,
            "payload": dict(wait_payload or {}),
        },
    ))

    logger.info(
        "[LotusMiddleware] execution_waiting id=%s plugin=%s action=%s message=%s payload=%s",
        execution.id,
        plugin_name,
        action_name,
        execution.message,
        dict(wait_payload or {}),
    )

    return execution
