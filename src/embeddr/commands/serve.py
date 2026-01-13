from embeddr_core.plugin_interface import EmbeddrEvent
from embeddr.mcp.server import mcp, register_plugin_tools
from embeddr.db.session import create_db_and_tables
import logging
import os
import sys
import warnings
import asyncio
from contextlib import asynccontextmanager
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
from embeddr.services.socket_manager import monitor_comfy_events, manager

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
setup_logging()
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Retrieve config from env (set by serve command)
    host = os.environ.get("EMBEDDR_HOST", "127.0.0.1")
    port = os.environ.get("EMBEDDR_PORT", "8003")
    mcp_enabled = os.environ.get(
        "EMBEDDR_ENABLE_MCP", "false").lower() == "true"
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

    _EVENT_BUS.subscribe("*", broadcast_to_frontend)

    # Register MCP tools from plugins
    if mcp_enabled:
        try:
            register_plugin_tools()
        except Exception as e:
            logger.error(f"Failed to register plugin MCP tools: {e}")

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
            # We wrap the broadcast in a task on the main loop
            asyncio.run_coroutine_threadsafe(
                manager.broadcast_event(
                    # Maps to "plugin:event_type" in frontend
                    f"plugin:{event.event_type}",
                    event.model_dump()
                ),
                loop
            )
        except Exception as e:
            logger.error(f"Bridge Error: {e}")

    # Subscribe to relevant events
    # (In V3 we will add wildcard subscription)
    _EVENT_BUS.subscribe("embeddings.generated", bridge_event_to_ws)
    _EVENT_BUS.subscribe("artifact.created", bridge_event_to_ws)
    _EVENT_BUS.subscribe("scan.started", bridge_event_to_ws)
    _EVENT_BUS.subscribe("scan.completed", bridge_event_to_ws)
    _EVENT_BUS.subscribe("scan.failed", bridge_event_to_ws)

    # Start ComfyUI WebSocket monitor
    # REMOVED: Managed by embeddr-comfyui plugin
    # asyncio.create_task(monitor_comfy_events())

    # Start Automation Manager (V1 - Legacy/Core Analysis)
    # this handles the Auto-Analysis plugins (embedding, captioning etc)
    # automation_manager.py logic needs to be robust against stress.
    try:
        from embeddr.services.automation_manager import automation_manager
        from embeddr.services.ingestion_service import ingestion_service

        # Start Ingestion Service (Bounded Queue)
        await ingestion_service.start()

        # Ensure V1 also uses a dedicated connection if possible, or assume we fix it in the file.
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
        for plugin in loaded_plugins:
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

    typer.secho("\n✨ Embeddr Local API has started!",
                fg=typer.colors.GREEN, bold=True)
    typer.echo("   " + "-" * 45)
    typer.secho(
        f"   👉 Web UI:    http://{display_host}:{port}", fg=typer.colors.CYAN)

    if mcp_enabled:
        typer.secho(
            f"   🔌 MCP SSE:   http://{display_host}:{port}/mcp/messages",
            fg=typer.colors.YELLOW,
        )

    if docs_enabled:
        typer.secho(
            f"   📚 API Docs:  http://{display_host}:{port}/api/docs",
            fg=typer.colors.MAGENTA,
        )

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

    # Manage MCP lifespan if enabled
    if hasattr(app.state, "mcp_app") and app.state.mcp_app:
        async with app.state.mcp_app.router.lifespan_context(app.state.mcp_app):
            yield
    else:
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
    ]


def dynamic_origins() -> list[str]:
    host = os.environ.get("EMBEDDR_HOST", "127.0.0.1")
    port = os.environ.get("EMBEDDR_PORT", "8003")
    return [f"http://{host}:{port}"]


def comfy_origins() -> list[str]:
    logger.info("Loading ComfyUI CORS origins...")
    return [
        "http://localhost:8188",  # ComfyUI default
        "http://127.0.0.1:8188",  # ComfyUI default
    ]


