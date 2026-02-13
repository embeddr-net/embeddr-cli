"""Register Lotus-backed tools on an MCP server.

Each Lotus capability with ``expose.mcp == True`` is registered as an MCP
tool.  At call time the tool dispatches through the standard Lotus pipeline
(``lotus_dispatch_action`` for async, ``plugin.execute`` for sync) so
security and audit rules apply uniformly.

The caller (MCP transport plugin) passes the ``FastMCP`` instance and we
do the rest.  Auth filtering happens at invocation time via the auth
context embedded in the MCP session.
"""
from __future__ import annotations

import inspect
import json
import logging
from typing import Any, Callable, Dict, Optional

from embeddr.core.plugin_loader import get_lotus_registry, get_plugin_instance
from embeddr_core.models.lotus import LotusCapability, LotusKind

logger = logging.getLogger("embeddr.mcp.tools.lotus")

# Maps JSON Schema types to Python annotations for MCP tool signatures.
_JSON_TYPE_MAP: Dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


# ---------------------------------------------------------------------------
# Reuse the typed-field accessors from the LLM tool manager so we don't
# duplicate the logic.  These are module-level helpers.
# ---------------------------------------------------------------------------

def _get_expose(cap: LotusCapability) -> Dict[str, Any]:
    act = cap.action
    if act and act.expose:
        return act.expose
    data = cap.data or {}
    return data.get("expose") or {} if isinstance(data, dict) else {}


def _get_action_name(cap: LotusCapability) -> str:
    act = cap.action
    if act and act.action:
        return act.action
    data = cap.data or {}
    return str(data.get("action") or "") if isinstance(data, dict) else ""


def _get_plugin_name(cap: LotusCapability) -> str:
    data = cap.data or {}
    from_data = str(data.get("plugin") or "") if isinstance(data, dict) else ""
    return from_data or str(cap.plugin or "")


def _get_exec_cfg(cap: LotusCapability) -> Dict[str, Any]:
    act = cap.action
    if act and act.exec:
        return act.exec
    data = cap.data or {}
    return (data.get("exec") or {}) if isinstance(data, dict) else {}


def _get_input_block(cap: LotusCapability) -> Dict[str, Any]:
    act = cap.action
    if act and act.input and isinstance(act.input, dict):
        return act.input
    data = cap.data or {}
    return (data.get("input") or {}) if isinstance(data, dict) else {}


def _get_json_schema(cap: LotusCapability) -> Dict[str, Any]:
    input_block = _get_input_block(cap)
    schema = input_block.get("schema")
    if isinstance(schema, dict) and schema.get("type") == "object":
        return schema
    return {"type": "object", "properties": {}, "required": []}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_lotus_tools(mcp_server: Any) -> None:
    """Walk the Lotus registry and register every ``expose.mcp=True``
    capability as a FastMCP tool.

    The generated tool function dispatches via the standard Lotus execute
    path so that all auth, audit, and event bus plumbing is reused.
    """
    reg = get_lotus_registry()
    caps = (
        reg.list(kind=LotusKind.action)
        + reg.list(kind=LotusKind.feature)
        + reg.list(kind=LotusKind.resolver)
    )

    count = 0
    for cap in caps:
        expose = _get_expose(cap)
        if not isinstance(expose, dict) or not expose.get("mcp", False):
            continue

        plugin_name = _get_plugin_name(cap)
        action_name = _get_action_name(cap)
        if not plugin_name or not action_name:
            continue

        tool_id = cap.id
        description = cap.description or cap.title or tool_id
        exec_cfg = _get_exec_cfg(cap)
        mode = exec_cfg.get("mode", "async")

        # Build the callable for FastMCP
        _register_one_tool(
            mcp_server,
            tool_id=tool_id,
            description=description,
            plugin_name=plugin_name,
            action_name=action_name,
            mode=mode,
            cap=cap,
        )
        count += 1

    logger.info("Registered %d Lotus tools on MCP server.", count)


