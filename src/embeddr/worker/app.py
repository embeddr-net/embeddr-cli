"""
Worker FastAPI app — lightweight headless instance for processing jobs.

Unlike the main create_app(), this does NOT mount:
- Security routes (workers authenticate via key to main)
- UI/SPA serving
- WebSocket manager for browser clients

It DOES mount:
- Health check endpoint
- Plugin loading (for job execution capabilities)
- Plugin-registered service endpoints (via WorkerServiceRegistry)
- Worker WebSocket client (connects to main)

The worker app is **plugin-agnostic**: it knows nothing about embeddings,
OCR, TTS, or any other domain-specific logic.  Plugins register their own
HTTP endpoints and job handlers; the worker just wires them up.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path

import typer
from fastapi import FastAPI

from embeddr.worker.client import WorkerClient, WorkerConfig
from embeddr.worker.service_registry import WorkerServiceRegistry

logger = logging.getLogger("embeddr.worker.app")


@asynccontextmanager
async def worker_lifespan(app: FastAPI):
    """Lifespan for the worker app — connect to main, load plugins, run jobs."""

    # Workers are stateless job executors — they don't need their own database.
    # Jobs are dispatched from the main instance with all necessary data,
    # and results are sent back over the WebSocket connection.

    # Load plugins to discover job capabilities
    capabilities: list[str] = []
    plugin_names: list[str] = []

    no_plugins = os.environ.get(
        "EMBEDDR_NO_PLUGINS", "false").lower() == "true"
    if not no_plugins:
        try:
            from embeddr.core.plugin_loader import (
                load_python_plugins,
                get_all_plugin_instances,
                initialize_all_plugins,
                startup_all_plugins,
            )
            from embeddr.lotus.core_caps import register_core_lotus_capabilities

            register_core_lotus_capabilities()

            # Load plugins from configured directories
            plugin_paths = []
            env_plugin_dir = os.environ.get(
                "EMBEDDR_PLUGINS_DIR") or os.environ.get("EMBEDDR_PLUGIN_DIR")
            if env_plugin_dir:
                plugin_paths.append(Path(env_plugin_dir))

            for p_path in plugin_paths:
                if p_path.exists():
                    logger.info("[worker] Loading plugins from %s", p_path)
                    load_python_plugins(p_path, app=app)

            logger.info("[worker] Initializing plugins...")
            initialize_all_plugins()

            logger.info("[worker] Starting up plugins...")
            startup_all_plugins()

            # Collect capabilities from loaded plugins
            for plugin in get_all_plugin_instances():
                plugin_names.append(plugin.name)
                caps = []
                if hasattr(plugin, "job_capabilities"):
                    caps = plugin.job_capabilities
                    capabilities.extend(caps)
                logger.info(
                    "[worker] Plugin loaded: %s (v%s) capabilities=%s",
                    plugin.name,
                    getattr(plugin, 'version', '?'),
                    caps or 'none',
                )

            typer.secho(
                f"   Loaded {len(plugin_names)} plugins with {len(capabilities)} job capabilities",
                fg=typer.colors.CYAN,
            )
        except Exception as e:
            logger.error("[worker] Failed to load plugins: %s",
                         e, exc_info=True)

    # Collect service endpoints registered by plugins and mount them
    svc_registry = WorkerServiceRegistry.get()
    n_routes = svc_registry.mount_routes(app)
    if n_routes:
        logger.info("[worker] Mounted %d plugin service endpoint(s)", n_routes)

    svc_manifest = svc_registry.get_service_manifest()
    if svc_manifest:
        typer.secho(
            f"   Service endpoints: {', '.join(ep['path'] for ep in svc_manifest.values())}",
            fg=typer.colors.CYAN,
        )

    # Build worker config from env vars
    config = WorkerConfig.from_env()

    # Create the job handler that routes dispatched jobs to ExecutionSpine handlers
    async def _worker_job_handler(
        job_id: str,
        data: dict,
        report_progress,
    ):
        """Route incoming worker jobs to the correct ExecutionSpine handler."""
        from embeddr.core.execution_spine import ExecutionSpine

        job_type = data.get("capability") or data.get("job_type", "")
        inputs = data.get("input") or data.get("inputs", {})

        handler = ExecutionSpine._handlers.get(job_type)
        if not handler:
            logger.error(
                "[worker] No handler for job type '%s' — "
                "registered handlers: %s",
                job_type, list(ExecutionSpine._handlers.keys()),
            )
            return None

        items = inputs.get("items", [])
        art_id = inputs.get("artifact_id", "")
        logger.info(
            "[worker] Executing job %s type=%s items=%d artifact_id=%s",
            job_id, job_type, len(items), art_id or "(none)",
        )
        t0 = time.monotonic()
        await report_progress(job_id, 0.1, "executing")

        # Run the (likely synchronous) handler in a thread so we don't block the event loop
        import functools
        result = await asyncio.get_event_loop().run_in_executor(
            None,
            functools.partial(handler, None, inputs),
        )

        elapsed = time.monotonic() - t0
        count = 0
        if isinstance(result, dict):
            count = result.get("embeddings_count", len(
                result.get("embeddings", [])))
        logger.info(
            "[worker] Job %s completed in %.1fs — embeddings_count=%d",
            job_id, elapsed, count,
        )
        await report_progress(job_id, 1.0, "complete")
        return result

    # Create and configure the worker client
    worker_client = WorkerClient(
        config=config, job_handler=_worker_job_handler)
    worker_client.state.capabilities = capabilities
    worker_client.state.plugins = plugin_names
    worker_client.state.services = svc_manifest

    # Store client on app for access from health endpoint
    app.state.worker_client = worker_client

    # Start the worker client connection in background
    client_task = asyncio.create_task(worker_client.start())

    typer.secho(
        f"\n🔧 Worker '{config.worker_name}' started",
        fg=typer.colors.GREEN,
        bold=True,
    )
    typer.secho(
        f"   Tags: {', '.join(config.tags) or 'none'}", fg=typer.colors.BRIGHT_BLACK)
    typer.secho(
        f"   Capabilities: {', '.join(capabilities) or 'none'}", fg=typer.colors.BRIGHT_BLACK)
    typer.secho(
        f"   Max concurrent jobs: {config.max_concurrent_jobs}", fg=typer.colors.BRIGHT_BLACK)
    typer.secho("   Press Ctrl+C to stop worker\n",
                fg=typer.colors.BRIGHT_BLACK)

    try:
        yield
    finally:
        # Graceful shutdown
        await worker_client.stop()
        client_task.cancel()
        try:
            await client_task
        except asyncio.CancelledError:
            pass

        logger.info("Worker shutdown complete")


def create_worker_app(
    enable_docs: bool = False,
    no_plugins: bool = False,
) -> FastAPI:
    """Create the lightweight worker FastAPI app."""
    app = FastAPI(
        title="Embeddr Worker",
        lifespan=worker_lifespan,
        docs_url="/api/docs" if enable_docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if enable_docs else None,
    )

    @app.get("/health")
    async def health():
        """Health check endpoint for monitoring."""
        client: WorkerClient = app.state.worker_client
        state = client.state
        health_data = client.health.collect()

        return {
            "status": "healthy" if state.connected else "disconnected",
            "worker_name": client.config.worker_name,
            "connected": state.connected,
            "registered": state.registered,
            "draining": state.draining,
            "active_jobs": len(state.active_jobs),
            "capabilities": state.capabilities,
            "plugins": state.plugins,
            "tags": client.config.tags,
            "metrics": health_data,
        }

    @app.get("/")
    async def root():
        return {
            "mode": "worker",
            "message": "Embeddr Worker Node. Use /health for status.",
        }

    # ── Plugin-provided service endpoints ──────────────────────────────
    # Plugins register their own HTTP handlers with the WorkerServiceRegistry
    # during on_load() which runs inside the lifespan.  We cannot mount
    # routes here (the registry is still empty) — mount_routes() is called
    # in the lifespan after plugins have loaded.

    return app
