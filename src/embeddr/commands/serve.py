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
from typing import Any, Dict, Sequence, Set, cast
from enum import Enum

import typer
import uvicorn
from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from embeddr.api import routes
from embeddr.api import websocket as websocket_routes
from embeddr.api.security import get_api_key, check_auth_enabled
from embeddr.api.transport_security import AuthenticatedTransportApp
from embeddr.services import auth_service
from embeddr_core.models.operator import Operator
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
from embeddr.core.plugin_asset_paths import resolve_plugin_static_layout
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

try:
    from embeddr_core.services.embedding import unload_model
except ImportError:

    def unload_model():
        pass


logger = logging.getLogger("embeddr.local")


# Suppress websockets deprecation warnings
warnings.filterwarnings(
    "ignore", category=DeprecationWarning, module="uvicorn")
warnings.filterwarnings(
    "ignore", category=DeprecationWarning, module="websockets")

# Define the path to the frontend build directory
# Root is parents[3] (embeddr-local-api)
FRONTEND_DIR = Path(os.environ.get(
    "EMBEDDR_FRONTEND_DIR", DEFAULT_FRONTEND_DIR))


def _build_cloud_defaults_script() -> str | None:
    """Build an inline <script> that seeds localStorage with cloud defaults.

    Returns None if not in cloud mode or no defaults are configured.
    """
    from embeddr.core.config import is_cloud_mode
    if not is_cloud_mode():
        return None

    import json as _json

    state: Dict[str, Any] = {}

    theme_mode = os.environ.get("EMBEDDR_DEFAULT_THEME_MODE", "").strip()
    if theme_mode:
        state["themeMode"] = theme_mode
    theme_light = os.environ.get("EMBEDDR_DEFAULT_THEME_LIGHT", "").strip()
    if theme_light:
        state["themePackLightId"] = theme_light
    theme_dark = os.environ.get("EMBEDDR_DEFAULT_THEME_DARK", "").strip()
    if theme_dark:
        state["themePackDarkId"] = theme_dark

    effects_raw = os.environ.get(
        "EMBEDDR_DEFAULT_EFFECTS_ENABLED", "").strip().lower()
    if effects_raw in ("1", "true", "yes"):
        state["effectsEnabled"] = True
    elif effects_raw in ("0", "false", "no"):
        state["effectsEnabled"] = False

    effects_list_raw = os.environ.get("EMBEDDR_DEFAULT_EFFECTS", "").strip()
    if effects_list_raw:
        enabled_effects = [
            e.strip() for e in effects_list_raw.split(",") if e.strip()]
        # Build effectToggles: listed effects are enabled, others default off.
        # The Zustand store defaults unlisted effects to true via getEffectToggle,
        # so we explicitly set only the ones we want on.
        toggles: Dict[str, bool] = {}
        for effect_id in enabled_effects:
            toggles[effect_id] = True
        state["effectToggles"] = toggles

    if not state:
        return None

    payload = _json.dumps({"state": state, "version": 3})
    return (
        "<script>"
        "if(!localStorage.getItem('embeddr-client-settings')){"
        f"localStorage.setItem('embeddr-client-settings','{payload}')"
        "}"
        "</script>"
    )


# Cache the script once at startup (env vars don't change at runtime)
_CLOUD_DEFAULTS_SCRIPT: str | None = None


