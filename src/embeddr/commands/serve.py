from embeddr.lotus.core_caps import register_core_lotus_capabilities
# from embeddr.mcp.tools.core import register_core_tools
from embeddr_core.plugin_interface import EmbeddrEvent
from embeddr.db.session import create_db_and_tables
import logging
import os
import sys
import warnings
import asyncio
from typing import Optional
from contextlib import asynccontextmanager, AsyncExitStack
from pathlib import Path

import typer
import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from embeddr.api import routes
from embeddr.core.logging_utils import setup_logging
from embeddr.core.config import get_data_dir
from embeddr.core.plugin_loader import (
    load_python_plugins,
    _EVENT_BUS,
    get_all_plugin_instances,
    initialize_all_plugins,
    startup_all_plugins,
)
from embeddr.core.execution_spine import ExecutionSpine
from embeddr.services.socket_manager import manager
from embeddr_core.services.config_service import resolve_plugin_config
from embeddr.db.session import get_engine
from sqlmodel import Session

# Import and load local FS scanner plugin manually for now until dynamic loading is robust
# This ensures it's available on startup
import importlib.util


# Internal imports - now from the embeddr package

# Add embeddr-core to path if it exists as a sibling
# From src/embeddr/commands/serve.py, parents[4] is the 'public' directory
PACKAGE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FRONTEND_DIR = PACKAGE_DIR / "web"

# core_path = Path(__file__).resolve().parents[4] / "embeddr-core" / "src"
# if core_path.exists():
#    sys.path.append(str(core_path))

try:
    from embeddr_core.services.embedding import unload_model
except ImportError:

    def unload_model():
        pass


logger = logging.getLogger("embeddr.local")
# logger.info("Embeddr Local is starting up...") # Moved to startup event to avoid double logging on reload


# Suppress websockets deprecation warnings
warnings.filterwarnings(
    "ignore", category=DeprecationWarning, module="uvicorn")
warnings.filterwarnings(
    "ignore", category=DeprecationWarning, module="websockets")

# Define the path to the frontend build directory
# Root is parents[3] (embeddr-local-api)
ROOT_DIR = Path(__file__).resolve().parents[3]
FRONTEND_DIR = Path(os.environ.get(
    "EMBEDDR_FRONTEND_DIR", DEFAULT_FRONTEND_DIR))

# Create MCP App globally to access its lifespan
# mcp_app = mcp.http_app(transport="http", path="/messages")


def _log_section(title: str) -> None:
    typer.secho(f"\n== {title} ==", fg=typer.colors.CYAN)


