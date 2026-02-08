import json
import importlib
import time
import os
from typing import Optional, List, Any, Dict
from uuid import uuid4

import typer
import questionary
import httpx

from embeddr.core.plugin_loader import get_lotus_registry, get_plugin_instance
from embeddr_core.models.lotus import LotusKind
from embeddr_core.plugin_interface import PluginContext
from embeddr_core.services.config_service import resolve_plugin_config_for_plugin
from embeddr.core.plugin_context_helpers import LotusContext

app = typer.Typer(help="Lotus capabilities and invocation")

# Default to remote access unless --local is specified
DEFAULT_API_URL = os.environ.get("EMBEDDR_API_URL", "http://localhost:8000")


def _resolve_input_schema(cap_data: dict) -> Optional[dict[str, Any]]:
    # Handle both object and dict access for cap (compatibility between local obj and remote dict)
    data = cap_data.get("data") if isinstance(
        cap_data, dict) else (cap_data.data or {})
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
    # Handle both object and dict
    data = cap.get("data") if isinstance(cap, dict) else (cap.data or {})
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
    is_dict = isinstance(cap, dict)
    cap_data = cap.get("data") if is_dict else (cap.data or {})

    data_out = dict(cap_data)
    if resolve_schema:
        schema = _resolve_input_schema(cap)
        if schema:
            data_out = dict(data_out)
            input_block = dict(data_out.get("input") or {})
            input_block["schema"] = schema
            data_out["input"] = input_block

    return {
        "id": cap.get("id") if is_dict else cap.id,
        "kind": cap.get("kind") if is_dict else cap.kind,
        "title": cap.get("title") if is_dict else cap.title,
        "description": cap.get("description") if is_dict else cap.description,
        "plugin": cap.get("plugin") if is_dict else cap.plugin,
        "version": cap.get("version") if is_dict else cap.version,
        "tags": cap.get("tags") if is_dict else cap.tags,
        "slot": cap.get("slot") if is_dict else cap.slot,
        "data": data_out,
    }


def _get_api_client():
    return httpx.Client(base_url=DEFAULT_API_URL, timeout=30.0)


def _fetch_cap(cap_id: str, local: bool):
    if local:
        registry = get_lotus_registry()
        cap = registry.get(cap_id)
        if cap:
            return cap
        # Try stripping kind prefix locally
        if cap_id.startswith("action.") or cap_id.startswith("trigger."):
            short_id = cap_id.split(".", 1)[1]
            return registry.get(short_id)
        return None
    else:
        try:
            with _get_api_client() as client:
                # API Limit is usually 500. We fetch max allowed to scan for ID.
                # Ideally API should support GET /lotus/{id}
                res = client.get("/api/v1/lotus/list", params={"limit": 500})
                if res.status_code != 200:
                    typer.secho(
                        f"API Error fetching capabilities: {res.status_code} {res.text}", fg="red")
                    return None

                items = res.json().get("items", [])
                for c in items:
                    if c["id"] == cap_id:
                        return c

                # Try stripping prefixes if exact match fails
                if cap_id.startswith("action."):
                    alt_id = cap_id[7:]
                    for c in items:
                        if c["id"] == alt_id:
                            return c
                if cap_id.startswith("trigger."):
                    alt_id = cap_id[8:]
                    for c in items:
                        if c["id"] == alt_id:
                            return c
        except Exception as e:
            typer.secho(f"Connection failed: {e}", fg="red")
            raise typer.Exit(1)
    return None


