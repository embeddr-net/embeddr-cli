import json
import importlib
from typing import Optional, List, Any, Dict
from uuid import uuid4

import typer
import questionary

from embeddr.core.plugin_loader import get_lotus_registry, get_plugin_instance
from embeddr_core.models.lotus import LotusKind
from embeddr_core.plugin_interface import PluginContext

app = typer.Typer(help="Lotus capabilities and invocation")


def _resolve_input_schema(cap) -> Optional[dict[str, Any]]:
    data = cap.data or {}
    input_block = data.get("input") or {}
    schema = input_block.get("schema")
    if isinstance(schema, dict):
        return schema

    model_ref = input_block.get("model")
    if not model_ref or not isinstance(model_ref, str) or ":" not in model_ref:
        return None

    module_path, class_name = model_ref.split(":", 1)
    try:
        module = importlib.import_module(module_path)
        model_cls = getattr(module, class_name, None)
        if model_cls and hasattr(model_cls, "model_json_schema"):
            return model_cls.model_json_schema()
    except Exception:
        return None

    return None


def _maybe_parse_value(raw: str) -> Any:
    if raw is None:
        return None
    val = raw.strip()
    if not val:
        return ""
    if val.startswith("{") or val.startswith("["):
        try:
            return json.loads(val)
        except Exception:
            return raw
    lowered = val.lower()
    if lowered in {"true", "false", "null"}:
        try:
            return json.loads(lowered)
        except Exception:
            return raw
    try:
        if "." in val:
            return float(val)
        return int(val)
    except Exception:
        return raw


