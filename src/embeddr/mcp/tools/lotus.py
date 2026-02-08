# embeddr/mcp/tools/lotus.py
from __future__ import annotations

import importlib
import logging
import os
import re
import sys
from typing import Any, Dict, Optional, Type, Union, get_args, get_origin

from pydantic import BaseModel
from sqlmodel import Session

from embeddr.api.v2.lotus_service import lotus_dispatch_action, lotus_prepare_inputs
from embeddr.core.event_bus import _EVENT_BUS
from embeddr.core.plugin_loader import (
    _PLUGIN_CAPABILITY_REGISTRY,
    get_all_plugin_instances,
    get_lotus_registry,
)
from embeddr.db.session import get_engine
from embeddr_core.models.lotus import LotusKind
from embeddr_core.plugin_interface import PluginContext
from embeddr_core.services.config_service import resolve_plugin_config_for_plugin
from embeddr.core.plugin_context_helpers import LotusContext
from embeddr_core.services.resource_manager import resource_manager

logger = logging.getLogger("embeddr.mcp.tools.lotus")


def _should_trace() -> bool:
    return os.environ.get("EMBEDDR_MCP_TRACE") == "1"


_IDENT_RE = re.compile(r"[^0-9a-zA-Z_]+")


def _safe_ident(s: str) -> str:
    s2 = _IDENT_RE.sub("_", s)
    if s2 and s2[0].isdigit():
        s2 = "_" + s2
    return s2 or "tool"


def _safe_plugin_module(plugin_name: str) -> str:
    return plugin_name.replace("-", "_").replace(".", "_")


def _is_optional(t: Any) -> bool:
    origin = get_origin(t)
    if origin is Union:
        return type(None) in get_args(t)
    return False


def _to_optional(t: Any) -> Any:
    if t is Any:
        return Optional[Any]
    if _is_optional(t):
        return t
    return Optional[t]


def _import_model(model_path: str) -> Type[BaseModel]:
    """
    Import 'pkg.module:ClassName' into a Pydantic model class.
    """
    if ":" not in model_path:
        raise ValueError(
            f"Invalid model path (expected module:Class): {model_path}")
    mod_name, cls_name = model_path.split(":", 1)
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_name, None)
    if cls is None:
        raise ValueError(f"Model class not found: {model_path}")
    if not issubclass(cls, BaseModel):
        raise ValueError(f"Model is not a pydantic BaseModel: {model_path}")
    return cls


def _import_model_with_fallbacks(
    model_path: str,
    *,
    plugin_name: Optional[str] = None,
    plugin_module: Optional[str] = None,
) -> Optional[Type[BaseModel]]:
    """
    Try:
      1) model_path as provided
      2) if plugin_module provided: {plugin_module}.models:Class
      3) if plugin_name provided: rewrite embeddr_plugins.<X> -> embeddr_plugins.<safe>
    """
    try:
        return _import_model(model_path)
    except Exception as e1:
        # 2) plugin_module.models:ClassName
        if plugin_module and ":" in model_path:
            _, cls_name = model_path.split(":", 1)
            try:
                return _import_model(f"{plugin_module}.models:{cls_name}")
            except Exception:
                try:
                    return _import_model(f"{plugin_module}.plugin:{cls_name}")
                except Exception:
                    pass

        # 3) safe-name fallbacks
        if plugin_name and ":" in model_path:
            safe = _safe_plugin_module(plugin_name)
            before, cls_name = model_path.split(":", 1)

            try_paths = []
            if before.startswith("embeddr_plugins."):
                parts = before.split(".")
                if len(parts) >= 2:
                    parts[1] = safe
                    try_paths.append(".".join(parts) + ":" + cls_name)

            try_paths.append(f"embeddr_plugins.{safe}:{cls_name}")
            try_paths.append(f"embeddr_plugins.{safe}.models:{cls_name}")

            for p in try_paths:
                try:
                    return _import_model(p)
                except Exception:
                    pass

        candidates = [k for k in sys.modules.keys(
        ) if k.startswith("embeddr_plugins.")]
        logger.warning(
            "Model import failed for %s. plugin=%s plugin_module=%s. Known embeddr_plugins modules (sample): %s",
            model_path,
            plugin_name,
            plugin_module,
            candidates[:30],
        )
        logger.warning("Original error: %s", e1)
        return None


def _schema_has_defs_ref(schema: Dict[str, Any]) -> bool:
    if not isinstance(schema, dict):
        return False
    if schema.get("$ref", "").startswith("#/$defs/"):
        return True
    for key in ("properties", "items", "anyOf", "oneOf", "allOf"):
        value = schema.get(key)
        if isinstance(value, dict):
            if _schema_has_defs_ref(value):
                return True
        elif isinstance(value, list):
            for item in value:
                if _schema_has_defs_ref(item):
                    return True
    return False


def _is_defs_ref_error(exc: Exception) -> bool:
    return "#/$defs/" in str(exc)