def _register_one_tool(
    mcp_server: Any,
    *,
    tool_id: str,
    description: str,
    plugin_name: str,
    action_name: str,
    mode: str,
    cap: LotusCapability,
) -> None:
    """Register a single capability as an MCP tool.

    FastMCP requires explicit function signatures (no ``**kwargs``).  We build
    a proper ``inspect.Signature`` from the capability's JSON schema so
    FastMCP can introspect the tool's parameters.
    """

    schema = _get_json_schema(cap)
    properties = schema.get("properties", {})
    required_set = set(schema.get("required", []))

    # Build the handler — uses **kwargs internally but we override __signature__
    # so FastMCP sees named parameters instead.
    async def _tool_handler(**kwargs: Any) -> str:  # noqa: E501
        return await _dispatch_tool(
            tool_id=tool_id,
            plugin_name=plugin_name,
            action_name=action_name,
            mode=mode,
            cap=cap,
            inputs=kwargs,
        )

    _tool_handler.__name__ = tool_id.replace(".", "_").replace("-", "_")
    _tool_handler.__doc__ = description

    # Override __signature__ with explicit parameters derived from the
    # capability's input schema.  inspect.signature() — which FastMCP uses —
    # respects __signature__ so the VAR_KEYWORD check passes cleanly.
    params: list[inspect.Parameter] = []
    for name, prop in properties.items():
        json_type = prop.get("type", "string")
        annotation = _JSON_TYPE_MAP.get(json_type, str)
        if name not in required_set:
            annotation = Optional[annotation]
        default = (
            inspect.Parameter.empty
            if name in required_set
            else prop.get("default")
        )
        params.append(
            inspect.Parameter(
                name,
                kind=inspect.Parameter.POSITIONAL_OR_KEYWORD,
                default=default,
                annotation=annotation,
            )
        )
    _tool_handler.__signature__ = inspect.Signature(
        params, return_annotation=str
    )

    try:
        mcp_server.tool(name=tool_id, description=description)(_tool_handler)
    except Exception:
        logger.warning("Failed to register MCP tool %s",
                       tool_id, exc_info=True)


async def _dispatch_tool(
    *,
    tool_id: str,
    plugin_name: str,
    action_name: str,
    mode: str,
    cap: LotusCapability,
    inputs: Dict[str, Any],
) -> str:
    """Execute a Lotus action from an MCP tool call."""
    logger.info(
        "MCP tool dispatch tool=%s plugin=%s action=%s mode=%s",
        tool_id, plugin_name, action_name, mode,
    )

    try:
        if mode == "sync":
            plugin = get_plugin_instance(plugin_name)
            if not plugin:
                return json.dumps({"error": f"Plugin not found: {plugin_name}"})

            from embeddr.core.event_bus import _EVENT_BUS
            from embeddr.core.plugin_loader import _PLUGIN_CAPABILITY_REGISTRY
            from embeddr_core.plugin_interface import PluginContext
            from embeddr_core.services.resource_manager import resource_manager

            ctx = PluginContext(
                bus=_EVENT_BUS,
                capability_registry=_PLUGIN_CAPABILITY_REGISTRY,
                resources=resource_manager,
            )
            result = plugin.execute(action_name, None, inputs, context=ctx)
            return json.dumps(result, default=str)
        else:
            # Async dispatch via execution spine
            from embeddr.api.v1.lotus_service import lotus_dispatch_action
            from embeddr.db.session import get_engine
            from sqlmodel import Session

            with Session(get_engine()) as session:
                result = lotus_dispatch_action(
                    result_id=tool_id,
                    plugin_name=plugin_name,
                    action_name=action_name,
                    inputs=inputs,
                    session=session,
                    capability=cap,
                )
            return json.dumps(result, default=str)
    except Exception as exc:
        logger.exception("MCP tool execution failed: %s", tool_id)
        return json.dumps({"error": str(exc)})
