from embeddr.lotus.core_caps import register_core_lotus_capabilities
from embeddr_core.plugin_interface import EmbeddrEvent
from embeddr.db.session import create_db_and_tables, get_engine
import logging
import os
import shutil
import subprocess
import sys
import warnings
import asyncio
from contextlib import asynccontextmanager, AsyncExitStack
from pathlib import Path
from typing import Dict, Set

import typer
import uvicorn
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from embeddr.api import routes
from embeddr.api import websocket as websocket_routes
from embeddr.api.security import get_api_key, check_auth_enabled
from embeddr.services import auth_service
from embeddr_core.models.user_account import UserAccount
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
from embeddr.runtime.route_registry import build_route_registrations

# Import and load local FS scanner plugin manually for now until dynamic loading is robust
# This ensures it's available on startup
import importlib.util


# Internal imports - now from the embeddr package

# Add embeddr-core to path if it exists as a sibling
# From src/embeddr/commands/serve.py, parents[4] is the 'public' directory
ROOT_DIR = Path(__file__).resolve().parents[3]
PACKAGE_DIR = Path(__file__).resolve().parents[1]
DEFAULT_FRONTEND_DIR = PACKAGE_DIR / "web"
DEFAULT_WEB_SOURCE_DIR = ROOT_DIR.parent / "embeddr-frontend"
DEFAULT_WEB_DIST_DIR = DEFAULT_WEB_SOURCE_DIR / "dist"

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
FRONTEND_DIR = Path(os.environ.get(
    "EMBEDDR_FRONTEND_DIR", DEFAULT_FRONTEND_DIR))


def _log_section(title: str) -> None:
    typer.secho(f"\n== {title} ==", fg=typer.colors.CYAN)


def _format_transport_link(display_host: str, port: str, link: str) -> str:
    if link.startswith("http://") or link.startswith("https://"):
        return link
    return f"http://{display_host}:{port}{link}"


def _bootstrap_default_admin() -> None:
    if not check_auth_enabled():
        return

    auth_mode = auth_service.get_auth_mode()
    if auth_mode not in {"db", "single", "multi"}:
        return

    operator_name = os.environ.get("EMBEDDR_DEFAULT_OPERATOR_NAME", "user")
    admin_username = os.environ.get("EMBEDDR_DEFAULT_ADMIN_USERNAME", "user")

    with Session(get_engine()) as session:
        result = auth_service.ensure_default_admin(
            session,
            mode=auth_mode,
            operator_name=operator_name,
            admin_username=admin_username,
        )

        if not result:
            result_data = None
        else:
            result_data = {
                "root_username": result.root_user.username,
                "root_key": result.root_key,
                "user_operator": result.user_operator_name,
                "user_username": result.user_username,
                "user_key": result.user_key,
            }

    if not result_data:
        typer.secho(
            "Default admin already exists; skipping bootstrap.",
            fg=typer.colors.BRIGHT_BLACK,
        )
        return

    typer.secho(
        "🚨 DESTRUCTIVE: Bootstrapped root operator",
        fg=typer.colors.YELLOW,
    )
    typer.secho(
        f"   Root Username: {result_data['root_username']}",
        fg=typer.colors.BRIGHT_YELLOW,
    )
    typer.secho(
        f"   Root Key: {result_data['root_key']}",
        fg=typer.colors.BRIGHT_YELLOW,
    )
    typer.secho(
        "   Store this key securely. It will not be shown again.",
        fg=typer.colors.BRIGHT_BLACK,
    )

    if result_data.get("user_key"):
        typer.secho(
            "🚨 DESTRUCTIVE: Bootstrapped single-user operator",
            fg=typer.colors.YELLOW,
        )
        typer.secho(
            f"   Operator: {result_data['user_operator']}",
            fg=typer.colors.BRIGHT_YELLOW,
        )
        typer.secho(
            f"   Username: {result_data['user_username']}",
            fg=typer.colors.BRIGHT_YELLOW,
        )
        typer.secho(
            f"   Client Key: {result_data['user_key']}",
            fg=typer.colors.BRIGHT_YELLOW,
        )
        typer.secho(
            "   Store this key securely. It will not be shown again.",
            fg=typer.colors.BRIGHT_BLACK,
        )