def _resolve_plugin(plugin_name: str):
    for p in get_all_plugin_instances():
        if getattr(p, "name", None) == plugin_name:
            return p
    return None


def _run_sync(
    *,
    tool_name: str,
    plugin_name: str,
    action_name: str,
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    plugin = _resolve_plugin(plugin_name)
    if not plugin:
        raise RuntimeError(f"Plugin not found: {plugin_name}")

    merged_inputs = inputs
    try:
        with Session(get_engine()) as session:
            merged_inputs = lotus_prepare_inputs(
                plugin_name=plugin_name,
                inputs=inputs,
                session=session,
            )
    except Exception as exc:
        logger.warning("MCP lotus_prepare_inputs failed: %s", exc)

    ctx = PluginContext(
        bus=_EVENT_BUS,
        capability_registry=_PLUGIN_CAPABILITY_REGISTRY,
        resources=resource_manager,
        config=resolve_plugin_config_for_plugin(plugin_name=plugin_name),
        lotus=LotusContext(),
    )

    # Sync execution returns the plugin output directly (what MCP inspector wants)
    return plugin.execute(action_name, None, merged_inputs, context=ctx)


def _run_dispatch(
    *,
    tool_name: str,
    plugin_name: str,
    action_name: str,
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Execute via lotus_dispatch_action so MCP == Lotus UI behaviour.
    NOTE: lotus_dispatch_action itself returns a queued response.
    """
    with Session(get_engine()) as session:
        out = lotus_dispatch_action(
            result_id=tool_name,
            plugin_name=plugin_name,
            action_name=action_name,
            inputs=inputs,
            session=session,
            background_tasks=None,
        )

    return out


def _run_from_cap(*, cap, tool_name: str, plugin_name: str, action_name: str, inputs: Dict[str, Any]):
    data = cap.data or {}
    exec_cfg = data.get("exec") or {}
    mode = exec_cfg.get("mode", "async")
    if _should_trace():
        logger.info(
            "[MCPTrace] invoke tool=%s plugin=%s action=%s mode=%s inputs=%s",
            tool_name,
            plugin_name,
            action_name,
            mode,
            inputs,
        )

    if mode == "sync":
        out = _run_sync(
            tool_name=tool_name,
            plugin_name=plugin_name,
            action_name=action_name,
            inputs=inputs,
        )
    else:
        out = _run_dispatch(
            tool_name=tool_name,
            plugin_name=plugin_name,
            action_name=action_name,
            inputs=inputs,
        )
    if _should_trace():
        logger.info(
            "[MCPTrace] dispatched tool=%s execution_id=%s status=%s",
            tool_name,
            out.get("execution_id"),
            out.get("status"),
        )
    return out


def _build_flat_tool_fn(
    *,
    cap,
    tool_name: str,
    Model: Type[BaseModel],
    plugin_name: str,
    action_name: str,
) -> Any:
    """
    Build a function with signature equal to Model fields (top-level args),
    so MCP inspector shows inputs nicely and {} calls work when defaults exist.

    IMPORTANT: This must respect exec.mode (sync vs async) -> call _run_from_cap.
    """
    fields = Model.model_fields  # pydantic v2

    # If any field name is not a valid identifier, fallback to dict style
    for fname in fields.keys():
        if not fname.isidentifier():
            logger.warning(
                "Tool %s: field '%s' is not a valid identifier; falling back to dict input.",
                tool_name,
                fname,
            )

            def handler(
                input: Optional[Dict[str, Any]] = None,
                _cap=cap,
                _tool_name=tool_name,
                _plugin_name=plugin_name,
                _action_name=action_name,
            ) -> Dict[str, Any]:
                obj = Model.model_validate(input or {})
                return _run_from_cap(
                    cap=_cap,
                    tool_name=_tool_name,
                    plugin_name=_plugin_name,
                    action_name=_action_name,
                    inputs=obj.model_dump(),
                )

            handler.__name__ = "lotus_" + _safe_ident(tool_name)
            handler.__annotations__ = {
                "input": Optional[Dict[str, Any]], "return": Dict[str, Any]}
            return handler

    # Build function source with defaults as None for optionals.
    # Pydantic will apply actual defaults when we validate.
    params_src = []
    annotations: Dict[str, Any] = {"return": Dict[str, Any]}

    required = [(fname, f) for fname, f in fields.items() if f.is_required()]
    optional = [(fname, f)
                for fname, f in fields.items() if not f.is_required()]

    for fname, f in required + optional:
        ann = f.annotation or Any
        if f.is_required():
            params_src.append(fname)
            annotations[fname] = ann
        else:
            params_src.append(f"{fname}=None")
            annotations[fname] = _to_optional(ann)

    fn_name = "lotus_" + _safe_ident(tool_name)

    # NOTE: This calls _run_from_cap (not _run_dispatch), so sync tools return actual results.
    src = f"def {fn_name}({', '.join(params_src)}):\n"
    src += "    data = locals().copy()\n"
    src += "    obj = Model.model_validate(data)\n"
    src += "    return _run_from_cap(cap=CAP, tool_name=TOOL, plugin_name=PLUGIN, action_name=ACTION, inputs=obj.model_dump())\n"

    ns: Dict[str, Any] = {
        "Model": Model,
        "_run_from_cap": _run_from_cap,
        "CAP": cap,
        "TOOL": tool_name,
        "PLUGIN": plugin_name,
        "ACTION": action_name,
    }
    exec(src, ns, ns)
    fn = ns[fn_name]
    fn.__annotations__ = annotations
    return fn


def register_lotus_tools(mcp):
    reg = get_lotus_registry()
    caps = reg.list()

    logger.info(
        "Registering Lotus-derived MCP tools from %d capabilities", len(caps))

    count = 0
    for cap in caps:
        if cap.kind not in {LotusKind.action, LotusKind.feature}:
            continue

        data: Dict[str, Any] = cap.data or {}
        expose = data.get("expose", {}) or {}
        if not expose.get("mcp", False):
            continue

        plugin_name = str(data.get("plugin") or cap.plugin or "")
        action_name = str(data.get("action") or "")
        if not plugin_name or not action_name:
            logger.warning(
                "Lotus MCP skipped %s (missing data.plugin/data.action)", cap.id)
            continue

        tool_name = cap.id
        desc = cap.description or cap.title or tool_name

        input_block = data.get("input") or {}
        model_path = input_block.get("model")
        # injected by loader (recommended)
        plugin_module = data.get("plugin_module")

        Model: Optional[Type[BaseModel]] = None
        if isinstance(model_path, str) and model_path.strip():
            Model = _import_model_with_fallbacks(
                model_path,
                plugin_name=plugin_name,
                plugin_module=plugin_module,
            )

        # DEBUG: log schema being used
        if Model is not None:
            try:
                schema = Model.model_json_schema()
                if _schema_has_defs_ref(schema):
                    logger.info(
                        "MCP tool %s uses $defs refs; registering as raw dict tool.",
                        tool_name,
                    )
                    Model = None
                else:
                    logger.info(
                        "MCP tool %s input schema keys: %s",
                        tool_name,
                        list(schema.get("properties", {}).keys()),
                    )
                    logger.debug("MCP tool %s full schema: %s",
                                 tool_name, schema)
            except Exception as e:
                logger.warning(
                    "MCP tool %s schema dump failed: %s", tool_name, e)

        if Model is not None:
            handler_fn = _build_flat_tool_fn(
                cap=cap,
                tool_name=tool_name,
                Model=Model,
                plugin_name=plugin_name,
                action_name=action_name,
            )
        else:
            # fallback: accept raw dict
            def handler(
                input=None,
                _cap=cap,
                _tool_name=tool_name,
                _plugin_name=plugin_name,
                _action_name=action_name,
            ) -> Dict[str, Any]:
                return _run_from_cap(
                    cap=_cap,
                    tool_name=_tool_name,
                    plugin_name=_plugin_name,
                    action_name=_action_name,
                    inputs=input or {},
                )

            handler.__name__ = "lotus_" + _safe_ident(tool_name)
            handler.__annotations__ = {"return": Dict[str, Any]}
            handler_fn = handler

        try:
            mcp.tool(name=tool_name, description=desc)(handler_fn)
            logger.info("Registered Lotus MCP tool: %s", tool_name)
            count += 1
        except Exception as exc:
            if _is_defs_ref_error(exc):
                logger.info(
                    "Skipping MCP tool %s due to $defs schema refs.",
                    tool_name,
                )
                continue

            logger.warning(
                "MCP tool %s registration failed: %s. Falling back to dict input.",
                tool_name,
                exc,
            )

            def fallback_handler(
                input=None,
                _cap=cap,
                _tool_name=tool_name,
                _plugin_name=plugin_name,
                _action_name=action_name,
            ) -> Dict[str, Any]:
                return _run_from_cap(
                    cap=_cap,
                    tool_name=_tool_name,
                    plugin_name=_plugin_name,
                    action_name=_action_name,
                    inputs=input or {},
                )

            fallback_handler.__name__ = "lotus_" + \
                _safe_ident(tool_name) + "_raw"
            fallback_handler.__annotations__ = {"return": Dict[str, Any]}

            try:
                mcp.tool(name=tool_name, description=desc)(fallback_handler)
                logger.info(
                    "Registered Lotus MCP tool (fallback): %s", tool_name)
                count += 1
            except Exception as fallback_exc:
                if _is_defs_ref_error(fallback_exc):
                    logger.info(
                        "Skipping MCP tool %s fallback due to $defs schema refs.",
                        tool_name,
                    )
                else:
                    logger.warning(
                        "MCP tool %s fallback registration failed: %s. Skipping tool.",
                        tool_name,
                        fallback_exc,
                    )
                continue

    logger.info("Registered %d Lotus-derived MCP tools", count)