def _format_transport_link(display_host: str, port: str, link: str) -> str:
    if link.startswith("http://") or link.startswith("https://"):
        return link
    return f"http://{display_host}:{port}{link}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Retrieve config from env (set by serve command)
    host = os.environ.get("EMBEDDR_HOST", "127.0.0.1")
    port = os.environ.get("EMBEDDR_PORT", "8003")
    mcp_transport = os.environ.get(
        "EMBEDDR_MCP_TRANSPORT", "disabled").lower()
    mcp_enabled = mcp_transport != "disabled"
    docs_enabled = os.environ.get(
        "EMBEDDR_ENABLE_DOCS", "false").lower() == "true"

    display_host = "127.0.0.1" if host == "0.0.0.0" else host

    # Initialize DB, load models, etc.
    create_db_and_tables()

    try:
        from embeddr_core.services.scanner_registry import scanner_registry
        logger.info(
            f"Registry state BEFORE plugin load: {scanner_registry._scanners.keys()}")
    except ImportError:
        logger.error("Could not import scanner_registry for debugging")

    # Plugin loading is handled in create_app to ensure API registration works
    # Here we just trigger startup hooks

    # Trigger Plugin on_startup hooks
    startup_all_plugins()

    # Start Execution Spine Worker
    # spine = ExecutionSpine()
    # spine_task = asyncio.create_task(spine.start_worker())

    # Bridge EventBus to WebSocket Manager
    def broadcast_to_frontend(event: EmbeddrEvent):
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(manager.broadcast_event(
                event.event_type, event.payload, source=event.source))
        except RuntimeError:
            pass

    # NOTE: Currently disabled to reduce noise, we subscribe to specific events below
    # _EVENT_BUS.subscribe("*", broadcast_to_frontend)

    # MCP tool registration is handled by transport plugins

    try:
        from embeddr_core.services.scanner_registry import scanner_registry
        logger.info(
            f"Registry state AFTER plugin load: {scanner_registry._scanners.keys()}")
        if "collection:directory" in scanner_registry._scanners:
            logger.info(
                f"Final scanner for collection:directory: {scanner_registry._scanners['collection:directory']}")
    except ImportError:
        pass

    # --- Event Bus <-> WebSocket Bridge ---
    loop = asyncio.get_running_loop()

    def bridge_event_to_ws(event):
        try:
            raw_event_type = str(event.event_type or "")
            if raw_event_type.startswith("ui:") or raw_event_type.startswith("ui."):
                logger.info("[UI EVENT] %s payload=%s",
                            event.event_type, event.payload)
            event_type = raw_event_type
            payload = event.payload

            if event.source == "comfyui":
                if event_type.startswith("comfy."):
                    event_type = event_type.split("comfy.", 1)[1]
                if event_type == "artifact.preview":
                    event_type = "preview"
                    if isinstance(payload, dict) and payload.get("data"):
                        payload = payload.get("data")
            # We wrap the broadcast in a task on the main loop
            asyncio.run_coroutine_threadsafe(
                # manager.broadcast_event(
                #     # Maps to "plugin:event_type" in frontend
                #     f"plugin:{event.event_type}",
                #     event.model_dump()
                # ),
                manager.broadcast_event(
                    # Maps to "plugin:event_type" in frontend
                    event_type,
                    payload,
                    source=event.source
                ),
                loop
            )

            if raw_event_type.startswith("execution."):
                from embeddr.api.DEPRECATED_endpoints import ws as ws_endpoint

                asyncio.run_coroutine_threadsafe(
                    ws_endpoint.broadcast_status(),
                    loop,
                )
        except Exception as e:
            logger.error(f"Bridge Error: {e}")

    # Subscribe to relevant events
    # (In V3 we will add wildcard subscription)
    _EVENT_BUS.subscribe("*", bridge_event_to_ws)
    # _EVENT_BUS.subscribe("embeddings.generated", bridge_event_to_ws)
    # _EVENT_BUS.subscribe("artifact.created", bridge_event_to_ws)
    # _EVENT_BUS.subscribe("scan.started", bridge_event_to_ws)
    # _EVENT_BUS.subscribe("scan.completed", bridge_event_to_ws)
    # _EVENT_BUS.subscribe("scan.failed", bridge_event_to_ws)

    # # LOTUS
    # # _EVENT_BUS.subscribe("ui:toast", bridge_event_to_ws)
    # # _EVENT_BUS.subscribe("ui:navigate", bridge_event_to_ws)
    # _EVENT_BUS.subscribe("ui:open_artifact", bridge_event_to_ws)
    # _EVENT_BUS.subscribe("ui:open_gallery", bridge_event_to_ws)

    # _EVENT_BUS.subscribe("execution.started", bridge_event_to_ws)
    # _EVENT_BUS.subscribe("execution.completed", bridge_event_to_ws)
    # _EVENT_BUS.subscribe("execution.failed", bridge_event_to_ws)

    # Start ComfyUI WebSocket monitor
    # REMOVED: Managed by embeddr-comfyui plugin
    # asyncio.create_task(monitor_comfy_events())

    # Legacy AutomationManager (V1) disabled by default.
    # Enable only for migration/debug with EMBEDDR_ENABLE_AUTOMATION_V1=true
    if os.environ.get("EMBEDDR_ENABLE_AUTOMATION_V1", "false").lower() == "true":
        try:
            from embeddr.services.automation_manager import automation_manager
            from embeddr.services.ingestion_service import ingestion_service

            await ingestion_service.start()
            automation_manager.start()
            logger.info("AutomationManager (V1) & IngestionService started.")
        except Exception as e:
            logger.error(f"Failed to start Automation/Ingestion services: {e}")

    # Display Loaded Plugins Summary
    from embeddr_core.plugin_interface import PluginIntent
    loaded_plugins = get_all_plugin_instances()
    if loaded_plugins:
        typer.echo("\n   🧩 Loaded Plugins:")
        typer.echo("   " + "-" * 45)
        for plugin in sorted(loaded_plugins, key=lambda p: p.name):
            try:
                badges = []
                # Check capabilities
                has_ui = any(a.ui_component for a in plugin.actions)
                if has_ui:
                    badges.append("⚛️ UI")
                if PluginIntent.REGISTER_API in plugin.intents:
                    badges.append("🔌 API")
                if PluginIntent.REGISTER_CLI in plugin.intents:
                    badges.append("💻 CLI")

                # Use ANSI colors for better look if typer supports it (it does)
                name_str = typer.style(
                    plugin.name, fg=typer.colors.WHITE, bold=True)
                version_str = typer.style(
                    f"v{plugin.version}", fg=typer.colors.BRIGHT_BLACK)
                badges_str = "  ".join(badges)

                typer.echo(
                    f"   • {name_str:<25} {version_str:<10} {badges_str}")
            except Exception as pe:
                typer.secho(
                    f"   ⚠️  Load Error in {plugin.name}: {pe}", fg=typer.colors.RED)
                logger.error(f"Error summarising plugin {plugin.name}: {pe}")

    try:
        from sqlalchemy.engine.url import make_url
        from embeddr.core.config import settings
        from embeddr.db.adapters import get_adapter
        from embeddr.db.session import get_engine
        from embeddr.services.blob_registry import (
            list_providers,
            list_resolvers,
            list_provider_resolvers,
            get_default_provider_name,
            get_default_resolver_name,
        )

        providers = list_providers()
        resolvers = list_resolvers()
        provider_resolvers = list_provider_resolvers()
        default_provider = get_default_provider_name()
        default_resolver = get_default_resolver_name()

        provider_override = os.environ.get("EMBEDDR_DB_PROVIDER")
        adapter = get_adapter(settings.DATABASE_URL,
                              provider=provider_override)
        engine = get_engine()
        try:
            engine_url = engine.url
        except Exception:
            engine_url = make_url(settings.DATABASE_URL)

        if engine_url.drivername == "sqlite":
            store_label = f"sqlite ({engine_url.database})"
        else:
            try:
                store_label = engine_url.render_as_string(hide_password=True)
            except Exception:
                store_label = settings.DATABASE_URL

        typer.secho("\n   🧠 Core Capabilities (routed by Lotus):",
                    fg=typer.colors.CYAN)
        typer.echo("   " + "-" * 45)
        if providers:
            available_providers = ", ".join(sorted(providers.keys()))
            typer.secho("   • artifacts.upload", fg=typer.colors.YELLOW)
            typer.echo(
                f"     - provider: {default_provider} (available: {available_providers})")
        else:
            typer.secho("   • artifacts.upload: none",
                        fg=typer.colors.BRIGHT_BLACK)

        typer.secho("   • artifacts.get", fg=typer.colors.YELLOW)
        if provider_override:
            typer.echo(
                f"     - store: {adapter.name} ({store_label}) [provider={provider_override}]")
        else:
            typer.echo(f"     - store: {adapter.name} ({store_label})")

        if resolvers:
            available_resolvers = ", ".join(sorted(resolvers.keys()))
            typer.secho("   • artifacts.resolve", fg=typer.colors.YELLOW)
            typer.echo(
                f"     - resolver: {default_resolver} (available: {available_resolvers})")
        else:
            typer.secho("   • artifacts.resolve: none",
                        fg=typer.colors.BRIGHT_BLACK)

        if providers or resolvers:
            typer.secho("\n   🧱 Blob Capabilities:", fg=typer.colors.CYAN)
            typer.echo("   " + "-" * 45)

            if providers:
                typer.secho("   • providers", fg=typer.colors.YELLOW)
                for name in sorted(providers.keys()):
                    suffix = " (default)" if name == default_provider else ""
                    resolver_name = provider_resolvers.get(name)
                    resolver_hint = f" -> {resolver_name}" if resolver_name else ""
                    typer.echo(f"     - {name}{suffix}{resolver_hint}")
            else:
                typer.secho("   • providers: none",
                            fg=typer.colors.BRIGHT_BLACK)

            if resolvers:
                typer.secho("   • resolvers", fg=typer.colors.YELLOW)
                for name in sorted(resolvers.keys()):
                    typer.echo(f"     - {name}")
            else:
                typer.secho("   • resolvers: none",
                            fg=typer.colors.BRIGHT_BLACK)
    except Exception as e:
        logger.warning("Failed to render storage capability summary: %s", e)

    typer.secho("\n✨ Embeddr Local API has started!",
                fg=typer.colors.GREEN, bold=True)
    typer.echo("   " + "-" * 45)

    typer.secho("   👤 User Endpoints:", fg=typer.colors.CYAN)
    typer.secho(
        f"   👉 Web UI:    http://{display_host}:{port}", fg=typer.colors.CYAN)
    if docs_enabled:
        typer.secho(
            f"   📚 API Docs:  http://{display_host}:{port}/api/docs",
            fg=typer.colors.MAGENTA,
        )

    typer.secho("   ⚙️  System Endpoints:", fg=typer.colors.CYAN)
    typer.secho(
        f"   • System Info:  http://{display_host}:{port}/api/v2/system/info",
        fg=typer.colors.BRIGHT_BLACK,
    )
    typer.secho(
        f"   • Route List:   http://{display_host}:{port}/api/v2/system/routes",
        fg=typer.colors.BRIGHT_BLACK,
    )

    transports = [p for p in loaded_plugins if p.name.startswith(
        "embeddr-transport-")]
    typer.secho("   🔌 Transports:", fg=typer.colors.CYAN)
    if not transports:
        typer.secho("   • None", fg=typer.colors.BRIGHT_BLACK)
    else:
        for plugin in transports:
            info = None
            if hasattr(plugin, "get_transport_info"):
                try:
                    info = plugin.get_transport_info()
                except Exception:
                    info = None
            if not info and hasattr(plugin, "transport_info"):
                info = getattr(plugin, "transport_info")

            title = (info or {}).get("title") or plugin.name
            links = (info or {}).get("links") or []

            typer.secho(f"   • {title}", fg=typer.colors.YELLOW)
            for link in links:
                label = link.get("label") or "Link"
                path = link.get("path") or ""
                if path:
                    url = _format_transport_link(display_host, str(port), path)
                    typer.secho(f"     - {label}: {url}",
                                fg=typer.colors.BRIGHT_BLACK)

    show_official_docs = True
    if show_official_docs:
        typer.secho(
            f"   📚 Official Docs:  https://docs.embeddr.net",
            fg=typer.colors.BLUE,
        )

    typer.echo("   " + "-" * 45)
    typer.secho("   Press Ctrl+C to stop server\n",
                fg=typer.colors.BRIGHT_BLACK)

    # Start Execution Spine Worker
    spine = ExecutionSpine()
    spine_task = asyncio.create_task(spine.start_worker())

    # Initialize Automation Manager (V2)
    from embeddr.services.automation_manager_v2 import automation_manager
    # Start the automation worker
    await automation_manager.start()

    # Subscribe to all relevant events
    _EVENT_BUS.subscribe("artifact.created", automation_manager.handle_event)
    _EVENT_BUS.subscribe("artifact.updated", automation_manager.handle_event)
    _EVENT_BUS.subscribe("relation.added", automation_manager.handle_event)

    # Transport lifespans (e.g., FastMCP)
    async with AsyncExitStack() as stack:
        for plugin in get_all_plugin_instances():
            if hasattr(plugin, "get_transport_lifespan"):
                try:
                    ctx = plugin.get_transport_lifespan()
                    if ctx is not None:
                        await stack.enter_async_context(ctx)
                except Exception as e:
                    logger.error(
                        "Failed to enter transport lifespan for %s: %s",
                        plugin.name,
                        e,
                    )

        yield

    # Cleanup resources
    spine._running = False
    spine_task.cancel()

    from embeddr.services.ingestion_service import ingestion_service
    await ingestion_service.stop()

    unload_model()

    logger.info("Embeddr Local is shutting down...")


