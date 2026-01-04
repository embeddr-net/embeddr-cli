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
from embeddr.core.plugin_loader import load_python_plugins
from embeddr.services.socket_manager import monitor_comfy_events

# Internal imports - now from the embeddr package
from embeddr.db.session import create_db_and_tables
from embeddr.mcp.server import mcp

# Add embeddr-core to path if it exists as a sibling
# From src/embeddr/commands/serve.py, parents[4] is the 'public' directory
PACKAGE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FRONTEND_DIR = PACKAGE_DIR / "web"

core_path = Path(__file__).resolve().parents[4] / "embeddr-core" / "src"
if core_path.exists():
    sys.path.append(str(core_path))

try:
    from embeddr_core.services.embedding import unload_model
except ImportError:

    def unload_model():
        pass


logger = logging.getLogger("embeddr.local")
setup_logging()
logger.info("Embeddr Local is starting up...")

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

    # Start ComfyUI WebSocket monitor
    asyncio.create_task(monitor_comfy_events())

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
            f"   📚 API Docs:  http://{display_host}:{port}/api/v1/docs",
            fg=typer.colors.MAGENTA,
        )

    typer.echo("   " + "-" * 45)
    typer.secho("   Press Ctrl+C to stop server\n",
                fg=typer.colors.BRIGHT_BLACK)

    # Manage MCP lifespan if enabled
    if hasattr(app.state, "mcp_app") and app.state.mcp_app:
        async with app.state.mcp_app.router.lifespan_context(app.state.mcp_app):
            yield
    else:
        yield

    # Cleanup resources
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
    enable_mcp: bool = False, enable_docs: bool = False, enable_comfy: bool = False
) -> FastAPI:
    app = FastAPI(
        title="Embeddr Local",
        lifespan=lifespan,
        docs_url="/api/v1/docs" if enable_docs else None,
        redoc_url="/api/v1/redoc" if enable_docs else None,
        openapi_url="/api/v1/openapi.json" if enable_docs else None,
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
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
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

    # Serve Plugins Directory
    plugins_dir = os.environ.get("EMBEDDR_PLUGINS_DIR")
    if not plugins_dir:
        plugins_dir = str(Path.cwd() / "plugins")

    if os.path.exists(plugins_dir):
        app.mount("/plugins", StaticFiles(directory=plugins_dir), name="plugins")
        logger.info(f"Serving plugins from {plugins_dir}")
        # Load Python plugins
        load_python_plugins(Path(plugins_dir), app)
    else:
        logger.info(f"Plugins directory not found at {plugins_dir}")

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
        else:
            # Default to data_dir/plugins
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
                create_app(enable_mcp=mcp, enable_docs=docs,
                           enable_comfy=comfy),
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
    return create_app(
        enable_mcp=enable_mcp, enable_docs=enable_docs, enable_comfy=enable_comfy
    )