def _extract_exposed_inputs(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    if not isinstance(result, dict):
        return []
    if isinstance(result.get("exposed_inputs"), list):
        return result.get("exposed_inputs") or []
    interface = result.get("interface") or {}
    if isinstance(interface.get("exposed_inputs"), list):
        return interface.get("exposed_inputs") or []
    meta = result.get("meta") or {}
    interface = meta.get("interface") or {}
    if isinstance(interface.get("exposed_inputs"), list):
        return interface.get("exposed_inputs") or []
    return []


def _resolve_resolver_config(cap) -> Optional[Dict[str, Any]]:
    data = cap.data or {}
    input_block = data.get("input") or {}
    resolver = input_block.get("resolver")
    if isinstance(resolver, dict):
        return resolver
    return None


def _prompt_required_from_schema(schema: Dict[str, Any], inputs: Dict[str, Any]) -> Dict[str, Any]:
    required = schema.get("required") or []
    if not required:
        return inputs
    properties = schema.get("properties", {})
    for field in required:
        if field in inputs:
            continue
        title = properties.get(field, {}).get("title") or field
        default_val = properties.get(field, {}).get("default")
        answer = questionary.text(
            f"{title} ({field})",
            default=str(default_val) if default_val is not None else "",
        ).ask()
        if answer is None or answer == "":
            typer.echo(f"Missing required input: {field}")
            raise typer.Exit(code=1)
        inputs[field] = _maybe_parse_value(answer)
    return inputs


def _prompt_exposed_inputs(
    exposed_inputs: List[Dict[str, Any]],
    target_field: str,
    inputs: Dict[str, Any],
) -> Dict[str, Any]:
    resolved: dict[str, Any] = {}
    for item in exposed_inputs:
        label = item.get("label") or f"{item.get('node')}_{item.get('port')}"
        desc = item.get("description") or ""
        default_val = item.get("value")
        prompt_label = f"{label}"
        if desc:
            prompt_label += f" — {desc}"
        answer = questionary.text(
            prompt_label,
            default="" if default_val is None else str(default_val),
        ).ask()
        if answer is None:
            continue
        parsed = _maybe_parse_value(answer)
        if parsed == "" and default_val is None:
            continue
        if parsed == "" and default_val is not None:
            parsed = default_val
        resolved[label] = parsed
    if resolved:
        inputs[target_field] = resolved
    return inputs


def _serialize_cap(cap, *, resolve_schema: bool = False):
    data = cap.data or {}
    if resolve_schema:
        schema = _resolve_input_schema(cap)
        if schema:
            data = dict(data)
            input_block = dict(data.get("input") or {})
            input_block["schema"] = schema
            data["input"] = input_block

    return {
        "id": cap.id,
        "kind": cap.kind,
        "title": cap.title,
        "description": cap.description,
        "plugin": cap.plugin,
        "version": cap.version,
        "tags": cap.tags,
        "slot": cap.slot,
        "data": data,
    }


@app.command("list")
def list_caps(
    kind: Optional[LotusKind] = typer.Option(None, help="Filter by kind"),
    plugin: Optional[str] = typer.Option(None, help="Filter by plugin"),
    slot: Optional[str] = typer.Option(None, help="Filter by slot"),
    query: Optional[str] = typer.Option(None, help="Search query"),
    limit: int = typer.Option(50, help="Max results for query"),
    as_json: bool = typer.Option(False, "--json", help="Output JSON"),
):
    registry = get_lotus_registry()
    if query:
        results = registry.query(query, limit=limit)
        caps = [r.capability for r in results]
    else:
        caps = registry.list(kind=kind, slot=slot, plugin=plugin)

    if as_json:
        typer.echo(json.dumps([_serialize_cap(c) for c in caps], indent=2))
        return

    typer.echo("\n🌸 Lotus Capabilities:")
    for cap in caps:
        typer.echo(
            f"- {cap.id} [{cap.kind}] {cap.plugin or 'core'} :: {cap.title}"
        )


@app.command("inspect")
def inspect_cap(
    cap_id: str = typer.Argument(..., help="Capability id"),
    as_json: bool = typer.Option(True, "--json", help="Output JSON"),
    resolve_schema: bool = typer.Option(
        True,
        "--resolve-schema/--no-resolve-schema",
        help="Resolve missing input schema from model reference",
    ),
):
    registry = get_lotus_registry()
    cap = registry.get(cap_id)
    if not cap:
        raise typer.Exit(code=1)

    payload = _serialize_cap(cap, resolve_schema=resolve_schema)
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return

    typer.echo(f"{cap.id} ({cap.kind})")
    typer.echo(f"Title: {cap.title}")
    typer.echo(f"Plugin: {cap.plugin} v{cap.version}")
    if cap.description:
        typer.echo(f"Description: {cap.description}")


@app.command("invoke")
def invoke_cap(
    cap_id: str = typer.Argument(..., help="Capability id"),
    input_json: str = typer.Option("{}", "--input", help="JSON input payload"),
    set_values: List[str] = typer.Option(
        None,
        "--set",
        help="Set input key=value (repeatable)",
    ),
    prompt: bool = typer.Option(
        False,
        "--prompt",
        help="Interactively prompt for required inputs",
    ),
):
    registry = get_lotus_registry()
    cap = registry.get(cap_id)
    if not cap:
        raise typer.Exit(code=1)

    try:
        inputs = json.loads(input_json) if input_json else {}
    except Exception as exc:
        typer.echo(f"Invalid JSON input: {exc}")
        raise typer.Exit(code=1)

    if set_values:
        for item in set_values:
            if "=" not in item:
                typer.echo(f"Invalid --set value: {item} (expected key=value)")
                raise typer.Exit(code=1)
            key, value = item.split("=", 1)
            inputs[key] = value

    if cap.kind == LotusKind.nav:
        route = (cap.data or {}).get("route")
        typer.echo(json.dumps({"ok": True, "kind": "nav", "route": route}))
        return

    if cap.kind != LotusKind.action:
        typer.echo(json.dumps({"ok": False, "error": "not_action"}))
        raise typer.Exit(code=1)

    data = cap.data or {}
    action_name = data.get("action") or cap.id
    plugin_name = data.get("plugin") or cap.plugin

    if not plugin_name:
        typer.echo(json.dumps({"ok": False, "error": "missing_plugin"}))
        raise typer.Exit(code=1)

    plugin = get_plugin_instance(plugin_name)
    if not plugin:
        typer.echo(json.dumps(
            {"ok": False, "error": f"plugin_not_found:{plugin_name}"}))
        raise typer.Exit(code=1)

    schema = _resolve_input_schema(cap) or {}
    resolver = _resolve_resolver_config(cap)

    required_fields = []
    if isinstance(schema, dict):
        required_fields = schema.get("required") or []

    if prompt and schema and required_fields:
        inputs = _prompt_required_from_schema(schema, inputs)
    elif prompt and resolver:
        defaults = resolver.get("defaults") if isinstance(
            resolver, dict) else None
        if isinstance(defaults, dict):
            inputs = {**defaults, **inputs}

        prompt_fields = resolver.get(
            "prompt_fields") if isinstance(resolver, dict) else None
        if isinstance(prompt_fields, list):
            for field in prompt_fields:
                if field in inputs:
                    continue
                answer = questionary.text(f"{field}").ask()
                if answer:
                    inputs[field] = _maybe_parse_value(answer)

        resolver_action = resolver.get(
            "action") if isinstance(resolver, dict) else None
        if resolver_action:
            context = PluginContext(
                bus=None, capability_registry=None, resources=None)
            result = plugin.execute(
                resolver_action,
                str(uuid4()),
                inputs,
                context=context,
            )
            input_schema = None
            if isinstance(result, dict):
                input_schema = result.get(
                    "input_schema") or result.get("schema")
            exposed = _extract_exposed_inputs(
                result) if isinstance(result, dict) else []

            target_field = resolver.get("target_field", "inputs") if isinstance(
                resolver, dict) else "inputs"
            if isinstance(input_schema, dict):
                inputs = _prompt_required_from_schema(input_schema, inputs)
            elif exposed:
                inputs = _prompt_exposed_inputs(exposed, target_field, inputs)
    elif prompt:
        raw = questionary.text(
            "Input JSON (optional)",
            default="{}",
        ).ask()
        if raw:
            try:
                extra = json.loads(raw)
                if isinstance(extra, dict):
                    inputs.update(extra)
            except Exception as exc:
                typer.echo(f"Invalid JSON input: {exc}")
                raise typer.Exit(code=1)

    execution_id = str(uuid4())
    context = PluginContext(bus=None, capability_registry=None, resources=None)
    try:
        result = plugin.execute(
            action_name, execution_id, inputs, context=context)
    except Exception as exc:
        typer.echo(json.dumps({"ok": False, "error": str(exc)}))
        raise typer.Exit(code=1)

    typer.echo(json.dumps({"ok": True, "result": result}, indent=2))