def _get_cloud_defaults_script() -> str | None:
    global _CLOUD_DEFAULTS_SCRIPT
    if _CLOUD_DEFAULTS_SCRIPT is None:
        _CLOUD_DEFAULTS_SCRIPT = _build_cloud_defaults_script() or ""
    return _CLOUD_DEFAULTS_SCRIPT or None


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
                "root_password": result.root_password,
                "user_operator": result.user_operator_name,
                "user_username": result.user_username,
                "user_key": result.user_key,
                "user_password": result.user_password,
            }

    # Ensure first-party service clients exist (if auth_service supports it)
    if hasattr(auth_service, "ensure_first_party_clients"):
        with Session(get_engine()) as session:
            root_operator = session.exec(
                select(Operator).where(Operator.is_root == True)
            ).first()
            root_user = session.exec(
                select(UserAccount).where(UserAccount.is_admin == True)
            ).first()
            if root_operator and root_user:
                fp_results = auth_service.ensure_first_party_clients(
                    session, operator_id=root_operator.id,
                    created_by_user_id=root_user.id,
                )
                session.commit()
                new_clients = {k: v for k, v in fp_results.items() if v != "(already registered)"}
                if new_clients:
                    typer.secho(
                        f"Registered {len(new_clients)} first-party service client(s).",
                        fg=typer.colors.GREEN,
                    )

    from embeddr.core.config import is_cloud_mode

    if not result_data:
        if not is_cloud_mode():
            typer.secho(
                "Default admin already exists; skipping bootstrap.",
                fg=typer.colors.BRIGHT_BLACK,
            )
        return

    if is_cloud_mode():
        # In cloud mode, log credentials for container logs (no interactive output)
        logger.info("Bootstrapped admin: user=%s key=%s", result_data["root_username"], result_data["root_key"])
        return

    typer.secho(
        "Bootstrapped root operator",
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
    if result_data.get("root_password"):
        typer.secho(
            f"   Root Password: {result_data['root_password']}",
            fg=typer.colors.BRIGHT_YELLOW,
        )
    typer.secho(
        "   Store these credentials securely. They will not be shown again.",
        fg=typer.colors.BRIGHT_BLACK,
    )

    if result_data.get("user_key"):
        typer.secho(
            "Bootstrapped single-user operator",
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
        if result_data.get("user_password"):
            typer.secho(
                f"   Password: {result_data['user_password']}",
                fg=typer.colors.BRIGHT_YELLOW,
            )
        typer.secho(
            "   Store these credentials securely. They will not be shown again.",
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

    # Clean up expired auth sessions on startup
    try:
        from embeddr.services import auth_service as _auth_svc
        from embeddr.db.session import get_engine as _get_engine
        from sqlmodel import Session as _Session
        with _Session(_get_engine()) as _s:
            cleaned = _auth_svc.cleanup_expired_sessions(_s)
            if cleaned > 0:
                logger.info("Cleaned up %d expired sessions on startup", cleaned)
    except Exception as e:
        logger.debug("Session cleanup skipped: %s", e)

    # Plugin loading is handled in create_app to ensure API registration works
    # Here we just trigger startup hooks

    # Trigger Plugin on_startup hooks
    startup_all_plugins()

    # Start Ingestion Service
    from embeddr.services.ingestion_service import ingestion_service
    await ingestion_service.start()

    # Start ingest reactor — auto-processes new artifacts (thumbnails, embeddings)
    from embeddr.services.ingest_reactor import start as start_ingest_reactor
    start_ingest_reactor()

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

        # Look for execution_id at top level or nested in data (e.g. LLM
        # plugin emits execution.event with {data: {execution_id: ...}})
        execution_id = (
            payload.get("id")
            or payload.get("execution_id")
            or (isinstance(payload.get("data"), dict)
                and (payload["data"].get("execution_id")
                     or payload["data"].get("id")))
        )
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
            event_type = str(event.event_type or "")
            payload = event.payload

            audience = _derive_audience(event_type, payload)

            # In secured auth modes, drop events that touch sensitive scopes
            # (execution.*, artifact.*) unless they have audience info or come
            # from a trusted system source.
            auth_mode = auth_service.get_auth_mode()
            is_sensitive = event_type.startswith("execution.") or event_type.startswith("artifact.")
            is_system = event.source in ("spine", "system", "embeddr", "plugin", "ingestion")
            if auth_mode != "open" and is_sensitive and not audience and not is_system:
                logger.debug("Dropping unscoped WS event type=%s source=%s", event_type, event.source)
                return

            asyncio.run_coroutine_threadsafe(
                manager.broadcast_event(event_type, payload, source=event.source, audience=audience),
                loop,
            )
        except Exception as e:
            logger.error("Event bridge error: %s", e)

    # Subscribe to relevant events
    _EVENT_BUS.subscribe("*", bridge_event_to_ws)

    # Render consolidated startup summary
    from embeddr.core.startup_summary import build_startup_context, render_startup_summary

    try:
        startup_ctx = build_startup_context(
            host=host,
            port=int(port),
            docs_enabled=docs_enabled,
        )
        render_startup_summary(startup_ctx)
    except Exception as e:
        logger.warning("Failed to render startup summary: %s", e)
        # Minimal fallback
        typer.secho(f"\n  Embeddr started on http://{display_host}:{port}\n",
                    fg=typer.colors.GREEN)

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


def _register_endpoints(
    app: FastAPI,
    router,
    prefix: str,
    tags: Sequence[str | Enum] | None,
    dependencies: Sequence[Any] | None = None,
):
    logger.info(f"Registering router: {prefix} with tags: {tags}")
    router_tags = cast(list[str | Enum] | None, list(tags) if tags is not None else None)
    router_dependencies = cast(
        list[Any] | None,
        list(dependencies) if dependencies is not None else None,
    )
    app.include_router(
        router,
        prefix=prefix,
        tags=router_tags,
        dependencies=router_dependencies,
    )


def create_app(
    enable_docs: bool = False,
    no_plugins: bool = False,
) -> FastAPI:
    logger.debug("== Config ==")
    logger.debug("Docs: %s", enable_docs)
    app = FastAPI(
        title="Embeddr Local",
        lifespan=lifespan,
        docs_url="/api/docs" if enable_docs else None,
        redoc_url="/api/redoc" if enable_docs else None,
        openapi_url="/api/openapi.json" if enable_docs else None,
    )

    # Install standardized error response handlers (ErrorResponse envelope)
    from embeddr.api.error_handler import install_error_handlers
    install_error_handlers(app)

    logger.debug("== CORS ==")
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
    logger.debug("Allowed CORS origins: %s", allowed_origins)

    logger.debug("== Security ==")
    is_auth_enabled = check_auth_enabled()
    if is_auth_enabled:
        logger.debug("Authentication: ENABLED")
        api_dependencies = [Depends(get_api_key)]
    else:
        logger.debug("Authentication: DISABLED")
        api_dependencies = []

    logger.debug("== API Routes ==")
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
    # Serve Plugins Directory
    logger.debug("== Plugins ==")
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

        logger.debug(
            "Plugin source env EMBEDDR_PLUGINS_DIR=%s EMBEDDR_PLUGIN_DIR=%s",
            (os.environ.get("EMBEDDR_PLUGINS_DIR") or "").strip() or "<unset>",
            (os.environ.get("EMBEDDR_PLUGIN_DIR") or "").strip() or "<unset>",
        )
        if plugin_paths:
            for resolved_path in plugin_paths:
                logger.debug("Plugin path candidate: %s", resolved_path)
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

        def _mount_plugin_static(plugin_name: str, source_path: Path) -> None:
            if plugin_name in mounted_plugins:
                return
            try:
                layout = resolve_plugin_static_layout(plugin_name, source_path)

                mount_path = f"/api/v1/plugins/{plugin_name}/static"
                alt_mount_path = f"/api/plugins/{plugin_name}/static"
                public_mount_path = f"/plugins/{plugin_name}/static"
                logger.debug(
                    "Mounting %s assets: %s -> %s",
                    plugin_name, mount_path, layout.static_root,
                )
                app.mount(
                    mount_path,
                    StaticFiles(directory=str(layout.static_root)),
                    name=f"plugin_static_{plugin_name}",
                )
                app.mount(
                    alt_mount_path,
                    StaticFiles(directory=str(layout.static_root)),
                    name=f"plugin_static_alias_{plugin_name}",
                )
                app.mount(
                    public_mount_path,
                    StaticFiles(directory=str(layout.public_root)),
                    name=f"plugin_static_public_{plugin_name}",
                )
                if layout.public_dist_root is not None:
                    app.mount(
                        f"{public_mount_path}/dist",
                        StaticFiles(directory=str(layout.public_dist_root)),
                        name=f"plugin_static_public_dist_{plugin_name}",
                    )
                if layout.public_assets_root is not None:
                    public_assets_path = f"/plugins/{plugin_name}/assets"
                    app.mount(
                        public_assets_path,
                        StaticFiles(directory=str(layout.public_assets_root)),
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
            loaded_source_path = getattr(plugin, '_source_path', None)
            if loaded_source_path and isinstance(loaded_source_path, Path) and loaded_source_path.exists():
                _mount_plugin_static(plugin.name, loaded_source_path)

        # Initialize all discovered plugins
        initialize_all_plugins()

        # Phase 3: Register transport routes (if provided)
        logger.debug("== Transports ==")
        for plugin in loaded_plugins:
            try:
                transport_app = AuthenticatedTransportApp(
                    app,
                    transport_name=str(plugin.name),
                    required_permissions=["lotus:dispatch"],
                )
                plugin.register_transport(transport_app)
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
        themes_candidates: list[Path] = []
        if configured_themes_dir:
            themes_candidates.append(Path(configured_themes_dir))
        themes_candidates.extend([
            Path(get_data_dir()) / "themes",
            repo_root / "embeddr-themes" / "themes",
        ])

        themes_root: Path | None = next(
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

        # Pre-read index.html and inject cloud defaults script if applicable.
        # Cached once — avoids re-reading the file on every SPA navigation.
        _index_html_path = os.path.join(FRONTEND_DIR, "index.html")
        _cloud_script = _get_cloud_defaults_script()
        _index_html_content: str | None = None
        if _cloud_script:
            try:
                raw = Path(_index_html_path).read_text(encoding="utf-8")
                _index_html_content = raw.replace(
                    "</head>", f"{_cloud_script}</head>", 1)
                logger.info(
                    "Cloud defaults script injected into index.html")
            except Exception as exc:
                logger.warning(
                    "Failed to inject cloud defaults: %s", exc)

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

            # Serve index.html — with cloud defaults injected if available
            if _index_html_content is not None:
                return HTMLResponse(content=_index_html_content)
            return FileResponse(_index_html_path)

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
        plugin_profile: str = typer.Option(
            None, help="Named plugin profile from embeddr.toml (e.g., core, dev)."),
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
        if plugin_profile:
            os.environ["EMBEDDR_PLUGIN_PROFILE"] = plugin_profile

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