def default_local_origins(port: str) -> list[str]:
    return [
        f"http://localhost:{port}",
        f"http://127.0.0.1:{port}",
    ]


def parse_env_origins() -> list[str]:
    origins = os.environ.get("EMBEDDR_CORS_ORIGINS", "")
    if not origins:
        return []
    logger.info("Loading CORS origins from EMBEDDR_CORS_ORIGINS...")
    return [origin.strip() for origin in origins.split(",") if origin.strip()]


def dev_origins() -> list[str]:
    logger.info("Loading development CORS origins...")
    return [
        "http://localhost:3000",  # React Dev Server
        "http://localhost:4173",  # SDK Dev UI
    ]


def dynamic_origins() -> list[str]:
    host = os.environ.get("EMBEDDR_HOST", "127.0.0.1")
    port = os.environ.get("EMBEDDR_PORT", "8003")
    return [f"http://{host}:{port}"]


def aitoolkit_origins() -> list[str]:
    logger.info("Loading AI Toolkit CORS origins...")
    return [
        "http://localhost:3001",  # AI Toolkit default
    ]


def _register_endpoints(app: FastAPI, router, prefix: str, tags: list[str]):
    logger.info(f"Registering router: {prefix} with tags: {tags}")
    app.include_router(router, prefix=prefix, tags=tags)