@app.command("list")
def list_caps(
    kind: Optional[LotusKind] = typer.Option(None, help="Filter by kind"),
    plugin: Optional[str] = typer.Option(None, help="Filter by plugin"),
    slot: Optional[str] = typer.Option(None, help="Filter by slot"),
    query: Optional[str] = typer.Option(None, help="Search query"),
    limit: int = typer.Option(50, help="Max results for query"),
    as_json: bool = typer.Option(False, "--json", help="Output JSON"),
    local: bool = typer.Option(
        False, "--local", help="Run locally instead of remote API"),
):
    caps = []

    if local:
        registry = get_lotus_registry()
        if query:
            results = registry.query(query, limit=limit)
            caps = [r.capability for r in results]
        else:
            caps = registry.list(kind=kind, slot=slot, plugin=plugin)
    else:
        try:
            with _get_api_client() as client:
                # Clamp limit to max allowed by API (500)
                api_limit = min(limit, 500)

                if query:
                    res = client.get("/api/v1/lotus/query",
                                     params={"q": query, "limit": api_limit})
                    if res.status_code != 200:
                        typer.secho(f"API Error: {res.text}", fg="red")
                        raise typer.Exit(1)
                    # Query returns results list with specialized structure
                    caps = res.json().get("results", [])
                else:
                    params = {"limit": api_limit}
                    if kind:
                        params["kind"] = kind.value
                    if plugin:
                        params["plugin"] = plugin
                    if slot:
                        params["slot"] = slot
                    res = client.get("/api/v1/lotus/list", params=params)
                    if res.status_code != 200:
                        typer.secho(f"API Error: {res.text}", fg="red")
                        raise typer.Exit(1)
                    caps = res.json().get("items", [])
        except Exception as e:
            typer.secho(f"Connection failed: {e}", fg="red")
            raise typer.Exit(1)

    if as_json:
        typer.echo(json.dumps([_serialize_cap(c) for c in caps], indent=2))
        return

    typer.echo(f"\n🌸 Lotus Capabilities ({'Local' if local else 'Remote'}):")
    for cap in caps:
        # Handle dict vs object
        c_id = cap.get("id") if isinstance(cap, dict) else cap.id
        c_kind = cap.get("kind") if isinstance(cap, dict) else cap.kind
        c_plugin = cap.get("plugin") if isinstance(
            cap, dict) else (cap.plugin or 'core')
        c_title = cap.get("title") if isinstance(cap, dict) else cap.title
        typer.echo(
            f"- {c_id} [{c_kind}] {c_plugin} :: {c_title}"
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
    local: bool = typer.Option(
        False, "--local", help="Run locally instead of remote API"),
):
    cap = _fetch_cap(cap_id, local)

    if not cap:
        typer.secho(f"Capability not found: {cap_id}", fg="red")
        raise typer.Exit(code=1)

    payload = _serialize_cap(cap, resolve_schema=resolve_schema)
    if as_json:
        typer.echo(json.dumps(payload, indent=2))
        return

    c_id = cap.get("id") if isinstance(cap, dict) else cap.id
    c_kind = cap.get("kind") if isinstance(cap, dict) else cap.kind
    c_title = cap.get("title") if isinstance(cap, dict) else cap.title
    c_plugin = cap.get("plugin") if isinstance(cap, dict) else cap.plugin
    c_ver = cap.get("version") if isinstance(cap, dict) else cap.version
    c_desc = cap.get("description") if isinstance(
        cap, dict) else cap.description

    typer.echo(f"{c_id} ({c_kind})")
    typer.echo(f"Title: {c_title}")
    typer.echo(f"Plugin: {c_plugin} v{c_ver}")
    if c_desc:
        typer.echo(f"Description: {c_desc}")


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
    local: bool = typer.Option(
        False, "--local", help="Run locally instead of remote API"),
    wait: bool = typer.Option(
        True, "--wait/--no-wait", help="Wait for remote execution result (default True)"),
):
    # 1. Resolve Inputs first (needs Cap metadata)
    cap = _fetch_cap(cap_id, local)

    if not cap:
        typer.secho(f"Capability not found: {cap_id}", fg="red")
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

    # Check Kind
    c_kind = cap.get("kind") if isinstance(cap, dict) else cap.kind
    c_data = cap.get("data") if isinstance(cap, dict) else (cap.data or {})

    if c_kind == LotusKind.nav:
        if local:
            route = c_data.get("route")
            typer.echo(json.dumps({"ok": True, "kind": "nav", "route": route}))
        else:
            # Remote Nav? Just echo same.
            route = c_data.get("route")
            typer.echo(json.dumps({"ok": True, "kind": "nav", "route": route}))
        return

    if c_kind != LotusKind.action:
        typer.echo(json.dumps({"ok": False, "error": "not_action"}))
        raise typer.Exit(code=1)

    # Prompting Logic (Shared)
    # Note: _resolve_input_schema might fail cleanly if source code missing,
    # but prompting logic handles missing schema by just ignoring it.
    schema = _resolve_input_schema(cap) or {}
    resolver = _resolve_resolver_config(cap)

    required_fields = []
    if isinstance(schema, dict):
        required_fields = schema.get("required") or []

    if prompt and schema and required_fields:
        inputs = _prompt_required_from_schema(schema, inputs)
    elif prompt and resolver:
        # 1. Prompt for initial prompt_fields
        prompt_fields = resolver.get(
            "prompt_fields") if isinstance(resolver, dict) else None

        # Check skip conditions
        skip_conditions = resolver.get(
            "skip_prompt_if") if isinstance(resolver, dict) else []
        should_skip = any(k in inputs for k in skip_conditions)

        if isinstance(prompt_fields, list) and not should_skip:
            for field in prompt_fields:
                if field in inputs:
                    continue
                ans = questionary.text(f"{field}").ask()
                if ans:
                    inputs[field] = _maybe_parse_value(ans)

        defaults = resolver.get("defaults") if isinstance(
            resolver, dict) else None
        if isinstance(defaults, dict):
            inputs = {**defaults, **inputs}

        # 2. Resolve Dynamic Inputs
        if local:
            # Full local support
            plugin_name = c_data.get("plugin") or cap.plugin
            plugin = get_plugin_instance(plugin_name)
            if plugin:
                resolver_action = resolver.get(
                    "action") if isinstance(resolver, dict) else None
                if resolver_action:
                    ctx = PluginContext(
                        bus=None,
                        capability_registry=None,
                        resources=None,
                        config=resolve_plugin_config_for_plugin(
                            plugin_name=plugin_name),
                        lotus=LotusContext(),
                    )
                    result = plugin.execute(
                        resolver_action, str(uuid4()), inputs, context=ctx)
                    input_schema = None
                    if isinstance(result, dict):
                        input_schema = result.get(
                            "input_schema") or result.get("schema")
                    exposed = _extract_exposed_inputs(
                        result) if isinstance(result, dict) else []
                    if isinstance(input_schema, dict):
                        inputs = _prompt_required_from_schema(
                            input_schema, inputs)
                    elif exposed:
                        target_field = resolver.get("target_field", "inputs") if isinstance(
                            resolver, dict) else "inputs"
                        inputs = _prompt_exposed_inputs(
                            exposed, target_field, inputs)
        else:
            # Remote Resolver
            resolver_action = resolver.get(
                "action") if isinstance(resolver, dict) else None
            if resolver_action:
                # Dispatch resolver action to API
                try:
                    typer.secho(
                        f"Resolving dynamic inputs via {resolver_action}...", fg="white", dim=True)
                    with _get_api_client() as client:
                        payload = {
                            "result_id": str(uuid4()),
                            "kind": "action",
                            "data": {
                                "plugin": c_data.get("plugin") or (cap.get("plugin") if isinstance(cap, dict) else cap.plugin),
                                "action": resolver_action,
                                "inputs": inputs
                            }
                        }
                        res = client.post(
                            "/api/v1/lotus/dispatch", json=payload)
                        if res.status_code == 200:
                            data = res.json()
                            exec_id = data.get("execution_id")
                            # Poll briefly for result
                            if exec_id:
                                start = time.time()
                                while time.time() - start < 10:
                                    poll = client.get(
                                        f"/api/v1/executions/{exec_id}")
                                    if poll.status_code == 200:
                                        job = poll.json()
                                        if job.get("status") == "completed":
                                            result = job.get("outputs")
                                            exposed = _extract_exposed_inputs(
                                                result) if isinstance(result, dict) else []
                                            if exposed:
                                                target_field = resolver.get("target_field", "inputs") if isinstance(
                                                    resolver, dict) else "inputs"
                                                inputs = _prompt_exposed_inputs(
                                                    exposed, target_field, inputs)
                                            break
                                        if job.get("status") == "failed":
                                            typer.secho(
                                                f"Resolver failed: {job.get('error')}", fg="red")
                                            break
                                    time.sleep(0.5)

                except Exception as e:
                    typer.secho(
                        f"Warning: Remote resolver failed: {e}", fg="yellow")
    elif prompt:
        raw = questionary.text("Input JSON (optional)", default="{}").ask()
        if raw:
            try:
                extra = json.loads(raw)
                if isinstance(extra, dict):
                    inputs.update(extra)
            except Exception as exc:
                typer.echo(f"Invalid JSON input: {exc}")
                raise typer.Exit(code=1)

    # Execute
    if local:
        action_name = c_data.get("action") or cap.id
        plugin_name = c_data.get("plugin") or cap.plugin

        if not plugin_name:
            typer.echo(json.dumps({"ok": False, "error": "missing_plugin"}))
            raise typer.Exit(code=1)

        plugin = get_plugin_instance(plugin_name)
        if not plugin:
            typer.echo(json.dumps(
                {"ok": False, "error": f"plugin_not_found:{plugin_name}"}))
            raise typer.Exit(code=1)

        execution_id = str(uuid4())
        context = PluginContext(
            bus=None,
            capability_registry=None,
            resources=None,
            config=resolve_plugin_config_for_plugin(plugin_name=plugin_name),
            lotus=LotusContext(),
        )
        try:
            result = plugin.execute(
                action_name, execution_id, inputs, context=context)
        except Exception as exc:
            typer.echo(json.dumps({"ok": False, "error": str(exc)}))
            raise typer.Exit(code=1)

        typer.echo(json.dumps({"ok": True, "result": result}, indent=2))

    else:
        # Remote Dispatch
        with _get_api_client() as client:
            payload = {
                "result_id": cap_id,  # Or use UUID if just dispatching
                "kind": "action",
                "data": {
                    "plugin": c_data.get("plugin") or (cap.get("plugin") if isinstance(cap, dict) else cap.plugin),
                    "action": c_data.get("action") or (cap.get("data", {}).get("action_name") if isinstance(cap, dict) else cap.id),
                    "inputs": inputs
                }
            }
            # Handle action naming fallback
            if not payload["data"]["action"]:
                # Try using cap ID if action not explicit?
                # Usually cap ID != action name exactly (e.g. search.text might be action search.text)
                payload["data"]["action"] = cap_id

            # Post
            try:
                res = client.post("/api/v1/lotus/dispatch", json=payload)
                if res.status_code != 200:
                    typer.secho(f"Dispatch Failed: {res.text}", fg="red")
                    raise typer.Exit(1)

                data = res.json()
                exec_id = data.get("execution_id")

                if not wait or not exec_id:
                    typer.echo(json.dumps(data, indent=2))
                    return

                # Poll for result
                typer.secho(
                    f"Polling execution {exec_id}...", fg="white", dim=True)
                start = time.time()
                while time.time() - start < 60:  # 60s timeout
                    poll = client.get(f"/api/v1/executions/{exec_id}")
                    if poll.status_code == 200:
                        job = poll.json()
                        status = job.get("status")
                        if status == "completed":
                            typer.echo(json.dumps(
                                {"ok": True, "result": job.get("outputs")}, indent=2))
                            return
                        if status == "failed":
                            typer.echo(json.dumps(
                                {"ok": False, "error": job.get("error")}, indent=2))
                            return
                    time.sleep(1)

                typer.echo(json.dumps(
                    {"ok": False, "error": "timeout_polling"}, indent=2))

            except Exception as e:
                typer.secho(f"Request Error: {e}", fg="red")
                raise typer.Exit(1)
