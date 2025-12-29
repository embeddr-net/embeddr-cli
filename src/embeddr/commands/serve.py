import typer
import uvicorn
from pathlib import Path
import os
import sys
import logging
import warnings
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Internal imports - now from the embeddr package
from embeddr.db.session import create_db_and_tables
from embeddr.api import routes
from embeddr.core.logging_utils import setup_logging
from embeddr.mcp.server import mcp

# Add embeddr-core to path if it exists as a sibling
# From src/embeddr/commands/serve.py, parents[4] is the 'public' directory
PACKAGE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FRONTEND_DIR = PACKAGE_DIR / "web"

core_path = Path(__file__).resolve().parents[4] / "embeddr-core" / "src"
if core_path.exists():
    sys.path.append(str(core_path))


logger = logging.getLogger("embeddr.local")
setup_logging()
logger.info("Embeddr Local is starting up...")

# Suppress websockets deprecation warnings
warnings.filterwarnings("ignore", category=DeprecationWarning, module="uvicorn")
warnings.filterwarnings("ignore", category=DeprecationWarning, module="websockets")

# Define the path to the frontend build directory
# Root is parents[3] (embeddr-local-api)
ROOT_DIR = Path(__file__).resolve().parents[3]
FRONTEND_DIR = Path(os.environ.get("EMBEDDR_FRONTEND_DIR", DEFAULT_FRONTEND_DIR))

# Create MCP App globally to access its lifespan
# mcp_app = mcp.http_app(transport="http", path="/messages")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Retrieve config from env (set by serve command)
    host = os.environ.get("EMBEDDR_HOST", "127.0.0.1")
    port = os.environ.get("EMBEDDR_PORT", "8003")
    mcp_enabled = os.environ.get("EMBEDDR_ENABLE_MCP", "false").lower() == "true"
    docs_enabled = os.environ.get("EMBEDDR_ENABLE_DOCS", "false").lower() == "true"

    display_host = "127.0.0.1" if host == "0.0.0.0" else host

    # Initialize DB, load models, etc.
    create_db_and_tables()

    typer.secho("\n✨ Embeddr Local API has started!", fg=typer.colors.GREEN, bold=True)
    typer.echo("   " + "-" * 45)
    typer.secho(f"   👉 Web UI:    http://{display_host}:{port}", fg=typer.colors.CYAN)

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
    typer.secho("   Press Ctrl+C to stop server\n", fg=typer.colors.BRIGHT_BLACK)

    # Manage MCP lifespan if enabled
    if hasattr(app.state, "mcp_app") and app.state.mcp_app:
        async with app.state.mcp_app.router.lifespan_context(app.state.mcp_app):
            yield
    else:
        yield

    logger.info("Embeddr Local is shutting down...")


def create_app(enable_mcp: bool = False, enable_docs: bool = False) -> FastAPI:
    app = FastAPI(
        title="Embeddr Local",
        lifespan=lifespan,
        docs_url="/api/v1/docs" if enable_docs else None,
        redoc_url="/api/v1/redoc" if enable_docs else None,
        openapi_url="/api/v1/openapi.json" if enable_docs else None,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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

    # Serve Static Files (Frontend)
    if os.path.exists(FRONTEND_DIR):
        assets_dir = os.path.join(FRONTEND_DIR, "assets")
        if os.path.exists(assets_dir):
            app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

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
        mcp: bool = typer.Option(False, help="Enable MCP server."),
        docs: bool = typer.Option(False, help="Enable API docs."),
    ):
        """
        Start the Embeddr Local API server.
        """
        # Set environment variables for the app to use in lifespan
        os.environ["EMBEDDR_HOST"] = host
        os.environ["EMBEDDR_PORT"] = str(port)
        os.environ["EMBEDDR_ENABLE_MCP"] = str(mcp).lower()
        os.environ["EMBEDDR_ENABLE_DOCS"] = str(docs).lower()

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
                create_app(enable_mcp=mcp, enable_docs=docs),
                host=host,
                port=port,
                log_level="warning",
            )


def create_app_factory() -> FastAPI:
    """Factory function for uvicorn reload mode"""
    enable_mcp = os.environ.get("EMBEDDR_ENABLE_MCP", "false").lower() == "true"
    enable_docs = os.environ.get("EMBEDDR_ENABLE_DOCS", "false").lower() == "true"
    return create_app(enable_mcp=enable_mcp, enable_docs=enable_docs)