def create_app(
    enable_mcp: bool = False,
    enable_docs: bool = False,
    no_plugins: bool = False,
    mcp_transport: str = "disabled",
) -> FastAPI:
    _log_section("Config")
    typer.secho(
        f"MCP: {enable_mcp} (transport={mcp_transport}) | Docs: {enable_docs}",
        fg=typer.colors.BRIGHT_BLACK,
    )
    app = FastAPI(
        title="Embeddr Local",
        lifespan=lifespan,
        docs_url="/api/docs" if enable_docs else None,
        redoc_url="/api/redoc" if enable_docs else None,
        openapi_url="/api/openapi.json" if enable_docs else None,
    )

    _log_section("CORS")
    port = os.environ.get("EMBEDDR_PORT", "8003")
    allowed_origins: set[str] = set(default_local_origins(port))
    allowed_origins |= set(parse_env_origins())
    allowed_origins |= set(dynamic_origins())
    if os.environ.get("EMBEDDR_ALLOW_DEV_ORIGINS", "false").lower() == "true":
        allowed_origins |= set(dev_origins())

    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(allowed_origins),
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT",
                       "PATCH", "DELETE", "OPTIONS", "HEAD"],
        allow_headers=["Authorization", "Content-Type",
                       "ngrok-skip-browser-warning"],
    )
    typer.secho("🔐 Allowed CORS origins:", fg=typer.colors.CYAN)
    for origin in allowed_origins:
        typer.echo(f"   - {origin}")

    _log_section("API Routes")
    # Include API Routes
    app.include_router(routes.router, prefix="/api/v1")

    # Include WebSocket Router
    from embeddr.api.DEPRECATED_endpoints import ws as ws_endpoint
    app.include_router(ws_endpoint.router)

    # Include V2 Routes
    from embeddr.api.v2 import artifacts as artifacts_v2
    from embeddr.api.v2 import plugins as plugins_v2
    from embeddr.api.v2 import system as system_v2
    from embeddr.api.v2 import collections as collections_v2
    from embeddr.api.v2 import executions as executions_v2
    from embeddr.api.v2 import workflows as workflows_v2
    from embeddr.api.v2 import config as config_v2
    from embeddr.api.v2 import maintenance as maintenance_v2
    # from embeddr.api.v2 import projections as projections_v2 # Moved to Plugin
    from embeddr.api.v2 import actions as actions_v2
    from embeddr.api.v2 import lotus as lotus_v2
    from embeddr.api.v2 import lotus_invoke_routes
    from embeddr.api.v2 import config_api as lotus_config
    from embeddr.api.v2 import resources as resources_v2

    _register_endpoints(app, artifacts_v2.router,
                        prefix="/api/v2/artifacts", tags=["artifacts"])
    _register_endpoints(app, plugins_v2.router,
                        prefix="/api/v2/plugins", tags=["plugins"])
    _register_endpoints(app, system_v2.router,
                        prefix="/api/v2/system", tags=["system"])
    _register_endpoints(app, collections_v2.router,
                        prefix="/api/v2/collections", tags=["collections"])
    _register_endpoints(app, executions_v2.router,
                        prefix="/api/v2/executions", tags=["executions"])
    _register_endpoints(app, workflows_v2.router,
                        prefix="/api/v2/workflows", tags=["workflows"])
    _register_endpoints(app, config_v2.router,
                        prefix="/api/v2/config", tags=["config"])
    _register_endpoints(app, maintenance_v2.router,
                        prefix="/api/v2/maintenance", tags=["maintenance"])
    _register_endpoints(app, actions_v2.router,
                        prefix="/api/v2/actions", tags=["actions"])
    _register_endpoints(app, resources_v2.router,
                        prefix="/api/v2/resources", tags=["resources"])

    _register_endpoints(app, lotus_v2.router,
                        prefix="/api/v2/lotus", tags=["lotus"])
    _register_endpoints(app, lotus_config.router, tags=["lotus", "config"],
                        prefix="/api/v2/lotus"
                        )
    _register_endpoints(app, lotus_invoke_routes.router,
                        prefix="/api/v2/lotus", tags=["lotus"])
    # app.include_router(projections_v2.router,
    #                    prefix="/api/v2/projections", tags=["projections"])

    # Serve Plugins Directory
    _log_section("Plugins")
    if not no_plugins:
        plugin_paths = []
        allow_dev_plugins = os.environ.get(
            "EMBEDDR_ALLOW_DEV_PLUGINS", "false").lower() == "true"

