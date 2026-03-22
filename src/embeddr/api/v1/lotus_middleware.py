from __future__ import annotations

import logging
import os
from typing import Any, Dict, List, Optional

from sqlmodel import Session

from embeddr_core.models.lotus import LotusCapability
from embeddr_core.services.lotus_client import invoke_action, invoke_action_for_plugin

logger = logging.getLogger("embeddr.api.lotus.middleware")


def _trace_enabled() -> bool:
    return os.environ.get("EMBEDDR_LOTUS_MIDDLEWARE_TRACE") == "1"


def _middleware_block(capability: Optional[LotusCapability]) -> Dict[str, Any]:
    if capability is None:
        return {}

    exec_cfg = {}
    if capability.action and isinstance(capability.action.exec, dict):
        exec_cfg = capability.action.exec
    elif isinstance(capability.data, dict):
        exec_cfg = capability.data.get("exec") or {}

    if not isinstance(exec_cfg, dict):
        return {}

    middleware = exec_cfg.get("middleware")
    if isinstance(middleware, dict):
        return middleware
    return {}


def _normalize_specs(raw_specs: Any) -> List[Dict[str, Any]]:
    if not raw_specs:
        return []

    if isinstance(raw_specs, (str, dict)):
        raw_specs = [raw_specs]

    if not isinstance(raw_specs, list):
        return []

    specs: List[Dict[str, Any]] = []
    for item in raw_specs:
        if isinstance(item, str):
            cap_id = item.strip()
            if cap_id:
                specs.append({"cap_id": cap_id, "required": True, "args": {}})
            continue

        if not isinstance(item, dict):
            continue

        raw_cap_id = (
            item.get("cap_id")
            or item.get("capability")
            or item.get("id")
        )
        if not isinstance(raw_cap_id, str):
            continue
        cap_id = raw_cap_id.strip()
        if not cap_id:
            continue

        args = item.get("args") if isinstance(item.get("args"), dict) else {}
        specs.append(
            {
                "cap_id": cap_id.strip(),
                "required": bool(item.get("required", True)),
                "args": args,
                "name": item.get("name"),
            }
        )

    return specs


def _normalize_decision(result: Any) -> Dict[str, Any]:
    if not isinstance(result, dict):
        return {
            "decision": "allow",
            "message": None,
            "wait_payload": {},
            "inputs_patch": {},
            "raw": result,
        }

    decision = str(result.get("decision") or "").strip().lower()
    if not decision:
        decision = "deny" if result.get("ok") is False else "allow"
    if decision not in {"allow", "deny", "defer"}:
        decision = "allow"

    wait_payload = result.get("wait_payload")
    if not isinstance(wait_payload, dict):
        wait_payload = {}

    inputs_patch = result.get("inputs_patch")
    if not isinstance(inputs_patch, dict):
        inputs_patch = {}

    return {
        "decision": decision,
        "message": result.get("message") or result.get("reason"),
        "wait_payload": wait_payload,
        "inputs_patch": inputs_patch,
        "raw": result,
    }


