import logging
import os
from typing import Any, Dict

from embeddr_core.services.config_service import resolve_plugin_config
from embeddr_core.models.lotus import LotusKind
from fastapi import BackgroundTasks
from sqlmodel import Session

from embeddr.core.execution_spine import ExecutionSpine
from embeddr.core.plugin_loader import get_all_plugin_instances, get_lotus_registry

logger = logging.getLogger("embeddr.api.lotus")


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


def _resolve_plugin(plugin_name: str):
    # local import to avoid circulars
    from embeddr.core.plugin_loader import get_all_plugin_instances

    for p in get_all_plugin_instances():
        if p.name == plugin_name:
            return p
    return None


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
    background_tasks: BackgroundTasks | None = None,
    parent_execution_id: str | None = None,
) -> Dict[str, Any]:
    if not plugin_name or not action_name:
        raise ValueError(
            "action dispatch requires plugin_name and action_name")

    plugin = _resolve_plugin(plugin_name)
    if not plugin:
        raise ValueError(f"Plugin not found: {plugin_name}")

    merged_inputs = lotus_prepare_inputs(
        plugin_name=plugin_name,
        inputs=inputs,
        session=session,
    )
    if not isinstance(merged_inputs, dict):
        merged_inputs = inputs or {}

    parent_uuid = None
    if parent_execution_id:
        try:
            from uuid import UUID

            parent_uuid = UUID(str(parent_execution_id))
        except Exception:
            logger.warning("Invalid parent_execution_id: %s",
                           parent_execution_id)

    logger.warning(
        "[Lotus] Dispatch action plugin=%s action=%s inputs=%s merged=%s parent=%s",
        plugin_name,
        action_name,
        _redact(inputs or {}),
        _redact(merged_inputs or {}),
        parent_execution_id,
    )

    execution = ExecutionSpine.submit_job(
        job_type=str(action_name),
        inputs=merged_inputs,
        plugin_name=str(plugin_name),
        resource_class="cpu",
        priority=10,
        parent_execution_id=parent_uuid,
        trigger="lotus",
    )

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