def _maybe_print_dev_admin_key() -> None:
    if not check_auth_enabled():
        return

    enabled = os.environ.get("EMBEDDR_DEV_SHOW_ADMIN_KEY", "").strip().lower()
    if enabled not in {"1", "true", "yes", "y"}:
        return

    confirm = os.environ.get(
        "EMBEDDR_DEV_SHOW_ADMIN_KEY_CONFIRM", ""
    ).strip().lower()
    if confirm not in {"1", "true", "yes", "y"}:
        typer.secho(
            "DEV ADMIN KEY: Confirmation missing. Set EMBEDDR_DEV_SHOW_ADMIN_KEY_CONFIRM=true.",
            fg=typer.colors.YELLOW,
        )
        return

    with Session(get_engine()) as session:
        admin_user = session.exec(
            select(UserAccount).where(UserAccount.is_admin == True)
        ).first()

        if not admin_user:
            typer.secho(
                "DEV ADMIN KEY: No admin user found.", fg=typer.colors.YELLOW
            )
            return

        api_key, raw_key = auth_service.create_api_key(
            session,
            user_id=admin_user.id,
            operator_id=admin_user.operator_id,
            name="dev-admin",
            scopes=["*"],
        )
        username_value = admin_user.username

    typer.secho(
        "🚨 DESTRUCTIVE: Created new admin API key for dev use",
        fg=typer.colors.YELLOW,
    )
    typer.secho(f"   Username: {username_value}",
                fg=typer.colors.BRIGHT_YELLOW)
    typer.secho(f"   Client Key: {raw_key}", fg=typer.colors.BRIGHT_YELLOW)
    typer.secho(
        "   Store it securely. This action creates a new key in the database.",
        fg=typer.colors.BRIGHT_BLACK,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Retrieve config from env (set by serve command)
    host = os.environ.get("EMBEDDR_HOST", "127.0.0.1")
    port = os.environ.get("EMBEDDR_PORT", "8003")
    docs_enabled = os.environ.get(
        "EMBEDDR_ENABLE_DOCS", "false").lower() == "true"

    display_host = "127.0.0.1" if host == "0.0.0.0" else host

    # Initialize DB, load models, etc.
    create_db_and_tables()
    _bootstrap_default_admin()
    _maybe_print_dev_admin_key()

    # Plugin loading is handled in create_app to ensure API registration works
    # Here we just trigger startup hooks

    # Trigger Plugin on_startup hooks
    startup_all_plugins()

    # Start Ingestion Service
    from embeddr.services.ingestion_service import ingestion_service
    await ingestion_service.start()

    # Transport tool registration is handled by transport plugins

    # --- Event Bus <-> WebSocket Bridge ---
    loop = asyncio.get_running_loop()

    def _as_set(value):
        if value is None:
            return set()
        if isinstance(value, (list, tuple, set)):
            return {str(v) for v in value if v}
        return {str(value)}

    def _load_execution_scope(execution_id: str):
        try:
            from uuid import UUID
            from embeddr_core.models.artifact_execution import ArtifactExecution

            execution_uuid = UUID(str(execution_id))
            with Session(get_engine()) as session:
                ex = session.get(ArtifactExecution, execution_uuid)
                if not ex:
                    return {}
                tags = ex.tags or {}
                return {
                    "client_ids": _as_set(tags.get("target_client_id") or tags.get("client_id")),
                    "operator_ids": _as_set(ex.operator_id),
                    "api_key_ids": _as_set(ex.api_key_id),
                }
        except Exception:
            return {}

    def _load_artifact_scope(artifact_id: str):
        try:
            from uuid import UUID
            from embeddr_core.models.artifact import Artifact

            artifact_uuid = UUID(str(artifact_id))
            with Session(get_engine()) as session:
                artifact = session.get(Artifact, artifact_uuid)
                if not artifact:
                    return {}
                return {
                    "user_ids": _as_set(artifact.owner_user_id),
                    "operator_ids": _as_set(artifact.owner_operator_id),
                }
        except Exception:
            return {}

    def _derive_audience(event_type: str, payload):
        if not isinstance(payload, dict):
            return None

        client_ids: Set[str] = set()
        user_ids: Set[str] = set()
        operator_ids: Set[str] = set()
        api_key_ids: Set[str] = set()

        client_ids.update(
            _as_set(payload.get("target_client_id")
                    or payload.get("target_client_ids"))
        )
        user_ids.update(
            _as_set(
                payload.get("target_user_id")
                or payload.get("target_user_ids")
                or payload.get("owner_user_id")
                or payload.get("user_id")
            )
        )
        operator_ids.update(
            _as_set(
                payload.get("target_operator_id")
                or payload.get("target_operator_ids")
                or payload.get("owner_operator_id")
                or payload.get("operator_id")
            )
        )
        api_key_ids.update(
            _as_set(
                payload.get("target_api_key_id")
                or payload.get("target_api_key_ids")
                or payload.get("api_key_id")
            )
        )

        auth_payload = payload.get("__embeddr_auth") or payload.get("auth")
        if isinstance(auth_payload, dict):
            user_ids.update(_as_set(auth_payload.get("user_id")))
            operator_ids.update(
                _as_set(auth_payload.get("operator_id"))
            )

        execution_id = payload.get("id") or payload.get("execution_id")
        if event_type.startswith("execution.") and execution_id:
            ex_scope = _load_execution_scope(str(execution_id))
            client_ids.update(ex_scope.get("client_ids", set()))
            operator_ids.update(ex_scope.get("operator_ids", set()))
            api_key_ids.update(ex_scope.get("api_key_ids", set()))

        artifact_id = payload.get("id") or payload.get("artifact_id")
        if event_type.startswith("artifact.") and artifact_id:
            artifact_scope = _load_artifact_scope(str(artifact_id))
            user_ids.update(artifact_scope.get("user_ids", set()))
            operator_ids.update(
                artifact_scope.get("operator_ids", set())
            )

        audience: Dict[str, Set[str]] = {
            "client_ids": client_ids,
            "user_ids": user_ids,
            "operator_ids": operator_ids,
            "api_key_ids": api_key_ids,
        }
        serialized_audience: Dict[str, list[str]] = {
            key: sorted(values)
            for key, values in audience.items()
            if values
        }
        return serialized_audience or None

    def bridge_event_to_ws(event):
        try:
            raw_event_type = str(event.event_type or "")
            if raw_event_type.startswith("ui:") or raw_event_type.startswith("ui."):
                logger.debug("[UI EVENT] %s payload=%s",
                             event.event_type, event.payload)
            event_type = raw_event_type
            payload = event.payload

            if event.source == "comfyui":
                if event_type.startswith("comfy."):
                    event_type = event_type.split("comfy.", 1)[1]
                if event_type == "artifact.preview":
                    event_type = "preview"
                    if isinstance(payload, dict) and payload.get("data"):
                        # Keep structured payload to preserve endpoint metadata.
                        payload = payload
            audience = _derive_audience(raw_event_type, payload)
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
                    source=event.source,
                    audience=audience,
                ),
                loop
            )

            if raw_event_type.startswith("execution."):
                # Optional: Broadcast status update explicitly if needed
                pass
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

    # Legacy AutomationManager startup removed.

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
        f"   • System Info:  http://{display_host}:{port}/api/v1/system/info",
        fg=typer.colors.BRIGHT_BLACK,
    )
    typer.secho(
        f"   • Route List:   http://{display_host}:{port}/api/v1/system/routes",
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

    # Initialize Job Runtime (official orchestration runtime)
    from embeddr.services.job_runtime import runtime
    # Start the runtime worker
    await runtime.start()

    # Subscribe to all relevant events
    _EVENT_BUS.subscribe("*", runtime.handle_event)

    # Transport lifespans (plugin-provided)
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
        "http://localhost:4173",  # SDK Dev UI / Vite preview
        "http://localhost:5173",  # Sprout / Vite default dev
        "http://localhost:5174",  # Sprout / Vite secondary dev
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
    ]