def evaluate_pre_dispatch_middlewares(
    *,
    capability: Optional[LotusCapability],
    result_id: str,
    plugin_name: str,
    action_name: str,
    inputs: Dict[str, Any],
    session: Session,
    auth_payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    middleware = _middleware_block(capability)
    specs = _normalize_specs(middleware.get("pre"))
    if _trace_enabled():
        logger.info(
            "[MiddlewareTrace] pre.start cap=%s plugin=%s action=%s middleware_count=%s",
            result_id,
            plugin_name,
            action_name,
            len(specs),
        )
    if not specs:
        return {
            "status": "allow",
            "inputs": dict(inputs or {}),
            "message": None,
            "middleware_cap_id": None,
            "wait_payload": {},
        }

    merged_inputs = dict(inputs or {})
    for spec in specs:
        cap_id = spec["cap_id"]
        payload = {
            "phase": "pre",
            "target": {
                "cap_id": result_id,
                "plugin_name": plugin_name,
                "action_name": action_name,
            },
            "inputs": merged_inputs,
            "auth": auth_payload or {},
            "args": spec.get("args") or {},
        }

        try:
            result = invoke_action(
                cap_id=cap_id,
                inputs=payload,
                session=session,
                context=None,
            )
        except Exception as exc:
            if _trace_enabled():
                logger.info(
                    "[MiddlewareTrace] pre.error middleware=%s required=%s error=%s",
                    cap_id,
                    spec.get("required", True),
                    exc,
                )
            if spec.get("required", True):
                return {
                    "status": "deny",
                    "inputs": merged_inputs,
                    "message": f"middleware_error:{cap_id}: {exc}",
                    "middleware_cap_id": cap_id,
                    "wait_payload": {},
                }
            logger.warning("Optional pre middleware failed cap_id=%s error=%s", cap_id, exc)
            continue

        decision = _normalize_decision(result)
        if _trace_enabled():
            logger.info(
                "[MiddlewareTrace] pre.result middleware=%s decision=%s message=%s",
                cap_id,
                decision["decision"],
                decision["message"],
            )
        patch = decision["inputs_patch"]
        if patch:
            merged_inputs.update(patch)

        if decision["decision"] == "allow":
            continue

        if decision["decision"] == "deny":
            return {
                "status": "deny",
                "inputs": merged_inputs,
                "message": decision["message"] or f"Denied by middleware {cap_id}",
                "middleware_cap_id": cap_id,
                "wait_payload": {},
            }

        return {
            "status": "defer",
            "inputs": merged_inputs,
            "message": decision["message"] or f"Deferred by middleware {cap_id}",
            "middleware_cap_id": cap_id,
            "wait_payload": decision["wait_payload"],
        }

    return {
        "status": "allow",
        "inputs": merged_inputs,
        "message": None,
        "middleware_cap_id": None,
        "wait_payload": {},
    }


def build_post_middleware_payload(
    *,
    capability: Optional[LotusCapability],
    result_id: str,
    plugin_name: str,
    action_name: str,
) -> Optional[Dict[str, Any]]:
    middleware = _middleware_block(capability)
    post_success = _normalize_specs(middleware.get("post_success"))
    post_error = _normalize_specs(middleware.get("post_error"))

    if not post_success and not post_error:
        return None

    return {
        "target": {
            "cap_id": result_id,
            "plugin_name": plugin_name,
            "action_name": action_name,
        },
        "post_success": post_success,
        "post_error": post_error,
    }


def run_post_execution_middlewares(
    *,
    execution_id: str,
    job_type: str,
    plugin_name: str,
    status: str,
    inputs: Dict[str, Any] | None,
    outputs: Dict[str, Any] | None,
    error: Optional[str],
) -> None:
    all_inputs = dict(inputs or {})
    middleware = all_inputs.get("__lotus_middleware")
    if not isinstance(middleware, dict):
        return

    phase = "post_success" if status == "completed" else "post_error"
    specs = _normalize_specs(middleware.get(phase))
    if _trace_enabled():
        logger.info(
            "[MiddlewareTrace] %s.start execution=%s plugin=%s action=%s middleware_count=%s status=%s",
            phase,
            execution_id,
            plugin_name,
            job_type,
            len(specs),
            status,
        )
    if not specs:
        return

    payload = {
        "phase": phase,
        "execution": {
            "id": execution_id,
            "status": status,
            "job_type": job_type,
            "plugin_name": plugin_name,
        },
        "target": middleware.get("target") or {},
        "inputs": all_inputs,
        "outputs": outputs or {},
        "error": error,
    }

    for spec in specs:
        cap_id = spec["cap_id"]
        middleware_payload = dict(payload)
        middleware_payload["args"] = spec.get("args") or {}
        try:
            invoke_action_for_plugin(
                cap_id=cap_id,
                inputs=middleware_payload,
                context=None,
            )
            if _trace_enabled():
                logger.info(
                    "[MiddlewareTrace] %s.ok middleware=%s execution=%s",
                    phase,
                    cap_id,
                    execution_id,
                )
        except Exception as exc:
            if _trace_enabled():
                logger.info(
                    "[MiddlewareTrace] %s.error middleware=%s required=%s execution=%s error=%s",
                    phase,
                    cap_id,
                    spec.get("required", True),
                    execution_id,
                    exc,
                )
            if spec.get("required", True):
                logger.error(
                    "Required post middleware failed cap_id=%s execution_id=%s status=%s error=%s",
                    cap_id,
                    execution_id,
                    status,
                    exc,
                )
            else:
                logger.warning(
                    "Optional post middleware failed cap_id=%s execution_id=%s status=%s error=%s",
                    cap_id,
                    execution_id,
                    status,
                    exc,
                )