def create_app(
    enable_mcp: bool = False,
    enable_docs: bool = False,
    enable_comfy: bool = False,
    no_plugins: bool = False
) -> FastAPI:
    app = FastAPI(
        title="Embeddr Local",
        lifespan=lifespan,
        docs_url="/api/docs" if enable_docs else None,
        redoc_url="/api/redoc" if enable_docs else None,
        openapi_url="/api/openapi.json" if enable_docs else None,
    )

    port = os.environ.get("EMBEDDR_PORT", "8003")
    allowed_origins: set[str] = set(default_local_origins(port))
    allowed_origins |= set(parse_env_origins())
    allowed_origins |= set(dynamic_origins())
    if enable_comfy:
        allowed_origins |= set(comfy_origins())
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

    # Store MCP app in state if enabled
    if enable_mcp:
        mcp_app = mcp.http_app(transport="streamable-http", path="/messages")
        app.state.mcp_app = mcp_app
        # Mount MCP Server
        # This exposes the MCP server over HTTP (Streamable) at /mcp/messages
        app.mount("/mcp", mcp_app)
    else:
        app.state.mcp_app = None

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
    app.include_router(artifacts_v2.router,
                       prefix="/api/v2/artifacts", tags=["artifacts"])
    app.include_router(plugins_v2.router,
                       prefix="/api/v2/plugins", tags=["plugins"])
    app.include_router(system_v2.router,
                       prefix="/api/v2/system", tags=["system"])
    app.include_router(collections_v2.router,
                       prefix="/api/v2/collections", tags=["collections"])
    app.include_router(executions_v2.router,
                       prefix="/api/v2/executions", tags=["executions"])
    app.include_router(workflows_v2.router,
                       prefix="/api/v2/workflows", tags=["workflows"])
    app.include_router(config_v2.router,
                       prefix="/api/v2/config", tags=["config"])
    app.include_router(maintenance_v2.router,
                       prefix="/api/v2/maintenance", tags=["maintenance"])
    # app.include_router(projections_v2.router,
    #                    prefix="/api/v2/projections", tags=["projections"])

    # Serve Plugins Directory
    if not no_plugins:
        plugin_paths = []

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
        # We are in embeddr-cli/src/embeddr/commands/serve.py -> parents[3] is embeddr-cli
        cli_root = Path(__file__).resolve().parents[3]
        repo_root = cli_root.parent

        # 2a. Dist Plugins (Compiled Frontend) - High Priority
        dist_plugins_src = repo_root / "embeddr-plugins" / "dist-plugins"
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
        plugin_paths.append(Path.cwd() / "plugins")

        # Phase 1: Load all plugins (Python Logic + API)
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
        mcp: bool = typer.Option(False, help="Enable MCP server."),
        comfy: bool = typer.Option(False, help="Enable ComfyUI integration."),
        docs: bool = typer.Option(False, help="Enable API docs."),
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
        os.environ["EMBEDDR_ENABLE_MCP"] = str(mcp).lower()
        os.environ["EMBEDDR_ENABLE_COMFY"] = str(comfy).lower()
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
                log_level="warning",
            )
        else:
            uvicorn.run(
                create_app(
                    enable_mcp=mcp,
                    enable_docs=docs,
                    enable_comfy=comfy,
                    no_plugins=no_plugins
                ),
                host=host,
                port=port,
                log_level="warning",
            )


def create_app_factory() -> FastAPI:
    """Factory function for uvicorn reload mode"""
    enable_mcp = os.environ.get(
        "EMBEDDR_ENABLE_MCP", "false").lower() == "true"
    enable_docs = os.environ.get(
        "EMBEDDR_ENABLE_DOCS", "false").lower() == "true"
    enable_comfy = os.environ.get(
        "EMBEDDR_ENABLE_COMFY", "false").lower() == "true"
    no_plugins = os.environ.get(
        "EMBEDDR_NO_PLUGINS", "false").lower() == "true"
    return create_app(
        enable_mcp=enable_mcp,
        enable_docs=enable_docs,
        enable_comfy=enable_comfy,
        no_plugins=no_plugins
    )
