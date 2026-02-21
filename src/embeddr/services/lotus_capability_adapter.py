from __future__ import annotations

from typing import Any, Dict, Optional

from embeddr_core.models.lotus import LotusCapability, LotusKind


def cap_expose(cap: LotusCapability) -> Dict[str, Any]:
    if cap.action and cap.action.expose:
        return dict(cap.action.expose)
    data = cap.data or {}
    expose = data.get("expose") if isinstance(data, dict) else None
    return expose if isinstance(expose, dict) else {}


def cap_plugin_name(cap: LotusCapability) -> str:
    data = cap.data or {}
    from_data = str(data.get("plugin_name") or data.get("plugin") or "")
    return from_data or str(cap.plugin or "")


def cap_action_name(cap: LotusCapability) -> str:
    if cap.action and cap.action.action:
        return str(cap.action.action)
    data = cap.data or {}
    return str(data.get("action_name") or data.get("action") or "")


def cap_exec_cfg(cap: LotusCapability) -> Dict[str, Any]:
    if cap.action and cap.action.exec:
        return dict(cap.action.exec)
    data = cap.data or {}
    exec_cfg = data.get("exec") if isinstance(data, dict) else None
    return exec_cfg if isinstance(exec_cfg, dict) else {}


def cap_input_block(cap: LotusCapability) -> Dict[str, Any]:
    if cap.action and cap.action.input and isinstance(cap.action.input, dict):
        return dict(cap.action.input)
    data = cap.data or {}
    input_block = data.get("input") if isinstance(data, dict) else None
    return input_block if isinstance(input_block, dict) else {}


def cap_output_block(cap: LotusCapability) -> Dict[str, Any]:
    if cap.action and cap.action.output and isinstance(cap.action.output, dict):
        return dict(cap.action.output)
    data = cap.data or {}
    output_block = data.get("output") if isinstance(data, dict) else None
    return output_block if isinstance(output_block, dict) else {}


def cap_input_schema(cap: LotusCapability) -> Optional[Dict[str, Any]]:
    schema = cap_input_block(cap).get("schema")
    return schema if isinstance(schema, dict) else None


def cap_output_schema(cap: LotusCapability) -> Optional[Dict[str, Any]]:
    schema = cap_output_block(cap).get("schema")
    return schema if isinstance(schema, dict) else None


def cap_input_model_path(cap: LotusCapability) -> Optional[str]:
    model = cap_input_block(cap).get("model")
    if isinstance(model, str) and model.strip():
        return model
    return None


def cap_json_schema(cap: LotusCapability) -> Dict[str, Any]:
    schema = cap_input_schema(cap)
    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema
    return {"type": "object", "properties": {}, "required": []}


def cap_transport_exposed(cap: LotusCapability, transport_key: str) -> bool:
    expose = cap_expose(cap)
    if not isinstance(expose, dict):
        return False
    return bool(expose.get(transport_key, False))


def cap_is_query_like(cap: LotusCapability) -> bool:
    expose = cap_expose(cap)
    if bool(expose.get("graphql_query")):
        return True

    if cap.kind == LotusKind.feature:
        return True

    tags = {str(tag).strip().lower() for tag in (cap.tags or []) if str(tag).strip()}
    if tags.intersection({"search", "query", "read", "lookup", "list", "get"}):
        return True

    action_name = cap_action_name(cap).strip().lower()
    cap_name = str(cap.id or "").strip().lower().split(".")[-1]

    for candidate in (action_name, cap_name):
        if candidate in {"search", "quote", "timeseries", "lookup", "list", "get", "status", "resolve"}:
            return True
        if candidate.startswith("get_") or candidate.startswith("list_") or candidate.startswith("search_"):
            return True

    return False
