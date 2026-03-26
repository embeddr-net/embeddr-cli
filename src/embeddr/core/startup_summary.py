"""Consolidated startup summary for Embeddr.

Renders a clean, compact startup banner after all plugins and services
have been loaded. Replaces the scattered typer.secho() calls in serve.py.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import typer

logger = logging.getLogger("embeddr.core.startup")

# Slot constants for service discovery
_SLOT_CATEGORIES: List[Tuple[str, List[str]]] = [
    ("Storage", ["storage.blob"]),
    ("Search", ["search.text", "search.similar"]),
    ("Inference", ["inference.llm", "inference.vision", "inference.embedding"]),
    ("Features", ["feature.generator"]),
    ("Resolvers", ["resource.adapter"]),
]


@dataclass
class TransportInfo:
    name: str
    title: str
    links: List[Dict[str, str]] = field(default_factory=list)


@dataclass
class StartupContext:
    """All the data needed to render the startup summary."""
    version: str = "0.2.0"
    host: str = "127.0.0.1"
    port: int = 8003
    docs_enabled: bool = False
    auth_mode: str = "open"
    store_label: str = "sqlite"
    plugin_count: int = 0
    disabled_count: int = 0
    plugin_profile: str = ""
    # Lotus capability breakdown
    action_count: int = 0
    feature_count: int = 0
    resolver_count: int = 0
    config_count: int = 0
    nav_count: int = 0
    artifact_type_count: int = 0
    other_cap_count: int = 0  # providers, workflows, storage, etc.
    # UI
    panel_count: int = 0
    transport_count: int = 0
    transports: List[TransportInfo] = field(default_factory=list)
    # Filled by _gather_services
    services: Dict[str, List[Dict[str, str]]] = field(default_factory=dict)
    # For verbose plugin listing
    plugin_lines: List[str] = field(default_factory=list)


def _gather_services(ctx: StartupContext) -> None:
    """Query the Lotus registry for active service capabilities by slot."""
    try:
        from embeddr.core.plugin_loader import get_lotus_registry
    except ImportError:
        return

    reg = get_lotus_registry()
    if not reg:
        return

    for category, slots in _SLOT_CATEGORIES:
        entries: List[Dict[str, str]] = []
        seen_plugins: set[str] = set()

        for slot_name in slots:
            caps = reg.list(slot=slot_name)
            for cap in caps:
                plugin = cap.plugin or "unknown"
                if plugin in seen_plugins:
                    # Same plugin, different slot — just add the slot label
                    for entry in entries:
                        if entry["plugin"] == plugin:
                            short = slot_name.rsplit(".", 1)[-1]
                            if short not in entry.get("slots", ""):
                                entry["slots"] += f", {short}"
                            break
                    continue

                seen_plugins.add(plugin)
                short = slot_name.rsplit(".", 1)[-1]
                entries.append({
                    "plugin": plugin,
                    "slots": short,
                    "title": cap.title or "",
                })

        if entries:
            ctx.services[category] = entries


def _format_transport_link(host: str, port: int, path: str) -> str:
    if path.startswith("http://") or path.startswith("https://"):
        return path
    path = path.lstrip("/")
    return f"http://{host}:{port}/{path}"


def build_startup_context(
    *,
    host: str = "127.0.0.1",
    port: int = 8003,
    docs_enabled: bool = False,
) -> StartupContext:
    """Build the startup context from current system state."""
    from embeddr.core.plugin_loader import get_all_plugin_instances
    from embeddr.core.plugin_discovery import resolve_plugin_filter

    ctx = StartupContext(
        host=host,
        port=port,
        docs_enabled=docs_enabled,
    )

    # Version
    try:
        from importlib.metadata import version as pkg_version
        ctx.version = pkg_version("embeddr-cli")
    except Exception:
        ctx.version = "0.2.0"

    # Auth mode
    ctx.auth_mode = os.environ.get("EMBEDDR_AUTH_MODE", "open").strip() or "open"

    # Store label
    try:
        from sqlalchemy.engine.url import make_url
        from embeddr.core.config import settings
        from embeddr.db.session import get_engine

        engine = get_engine()
        try:
            engine_url = engine.url
        except Exception:
            engine_url = make_url(settings.DATABASE_URL)

        if engine_url.drivername == "sqlite":
            db_path = engine_url.database or "embeddr.db"
            # Show relative path if in workspace
            try:
                from pathlib import Path
                db_p = Path(str(db_path))
                cwd = Path.cwd()
                ctx.store_label = f"sqlite ({db_p.relative_to(cwd)})"
            except (ValueError, TypeError):
                ctx.store_label = f"sqlite ({db_path})"
        else:
            try:
                ctx.store_label = engine_url.render_as_string(hide_password=True)
            except Exception:
                ctx.store_label = str(engine_url.drivername)
    except Exception as e:
        logger.debug("Failed to resolve store label: %s", e)

    # Plugins
    loaded = get_all_plugin_instances()
    ctx.plugin_count = len(loaded)

    # Plugin profile / filter info
    pf = resolve_plugin_filter()
    ctx.plugin_profile = pf.profile_name
    if pf.mode == "denylist":
        ctx.disabled_count = len(pf.plugins)

    # Capability breakdown from Lotus registry
    try:
        from embeddr.core.plugin_loader import get_lotus_registry
        from embeddr_core.models.lotus import LotusKind

        reg = get_lotus_registry()
        all_caps = reg.list()
        kind_counts: Dict[str, int] = {}
        for cap in all_caps:
            k = cap.kind.value if cap.kind else "unknown"
            kind_counts[k] = kind_counts.get(k, 0) + 1

        ctx.action_count = kind_counts.get("action", 0)
        ctx.feature_count = kind_counts.get("feature", 0)
        ctx.resolver_count = kind_counts.get("resolver", 0)
        ctx.config_count = kind_counts.get("config", 0)
        ctx.nav_count = kind_counts.get("nav", 0)
        ctx.artifact_type_count = kind_counts.get("artifact_type", 0)
        ctx.other_cap_count = sum(
            v for k, v in kind_counts.items()
            if k not in ("action", "feature", "resolver", "config", "nav", "artifact_type")
        )
    except Exception as e:
        logger.debug("Failed to count capabilities: %s", e)

    # Panel counts from plugins
    try:
        for plugin in loaded:
            panels = getattr(plugin, "panels", None)
            if panels:
                ctx.panel_count += len(panels)
    except Exception as e:
        logger.debug("Failed to count panels: %s", e)

    # Build verbose plugin lines
    try:
        from embeddr_core.plugin_interface import PluginIntent
        for plugin in sorted(loaded, key=lambda p: p.name):
            badges = []
            try:
                if any(getattr(a, "ui_component", None) for a in getattr(plugin, "actions", [])):
                    badges.append("UI")
            except Exception:
                pass
            if PluginIntent.REGISTER_API in plugin.intents:
                badges.append("API")
            if PluginIntent.REGISTER_CLI in plugin.intents:
                badges.append("CLI")
            badge_str = f" [{', '.join(badges)}]" if badges else ""
            ctx.plugin_lines.append(f"{plugin.name} v{plugin.version}{badge_str}")
    except Exception as e:
        logger.debug("Failed to build plugin lines: %s", e)

    # Transports
    for plugin in loaded:
        if plugin.name.startswith("embeddr-transport-"):
            try:
                info = plugin.get_transport_info() or {}
            except Exception:
                info = {}
            if not info and hasattr(plugin, "transport_info"):
                info = getattr(plugin, "transport_info", {})

            title = (info or {}).get("title") or plugin.name
            links = (info or {}).get("links") or []
            ctx.transports.append(TransportInfo(
                name=plugin.name,
                title=title,
                links=links,
            ))

    # Services from Lotus registry
    _gather_services(ctx)

    return ctx


def render_startup_summary(ctx: StartupContext) -> None:
    """Render the consolidated startup summary."""
    display_host = ctx.host if ctx.host != "0.0.0.0" else "localhost"
    bar = "─" * 48

    typer.echo("")
    typer.secho(f"  Embeddr v{ctx.version}", fg=typer.colors.GREEN, bold=True)
    typer.secho(f"  {bar}", fg=typer.colors.BRIGHT_BLACK)

    # Core info
    typer.echo(f"  Store       {ctx.store_label}")
    typer.echo(f"  Auth        {ctx.auth_mode}")

    plugin_info = f"{ctx.plugin_count} loaded"
    if ctx.disabled_count:
        plugin_info += f" ({ctx.disabled_count} disabled)"
    if ctx.plugin_profile:
        plugin_info += f" [profile: {ctx.plugin_profile}]"
    typer.echo(f"  Plugins     {plugin_info}")

    total_caps = (
        ctx.action_count + ctx.feature_count + ctx.resolver_count
        + ctx.config_count + ctx.nav_count + ctx.artifact_type_count
        + ctx.other_cap_count
    )
    if total_caps:
        caps_parts = []
        if ctx.action_count:
            caps_parts.append(f"{ctx.action_count} actions")
        if ctx.feature_count:
            caps_parts.append(f"{ctx.feature_count} features")
        if ctx.resolver_count:
            caps_parts.append(f"{ctx.resolver_count} resolvers")
        if ctx.config_count:
            caps_parts.append(f"{ctx.config_count} configs")
        if ctx.artifact_type_count:
            caps_parts.append(f"{ctx.artifact_type_count} types")
        if ctx.nav_count:
            caps_parts.append(f"{ctx.nav_count} nav")
        if ctx.other_cap_count:
            caps_parts.append(f"{ctx.other_cap_count} other")
        typer.echo(f"  Lotus       {total_caps} capabilities ({', '.join(caps_parts)})")

    if ctx.panel_count:
        typer.echo(f"  Panels      {ctx.panel_count}")

    # Verbose plugin listing (debug level)
    for line in ctx.plugin_lines:
        logger.debug("  Plugin: %s", line)

    # Services
    if ctx.services:
        typer.echo("")
        typer.secho("  Services", fg=typer.colors.CYAN)
        for category, entries in ctx.services.items():
            parts = []
            for entry in entries:
                plugin = entry["plugin"]
                slots = entry.get("slots", "")
                if slots:
                    parts.append(f"{plugin} ({slots})")
                else:
                    parts.append(plugin)
            line = ", ".join(parts)
            label = f"    {category:<12}"
            typer.echo(f"{label}{line}")

    # Transports
    if ctx.transports:
        typer.echo("")
        typer.secho("  Transports", fg=typer.colors.CYAN)
        for transport in ctx.transports:
            if transport.links:
                for link in transport.links:
                    label = link.get("label") or transport.title
                    path = link.get("path") or ""
                    if path:
                        url = _format_transport_link(display_host, ctx.port, path)
                        typer.echo(f"    {label:<20}{url}")
            else:
                typer.echo(f"    {transport.title}")

    # URLs
    typer.echo("")
    typer.secho(f"  {bar}", fg=typer.colors.BRIGHT_BLACK)
    typer.secho(f"  Web UI      http://{display_host}:{ctx.port}", fg=typer.colors.CYAN)
    if ctx.docs_enabled:
        typer.secho(f"  API Docs    http://{display_host}:{ctx.port}/api/docs", fg=typer.colors.MAGENTA)
    typer.secho(f"  {bar}", fg=typer.colors.BRIGHT_BLACK)
    typer.secho("  Press Ctrl+C to stop server\n", fg=typer.colors.BRIGHT_BLACK)