def nelumbo_origins() -> list[str]:
    raw = os.environ.get("EMBEDDR_NELUMBO_ORIGINS", "")
    if raw:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]
    return [
        "http://localhost:3005",
        "http://127.0.0.1:3005",
        "http://localhost:8898",
        "http://127.0.0.1:8898",
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


def toml_cors_origins() -> list[str]:
    """Read [cors] origins from embeddr.toml if present."""
    try:
        from embeddr.core.project import find_project_root, load_project_config
        root = find_project_root()
        if not root:
            return []
        cfg = load_project_config(root)
        cors_cfg = cfg.get("cors", {})
        raw = cors_cfg.get("origins", [])
        if isinstance(raw, list):
            return [str(o).strip() for o in raw if str(o).strip()]
        if isinstance(raw, str):
            return [o.strip() for o in raw.split(",") if o.strip()]
        return []
    except Exception as e:
        logger.warning("Failed to read [cors] from embeddr.toml: %s", e)
        return []


def _register_endpoints(app: FastAPI, router, prefix: str, tags: list[str], dependencies: list = None):
    logger.info(f"Registering router: {prefix} with tags: {tags}")
    app.include_router(router, prefix=prefix, tags=tags,
                       dependencies=dependencies)


def create_app(
    enable_docs: bool = False,
    no_plugins: bool = False,
) -> FastAPI:
    _log_section("Config")
    typer.secho(
        f"Docs: {enable_docs}",
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
    allowed_origins |= set(nelumbo_origins())

    # Read [cors] from embeddr.toml
    toml_origins = toml_cors_origins()
    if toml_origins:
        allowed_origins |= set(toml_origins)

    # Also check toml allow_dev_origins flag
    toml_dev = False
    try:
        from embeddr.core.project import find_project_root, load_project_config
        root = find_project_root()
        if root:
            cors_cfg = load_project_config(root).get("cors", {})
            toml_dev = cors_cfg.get("allow_dev_origins", False)
    except Exception:
        pass

    if os.environ.get("EMBEDDR_ALLOW_DEV_ORIGINS", "false").lower() == "true" or toml_dev:
        allowed_origins |= set(dev_origins())

    # Handle wildcard: allow_origin_regex so credentials still work
    use_wildcard = "*" in allowed_origins
    allowed_origins.discard("*")

    cors_kwargs: dict = dict(
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT",
                       "PATCH", "DELETE", "OPTIONS", "HEAD"],
        allow_headers=["Authorization", "Content-Type",
                       "ngrok-skip-browser-warning", "X-API-Key"],
    )
    if use_wildcard:
        cors_kwargs["allow_origin_regex"] = r".*"
        typer.secho("   ⚠  Wildcard (*) origin — all origins allowed",
                    fg=typer.colors.YELLOW)
    cors_kwargs["allow_origins"] = list(allowed_origins)

    app.add_middleware(CORSMiddleware, **cors_kwargs)
    typer.secho("🔐 Allowed CORS origins:", fg=typer.colors.CYAN)
    for origin in allowed_origins:
        typer.echo(f"   - {origin}")

    _log_section("Security")
    is_auth_enabled = check_auth_enabled()
    if is_auth_enabled:
        typer.secho("🔒 Authentication: ENABLED", fg=typer.colors.GREEN)
        api_dependencies = [Depends(get_api_key)]
    else:
        typer.secho(
            "🔓 Authentication: DISABLED (set EMBEDDR_API_KEY to enable)", fg=typer.colors.YELLOW)
        api_dependencies = []

    _log_section("API Routes")
    # Include WebSocket (Auth handled internally)
    app.include_router(websocket_routes.router)

    # Include API Routes
    app.include_router(routes.router, prefix="/api/v1",
                       dependencies=api_dependencies)

    route_registrations = build_route_registrations(api_dependencies)

    for router_obj, prefix, tags, dependencies in route_registrations:
        _register_endpoints(
            app,
            router_obj,
            prefix=prefix,
            tags=tags,
            dependencies=dependencies,
        )
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
        env_plugin_dir_legacy = (os.environ.get(
            "EMBEDDR_PLUGIN_DIR") or "").strip()
        if env_plugin_dir_legacy:
            plugin_paths.append(Path(env_plugin_dir_legacy))

        env_plugin_dir = (
            os.environ.get("EMBEDDR_PLUGINS_DIR")
            or os.environ.get("EMBEDDR_PLUGIN_DIR")
            or ""
        ).strip()
        if env_plugin_dir:
            # Avoid duplicate if same as above (though simplistic check)
            p = Path(env_plugin_dir)
            if not any(existing == p for existing in plugin_paths):
                plugin_paths.append(p)

        if not plugin_paths:
            data_dir = get_data_dir()
            fallback_candidates = [
                data_dir / "plugins-pack",
                data_dir / "plugins",
            ]
            for candidate in fallback_candidates:
                if candidate.exists() and not any(existing == candidate for existing in plugin_paths):
                    plugin_paths.append(candidate)

        typer.secho(
            f"   Plugin source env (EMBEDDR_PLUGINS_DIR): {(os.environ.get('EMBEDDR_PLUGINS_DIR') or '').strip() or '<unset>'}",
            fg=typer.colors.BRIGHT_BLACK,
        )
        typer.secho(
            f"   Plugin source env (EMBEDDR_PLUGIN_DIR): {(os.environ.get('EMBEDDR_PLUGIN_DIR') or '').strip() or '<unset>'}",
            fg=typer.colors.BRIGHT_BLACK,
        )
        if plugin_paths:
            for resolved_path in plugin_paths:
                typer.secho(
                    f"   Plugin path candidate: {resolved_path}",
                    fg=typer.colors.BRIGHT_BLACK,
                )
        else:
            typer.secho(
                "   No plugin path candidates discovered.",
                fg=typer.colors.YELLOW,
            )

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

        # Phase 2: Mount Static Assets for plugins
        # We mount each plugin's directory to /api/v1/plugins/{plugin_name}/static
        # This ensures the correct directory (Source vs Dist) is served for each plugin
        mounted_plugins: set[str] = set()

        def _resolve_dist_root(plugin_name: str) -> "Path | None":
            """Find a plugin's built output in the dist directory.

            Searches ``dist/{plugin_name}`` (legacy flat layout) first, then
            ``dist/{prefix}/{plugin_name}`` for any subdirectory *prefix* that
            the SDK build may have produced (e.g. ``plugins``, ``services``).
            """
            cli_root = Path(__file__).resolve().parents[3]
            repo_root = cli_root.parent
            dist_base = repo_root / "embeddr-plugins" / "dist"

            # Legacy: dist/<plugin_name>
            flat = dist_base / plugin_name
            if flat.exists():
                return flat

            # New: dist/<prefix>/<plugin_name>
            if dist_base.exists():
                for prefix_dir in sorted(dist_base.iterdir()):
                    if not prefix_dir.is_dir():
                        continue
                    candidate = prefix_dir / plugin_name
                    if candidate.exists():
                        return candidate

            return None

        def _mount_plugin_static(plugin_name: str, source_path: Path) -> None:
            if plugin_name in mounted_plugins:
                return
            try:
                dist_root = _resolve_dist_root(plugin_name)
                static_root = dist_root if dist_root and dist_root.exists() else source_path
                public_root = static_root
                if dist_root:
                    public_web_root = dist_root / "web" / "dist"
                    if public_web_root.exists():
                        public_root = public_web_root
                public_assets_root = None
                if dist_root:
                    dist_assets_root = dist_root / "assets"
                    if dist_assets_root.exists():
                        public_assets_root = dist_assets_root
                if not public_assets_root:
                    source_assets_root = source_path / "assets"
                    if source_assets_root.exists():
                        public_assets_root = source_assets_root

                mount_path = f"/api/v1/plugins/{plugin_name}/static"
                alt_mount_path = f"/api/plugins/{plugin_name}/static"
                public_mount_path = f"/plugins/{plugin_name}/static"
                typer.secho(
                    f"   Mounting {plugin_name} assets: {mount_path} -> {static_root}",
                    fg=typer.colors.BRIGHT_BLACK,
                )
                app.mount(
                    mount_path,
                    StaticFiles(directory=str(static_root)),
                    name=f"plugin_static_{plugin_name}",
                )
                app.mount(
                    alt_mount_path,
                    StaticFiles(directory=str(static_root)),
                    name=f"plugin_static_alias_{plugin_name}",
                )
                app.mount(
                    public_mount_path,
                    StaticFiles(directory=str(public_root)),
                    name=f"plugin_static_public_{plugin_name}",
                )
                if public_assets_root is not None:
                    public_assets_path = f"/plugins/{plugin_name}/assets"
                    app.mount(
                        public_assets_path,
                        StaticFiles(directory=str(public_assets_root)),
                        name=f"plugin_assets_public_{plugin_name}",
                    )
                mounted_plugins.add(plugin_name)
            except Exception as e:
                logger.error(
                    "Failed to mount static assets for %s: %s", plugin_name, e
                )

        # Mount static assets for any plugin folder that exists, even if it failed to load.
        for p_path in plugin_paths:
            if not p_path.exists():
                continue
            for plugin_file in p_path.rglob("plugin.py"):
                source_path = plugin_file.parent
                plugin_name = source_path.name
                if source_path.exists():
                    _mount_plugin_static(plugin_name, source_path)

        # Also mount for loaded plugin instances (covers any custom source paths).
        loaded_plugins = get_all_plugin_instances()
        for plugin in loaded_plugins:
            source_path = getattr(plugin, '_source_path', None)
            if source_path and isinstance(source_path, Path) and source_path.exists():
                _mount_plugin_static(plugin.name, source_path)

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

    else:
        typer.secho("🚫 Plugins disabled by flag.", fg=typer.colors.YELLOW)
        logger.info("Plugins disabled by EMBEDDR_NO_PLUGINS")

    # Serve Theme Packs
    try:
        cli_root = Path(__file__).resolve().parents[3]
        repo_root = cli_root.parent
        configured_themes_dir = (os.environ.get(
            "EMBEDDR_THEMES_DIR") or "").strip()
        themes_candidates = []
        if configured_themes_dir:
            themes_candidates.append(Path(configured_themes_dir))
        themes_candidates.extend([
            get_data_dir() / "themes",
            repo_root / "embeddr-themes" / "themes",
        ])

        themes_root = next(
            (candidate for candidate in themes_candidates if candidate.exists()), None)
        if themes_root is not None:
            app.mount(
                "/themes",
                StaticFiles(directory=str(themes_root)),
                name="themes",
            )
            logger.info("Mounted themes at /themes -> %s", themes_root)
        else:
            logger.warning(
                "No themes directory found. Checked: %s", themes_candidates)
    except Exception as exc:
        logger.error("Failed to mount theme packs: %s", exc)

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
    @app.command("prepare-frontend")
    def prepare_frontend(
        source_dir: Path = typer.Option(
            DEFAULT_WEB_SOURCE_DIR,
            help="Path to the embeddr-frontend project",
        ),
        dist_dir: Path = typer.Option(
            None,
            help="Path to built frontend output (defaults to <source_dir>/dist)",
        ),
        target_dir: Path = typer.Option(
            DEFAULT_FRONTEND_DIR,
            help="Where to copy frontend assets for embeddr serve",
        ),
        build: bool = typer.Option(
            True,
            "--build/--no-build",
            help="Run pnpm build in source_dir before copy",
        ),
        confirm: bool = typer.Option(
            False,
            "--confirm",
            help="Confirm destructive copy (target directory will be replaced)",
        ),
    ):
        src_root = source_dir.expanduser().resolve()
        if not src_root.exists():
            typer.secho(
                f"Frontend source directory not found: {src_root}",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1)

        if build:
            try:
                subprocess.run(["pnpm", "build"],
                               cwd=str(src_root), check=True)
            except subprocess.CalledProcessError as exc:
                typer.secho(
                    f"Failed to build frontend in {src_root}: {exc}",
                    fg=typer.colors.RED,
                )
                raise typer.Exit(code=1)
            except FileNotFoundError:
                typer.secho(
                    "pnpm not found. Install pnpm or use --no-build.",
                    fg=typer.colors.RED,
                )
                raise typer.Exit(code=1)

        resolved_dist = (
            dist_dir.expanduser().resolve()
            if dist_dir
            else (src_root / "dist").resolve()
        )
        index_path = resolved_dist / "index.html"
        if not index_path.exists():
            typer.secho(
                f"Built frontend index not found: {index_path}",
                fg=typer.colors.RED,
            )
            raise typer.Exit(code=1)

        resolved_target = target_dir.expanduser().resolve()
        if not confirm:
            typer.secho(
                "This operation replaces the target frontend directory. Re-run with --confirm.",
                fg=typer.colors.YELLOW,
            )
            typer.echo(f"Target: {resolved_target}")
            raise typer.Exit(code=1)

        if resolved_target.exists():
            shutil.rmtree(resolved_target)
        resolved_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(resolved_dist, resolved_target)

        typer.secho("Frontend prepared successfully.", fg=typer.colors.GREEN)
        typer.echo(f"Source dist: {resolved_dist}")
        typer.echo(f"Target dir: {resolved_target}")

    @app.command()
    def serve(
        host: str = typer.Option("127.0.0.1", help="The host to bind to."),
        port: int = typer.Option(8003, help="The port to bind to."),
        reload: bool = typer.Option(False, help="Enable auto-reload."),
        dev_origins: bool = typer.Option(
            False, help="Enable development CORS origins."
        ),
        docs: bool = typer.Option(False, help="Enable API docs."),
        verbose: bool = typer.Option(
            False, help="Enable verbose startup logs."),
        no_plugins: bool = typer.Option(
            False, help="Disable all plugin loading."),
        plugins_dir: str = typer.Option(
            None, help="Directory to serve plugins from."),
        worker: bool = typer.Option(
            False, help="Run as a headless worker node (no UI, no auth routes)."),
        main_url: str = typer.Option(
            None, help="URL of the main instance to connect to (worker mode)."),
        worker_key: str = typer.Option(
            None, help="API key for authenticating with the main instance (worker mode)."),
        worker_name: str = typer.Option(
            None, help="Human-readable name for this worker (defaults to hostname)."),
        worker_tags: str = typer.Option(
            None, help="Comma-separated capability tags (e.g., gpu,encoder,av1)."),
    ):
        """
        Start the Embeddr Local API server.

        In worker mode (--worker), starts a headless instance that connects to
        a main embeddr server and processes jobs remotely.
        """
        # Validate worker mode flags
        if worker:
            if not main_url:
                typer.secho(
                    "Error: --main-url is required in worker mode.",
                    fg=typer.colors.RED,
                )
                raise typer.Exit(1)
            if not worker_key:
                typer.secho(
                    "Error: --worker-key is required in worker mode.",
                    fg=typer.colors.RED,
                )
                raise typer.Exit(1)

        # Set environment variables for the app to use in lifespan
        os.environ["EMBEDDR_HOST"] = host
        os.environ["EMBEDDR_PORT"] = str(port)
        os.environ["EMBEDDR_VERBOSE"] = str(verbose).lower()
        setup_logging(verbose=verbose)
        os.environ["EMBEDDR_ENABLE_DOCS"] = str(docs).lower()
        os.environ["EMBEDDR_ALLOW_DEV_ORIGINS"] = str(dev_origins).lower()
        os.environ["EMBEDDR_NO_PLUGINS"] = str(no_plugins).lower()

        # Worker mode env vars
        if worker:
            os.environ["EMBEDDR_WORKER_MODE"] = "true"
            os.environ["EMBEDDR_WORKER_MAIN_URL"] = main_url
            os.environ["EMBEDDR_WORKER_KEY"] = worker_key
            if worker_name:
                os.environ["EMBEDDR_WORKER_NAME"] = worker_name
            if worker_tags:
                os.environ["EMBEDDR_WORKER_TAGS"] = worker_tags

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

        if worker:
            # Worker mode: use the lightweight worker app
            from embeddr.worker.app import create_worker_app
            worker_app = create_worker_app(
                enable_docs=docs,
                no_plugins=no_plugins,
            )
            typer.secho(
                f"\n🔧 Starting Embeddr Worker Node",
                fg=typer.colors.CYAN,
                bold=True,
            )
            typer.secho(
                f"   Connecting to: {main_url}",
                fg=typer.colors.CYAN,
            )
            uvicorn.run(
                worker_app,
                host=host,
                port=port,
                log_level=log_level,
                ws="websockets-sansio",
            )
        elif reload:
            # When reloading, we can't pass the app instance directly
            # We need to pass the import string.
            # However, factory=True allows us to pass arguments to the factory function
            # But uvicorn.run with factory=True and reload=True is tricky with arguments
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
                    enable_docs=docs,
                    no_plugins=no_plugins,
                ),
                host=host,
                port=port,
                log_level=log_level,
                ws="websockets-sansio",
            )


def create_app_factory() -> FastAPI:
    """Factory function for uvicorn reload mode"""
    setup_logging()
    enable_docs = os.environ.get(
        "EMBEDDR_ENABLE_DOCS", "false").lower() == "true"
    no_plugins = os.environ.get(
        "EMBEDDR_NO_PLUGINS", "false").lower() == "true"
    return create_app(
        enable_docs=enable_docs,
        no_plugins=no_plugins
    )