# 1. Environment / CLI Plugin Dir (Highest Priority)
        # This allows users to override dev plugins with their own or dist-plugins
        if os.environ.get("EMBEDDR_PLUGIN_DIR"):
            plugin_paths.append(Path(os.environ.get("EMBEDDR_PLUGIN_DIR")))

        env_plugin_dir = os.environ.get(
            "EMBEDDR_PLUGINS_DIR") or os.environ.get("EMBEDDR_PLUGIN_DIR")
        if env_plugin_dir:
            # Avoid duplicate if same as above (though simplistic check)
            p = Path(env_plugin_dir)
            if not any(existing == p for existing in plugin_paths):
                plugin_paths.append(p)

        # 2. Dev Workspace "embeddr-plugins" (Relative to this file)
        # Only load when explicitly enabled.
        if allow_dev_plugins:
            # We are in embeddr-cli/src/embeddr/commands/serve.py -> parents[3] is embeddr-cli
            cli_root = Path(__file__).resolve().parents[3]
            repo_root = cli_root.parent

            # 2a. Dist Plugins (Compiled Frontend) - High Priority
            dist_plugins_src = repo_root / "embeddr-plugins" / "plugins-dist"
            if dist_plugins_src.exists():
                if not any(existing == dist_plugins_src for existing in plugin_paths):
                    logger.debug(f"Found dist plugins at {dist_plugins_src}")
                    plugin_paths.append(dist_plugins_src)

            # 2b. Source Plugins
            dev_plugins_src = repo_root / "embeddr-plugins" / "plugins"

            if dev_plugins_src.exists():
                # Only add if not already added by env vars (to avoid shadowing/duplication issues if user pointed to it)
                if not any(existing == dev_plugins_src for existing in plugin_paths):
                    logger.debug(f"Found dev plugins at {dev_plugins_src}")
                    plugin_paths.append(dev_plugins_src)

        # 3. Local "plugins" folder
        # plugin_paths.append(Path.cwd() / "plugins")

        # Phase 1: Load all plugins (Python Logic + API)
        register_core_lotus_capabilities()
        for p_path in plugin_paths:
            if p_path.exists():
                logger.debug(f"Loading plugins from {p_path}...")
                load_python_plugins(p_path, app=app)

        # Phase 2: Mount Static Assets for loaded plugins
        # We mount each plugin's directory to /api/v2/plugins/{plugin_name}/static
        # This ensures the correct directory (Source vs Dist) is served for each plugin
        loaded_plugins = get_all_plugin_instances()
        for plugin in loaded_plugins:
            source_path = getattr(plugin, '_source_path', None)
            if source_path and isinstance(source_path, Path) and source_path.exists():
                try:
                    mount_path = f"/api/v2/plugins/{plugin.name}/static"
                    typer.secho(
                        f"   Mounting {plugin.name} assets: {mount_path} -> {source_path}", fg=typer.colors.BRIGHT_BLACK)
                    app.mount(mount_path, StaticFiles(directory=str(
                        source_path)), name=f"plugin_static_{plugin.name}")
                except Exception as e:
                    logger.error(
                        f"Failed to mount static assets for {plugin.name}: {e}")

        # Initialize all discovered plugins
        initialize_all_plugins()

        # Phase 3: Register transport routes (if provided)
        _log_section("Transports")
        for plugin in loaded_plugins:
            if hasattr(plugin, "register_transport"):
                try:
                    plugin.register_transport(app)
                except Exception as exc:
                    logger.error(
                        "Failed to register transport for %s: %s",
                        plugin.name,
                        exc,
                    )

        if enable_mcp and mcp_transport == "plugin":
            if not any(p.name == "embeddr-transport-mcp" for p in loaded_plugins):
                raise RuntimeError(
                    "MCP transport plugin not installed. Install embeddr-transport-mcp "
                    "or use --mcp-transport embedded."
                )
            typer.secho(
                "MCP transport: plugin (embeddr-transport-mcp)",
                fg=typer.colors.GREEN,
            )
    else:
        typer.secho("🚫 Plugins disabled by flag.", fg=typer.colors.YELLOW)
        logger.info("Plugins disabled by EMBEDDR_NO_PLUGINS")

    # Serve Static Files (Frontend)
    if os.path.exists(FRONTEND_DIR):
        assets_dir = os.path.join(FRONTEND_DIR, "assets")
        if os.path.exists(assets_dir):
            app.mount("/assets", StaticFiles(directory=assets_dir),
                      name="assets")

        # Catch-all route for SPA (Single Page Application)
        @app.get("/{full_path:path}")
        async def serve_spa(full_path: str):
            # Prevent the catch-all from hijacking API requests that didn't match
            if full_path.startswith("api/"):
                return JSONResponse(
                    status_code=404,
                    content={"detail": f"API route not found: {full_path}"},
                )

            # Check if file exists in static dir (e.g. favicon.ico, manifest.json)
            file_path = os.path.join(FRONTEND_DIR, full_path)
            if full_path and os.path.exists(file_path) and os.path.isfile(file_path):
                return FileResponse(file_path)

            # Otherwise return index.html for React Router to handle
            return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))

    else:
        logger.warning(
            f"Frontend directory not found at {FRONTEND_DIR}. WebUI will not be available."
        )

        @app.get("/")
        def index():
            return {"message": "Embeddr API is running. Frontend not found."}

    return app


def register(app: typer.Typer):
    @app.command()
    def serve(
        host: str = typer.Option("127.0.0.1", help="The host to bind to."),
        port: int = typer.Option(8003, help="The port to bind to."),
        reload: bool = typer.Option(False, help="Enable auto-reload."),
        dev_origins: bool = typer.Option(
            False, help="Enable development CORS origins."
        ),
        mcp: bool = typer.Option(False, help="Enable MCP transport plugin."),
        mcp_transport: Optional[str] = typer.Option(
            None,
            help="MCP transport: plugin | disabled",
        ),
        docs: bool = typer.Option(False, help="Enable API docs."),
        verbose: bool = typer.Option(
            False, help="Enable verbose startup logs."),
        no_plugins: bool = typer.Option(
            False, help="Disable all plugin loading."),
        plugins_dir: str = typer.Option(
            None, help="Directory to serve plugins from."),
    ):
        """
        Start the Embeddr Local API server.
        """
        # Set environment variables for the app to use in lifespan
        os.environ["EMBEDDR_HOST"] = host
        os.environ["EMBEDDR_PORT"] = str(port)
        if mcp_transport is None:
            if mcp:
                mcp_transport = "plugin"
            else:
                try:
                    with Session(get_engine()) as session:
                        cfg = resolve_plugin_config(
                            session=session,
                            plugin_name="embeddr-core",
                            config_id="embeddr-core.mcp.transport",
                            scope="global",
                            scope_id=None,
                        )
                    enabled = bool(cfg.get("enabled", True))
                    transport = str(cfg.get("transport", "embedded")).lower()
                    if not enabled:
                        mcp_transport = "disabled"
                    elif transport in {"embedded", "plugin", "disabled"}:
                        mcp_transport = transport
                    else:
                        mcp_transport = "embedded"
                except Exception:
                    mcp_transport = "disabled"

        if mcp_transport == "embedded":
            typer.secho(
                "Embedded MCP transport is deprecated; using plugin transport instead.",
                fg=typer.colors.YELLOW,
            )
            mcp_transport = "plugin"

        os.environ["EMBEDDR_VERBOSE"] = str(verbose).lower()
        setup_logging(verbose=verbose)

        os.environ["EMBEDDR_ENABLE_MCP"] = str(
            mcp_transport != "disabled").lower()
        os.environ["EMBEDDR_MCP_TRANSPORT"] = mcp_transport
        os.environ["EMBEDDR_ENABLE_DOCS"] = str(docs).lower()
        os.environ["EMBEDDR_ALLOW_DEV_ORIGINS"] = str(dev_origins).lower()
        os.environ["EMBEDDR_NO_PLUGINS"] = str(no_plugins).lower()

        if mcp and reload:
            typer.secho(
                "\n⚠️  Warning: Running MCP with reload enabled may cause connection issues.",
                fg=typer.colors.YELLOW,
            )

        # Check if data directory exists
        data_dir_env = os.environ.get("EMBEDDR_DATA_DIR")
        if data_dir_env:
            data_path = Path(data_dir_env)
        else:
            data_path = get_data_dir()

        if not data_path.exists():
            typer.secho(
                f"\n⚠️  Data directory not found at: {data_path}", fg=typer.colors.YELLOW
            )
            if not typer.confirm("   Do you want to create it?"):
                typer.echo("Aborting.")
                raise typer.Exit()

            # Create it now
            try:
                data_path.mkdir(parents=True, exist_ok=True)
                typer.secho(
                    f"   Created data directory at: {data_path}\n",
                    fg=typer.colors.GREEN,
                )
            except Exception as e:
                typer.secho(
                    f"   Failed to create data directory: {e}", fg=typer.colors.RED
                )
                raise typer.Exit(1)

        if plugins_dir:
            os.environ["EMBEDDR_PLUGINS_DIR"] = str(
                Path(plugins_dir).resolve())
        elif not os.environ.get("EMBEDDR_PLUGINS_DIR") and not os.environ.get("EMBEDDR_PLUGIN_DIR"):
            # Default to data_dir/plugins ONLY if not already set by env
            # We already resolved data_path above
            base_dir = data_path

            plugins_path = base_dir / "plugins"
            # Create plugins directory if it doesn't exist
            plugins_path.mkdir(parents=True, exist_ok=True)
            os.environ["EMBEDDR_PLUGINS_DIR"] = str(plugins_path)

        log_level = "info" if verbose else "warning"

        if reload:
            # When reloading, we can't pass the app instance directly
            # We need to pass the import string.
            # However, factory=True allows us to pass arguments to the factory function
            # But uvicorn.run with factory=True and reload=True is tricky with arguments
            # So we'll use an environment variable to pass the mcp flag if needed
            uvicorn.run(
                "embeddr.commands.serve:create_app_factory",
                host=host,
                port=port,
                reload=reload,
                factory=True,
                log_level=log_level,
                ws="websockets-sansio",
            )
        else:
            uvicorn.run(
                create_app(
                    enable_mcp=mcp,
                    enable_docs=docs,
                    no_plugins=no_plugins,
                    mcp_transport=mcp_transport,
                ),
                host=host,
                port=port,
                log_level=log_level,
                ws="websockets-sansio",
            )


def create_app_factory() -> FastAPI:
    """Factory function for uvicorn reload mode"""
    setup_logging()
    enable_mcp = os.environ.get(
        "EMBEDDR_ENABLE_MCP", "false").lower() == "true"
    mcp_transport = os.environ.get(
        "EMBEDDR_MCP_TRANSPORT", "disabled").lower()
    enable_docs = os.environ.get(
        "EMBEDDR_ENABLE_DOCS", "false").lower() == "true"
    no_plugins = os.environ.get(
        "EMBEDDR_NO_PLUGINS", "false").lower() == "true"
    return create_app(
        enable_mcp=enable_mcp,
        mcp_transport=mcp_transport,
        enable_docs=enable_docs,
        no_plugins=no_plugins
    )
