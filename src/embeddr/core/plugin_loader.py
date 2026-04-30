import importlib.util
import logging
import sys
import types
import inspect
import asyncio
import os
import subprocess
import json
from pathlib import Path
from typing import Optional, Any, List, Dict
from embeddr_core.services.lotus_registry import LotusRegistry
from embeddr_core.models.lotus import LotusKind, LotusCapability
from embeddr_core.models.artifact_type import ArtifactType, ArtifactTypeSpec
from embeddr.db.session import get_engine
from sqlmodel import Session, select
from fastapi import FastAPI, APIRouter, Depends
from embeddr_core.plugin_interface import (
    EmbeddrPlugin,
    PluginIntent,
    PluginContext,
    ConfigRendererComponent,
)
from embeddr_core.services.resource_manager import resource_manager
from embeddr.core.event_bus import SimpleEventBus, _EVENT_BUS
from embeddr.core.execution_spine import ExecutionSpine
from embeddr_core.services.config_service import resolve_plugin_config_for_plugin
from embeddr.core.plugin_context_helpers import LotusContext
from embeddr.core.plugin_discovery import iter_plugin_dirs, load_disabled_plugins
from embeddr.core.plugin_asset_paths import (
    resolve_plugin_bundle_name,
    resolve_plugin_frontend_dist_dir,
)

logger = logging.getLogger(__name__)

# Ensure virtual package for plugins exists
if "embeddr_plugins" not in sys.modules:
    plugin_pkg = types.ModuleType("embeddr_plugins")
    plugin_pkg.__path__ = []
    sys.modules["embeddr_plugins"] = plugin_pkg


# LOTUS
_LOTUS_REGISTRY = LotusRegistry()


def get_lotus_registry() -> LotusRegistry:
    return _LOTUS_REGISTRY


# Registry of loaded plugins
_LOADED_PLUGINS: List[EmbeddrPlugin] = []
# _EVENT_BUS is imported from event_bus.py (singleton)

# Capability registry: maps PluginIntent to list of plugin instances
_PLUGIN_CAPABILITY_REGISTRY: Dict[PluginIntent, list] = {}


def get_plugins_by_intent(intent: PluginIntent) -> List[EmbeddrPlugin]:
    """
    Return all loaded plugins that provide the given intent/capability.
    """
    return _PLUGIN_CAPABILITY_REGISTRY.get(intent, [])


def register_plugin_capabilities(plugin: EmbeddrPlugin):
    """
    Register plugin's capabilities (intents) in the global registry.
    """
    for intent in plugin.intents:
        if intent not in _PLUGIN_CAPABILITY_REGISTRY:
            _PLUGIN_CAPABILITY_REGISTRY[intent] = []
        _PLUGIN_CAPABILITY_REGISTRY[intent].append(plugin)


def get_loaded_plugins() -> List[Dict[str, Any]]:
    plugins = []
    for p in _LOADED_PLUGINS:
        panels = getattr(p, "panels", [])
        docks = getattr(p, "docks", [])
        config_renderers = getattr(p, "config_renderers", [])
        source_path = getattr(p, "_source_path", None)

        registry_meta: Dict[str, Any] = {}
        manifest_path: Optional[Path] = None
        if isinstance(source_path, str):
            source_path = Path(source_path)
        if isinstance(source_path, Path):
            candidate = source_path if source_path.is_dir() else source_path.parent
            if candidate.exists():
                manifest_path = candidate / "embeddr.plugin.json"
        if manifest_path and manifest_path.exists() and manifest_path.is_file():
            try:
                with manifest_path.open("r", encoding="utf-8") as f:
                    manifest_data = json.load(f)
                registry_data = manifest_data.get("registry")
                if isinstance(registry_data, dict):
                    registry_meta = registry_data
            except (OSError, json.JSONDecodeError, ValueError) as e:
                logger.debug("Failed to read plugin manifest %s: %s", manifest_path, e)
                registry_meta = {}

        display_name = registry_meta.get("name") if isinstance(registry_meta.get("name"), str) else None
        if isinstance(display_name, str):
            display_name = display_name.strip() or None

        description = registry_meta.get("description") if isinstance(registry_meta.get("description"), str) else None
        if isinstance(description, str):
            description = description.strip() or None

        author = registry_meta.get("author") if isinstance(registry_meta.get("author"), str) else None
        if isinstance(author, str):
            author = author.strip() or None

        homepage_url = registry_meta.get("homepage_url") if isinstance(registry_meta.get("homepage_url"), str) else None
        if isinstance(homepage_url, str):
            homepage_url = homepage_url.strip() or None

        repository_url = registry_meta.get("repository_url") if isinstance(registry_meta.get("repository_url"), str) else None
        if isinstance(repository_url, str):
            repository_url = repository_url.strip() or None

        license_name = registry_meta.get("license") if isinstance(registry_meta.get("license"), str) else None
        if isinstance(license_name, str):
            license_name = license_name.strip() or None

        tags = registry_meta.get("tags") if isinstance(registry_meta.get("tags"), list) else []
        tags = [t.strip() for t in tags if isinstance(t, str) and t.strip()]

        # Construct the frontend JS URL assuming standard build output.
        # Server mounts /plugins -> plugins directory.
        # Frontend expects 'url' field.

        plugin_data = {
            "id": p.name,  # Frontend typically uses 'id'
            "name": p.name,
            "display_name": display_name,
            "version": p.version,
            "description": description,
            "author": author,
            "homepage_url": homepage_url,
            "repository_url": repository_url,
            "license": license_name,
            "tags": tags,
            "intents": [i.value for i in p.intents],
            "actions": [a.model_dump() for a in p.actions],
            "frontend_actions": [a.model_dump() for a in getattr(p, "frontend_actions", [])],
            "frontend_components": [c.model_dump() for c in getattr(p, "frontend_components", [])],
            "panels": [c.model_dump() for c in panels],
            "docks": [c.model_dump() for c in docks],
            "config_renderers": [
                c.model_dump() for c in config_renderers
                if isinstance(c, ConfigRendererComponent)
            ],
            "pages": [c.model_dump() for c in getattr(p, "pages", [])],
            "widgets": [c.model_dump() for c in getattr(p, "widgets", [])],
            # Reactive renderables — zen-shell's loader reads these and
            # populates BOTH the renderable + reactive-context registries.
            # Returned as plain dicts (already serialized by the adapter).
            "renderables": [
                (r if isinstance(r, dict) else r.model_dump())
                for r in (getattr(p, "renderables", []) or [])
            ],
        }

        # Only add URL if the plugin actually has a dist (frontend build)
        dist_dir = resolve_plugin_frontend_dist_dir(
            plugin_name=p.name,
            source_path=source_path,
        )

        if dist_dir:
            # Point to the UMD bundle generated by embeddr-sdk
            # Bundle name is derived from the plugin folder name (normalized)
            # We mount static assets at /plugins/{name}/static (public)
            bundle_base = (
                source_path.name if source_path else p.name).replace("_", "-")
            bundle_name = resolve_plugin_bundle_name(dist_dir, bundle_base)

            if bundle_name:
                plugin_data["url"] = (
                    f"/plugins/{p.name}/static/dist/{bundle_name}"
                )

            css_path = dist_dir / "style.css"
            if css_path.exists():
                plugin_data["css_url"] = (
                    f"/plugins/{p.name}/static/dist/style.css"
                )

        plugins.append(plugin_data)
    return plugins


def get_all_plugin_instances() -> List[EmbeddrPlugin]:
    return _LOADED_PLUGINS


def get_plugin_instance(name: str) -> Optional[EmbeddrPlugin]:
    for p in _LOADED_PLUGINS:
        if p.name == name:
            return p
    return None


def load_python_plugins(plugins_dir: Path, app: Optional[FastAPI] = None, cli_app: Any = None) -> None:
    """
    Scans the plugins directory for subdirectories containing a 'plugin.py' file.
    If found, it attempts to load the module and register it.
    Also recurses into specific folders like 'examples' and 'private_plugins'.
    """
    if not plugins_dir.exists():
        return

    logger.debug(f"Scanning for Python plugins in {plugins_dir}...")
    import typer

    from embeddr.core.plugin_discovery import resolve_plugin_filter
    plugin_filter = resolve_plugin_filter()

    def _maybe_install_requirements(p_dir: Path) -> None:
        if str(os.environ.get("EMBEDDR_PLUGINS_AUTO_INSTALL", "")).lower() not in (
            "1",
            "true",
            "yes",
            "on",
        ):
            return

        req_file = p_dir / "requirements.txt"
        if not req_file.exists():
            return

        logger.info("[PluginLoader] Auto-installing deps for %s", p_dir.name)
        try:
            subprocess.check_call(
                ["uv", "pip", "install", "-r", str(req_file)])
            return
        except (FileNotFoundError, subprocess.CalledProcessError):
            logger.warning(
                "[PluginLoader] uv pip failed, falling back to pip for %s", p_dir.name)

        try:
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "-r", str(req_file)])
        except subprocess.CalledProcessError as exc:
            logger.error(
                "[PluginLoader] Dependency install failed for %s: %s", p_dir.name, exc)

    def process_plugin_dir(p_dir: Path):
        if not plugin_filter.should_load(p_dir.name):
            logger.debug(
                "[PluginLoader] Skipping plugin %s (filter: %s%s)",
                p_dir.name,
                plugin_filter.mode,
                f", profile={plugin_filter.profile_name}" if plugin_filter.profile_name else "",
            )
            return
        plugin_file = p_dir / "plugin.py"
        if not plugin_file.exists():
            return

        _maybe_install_requirements(p_dir)

        folder_name = p_dir.name  # directory name
        try:
            # --- Ensure virtual package for plugins exists ---
            if "embeddr_plugins" not in sys.modules:
                pkg = types.ModuleType("embeddr_plugins")
                pkg.__path__ = []
                sys.modules["embeddr_plugins"] = pkg

            # --- Folder-based module name (how we load from disk) ---
            folder_safe = folder_name.replace("-", "_").replace(".", "_")
            folder_module_name = f"embeddr_plugins.{folder_safe}"

            logger.debug(
                "[PluginLoader] Loading from dir=%s folder_module=%s",
                str(p_dir),
                folder_module_name,
            )

            # Load the module as a package (submodule_search_locations allows relative imports)
            spec = importlib.util.spec_from_file_location(
                folder_module_name,
                plugin_file,
                submodule_search_locations=[str(p_dir)],
            )
            if not spec or not spec.loader:
                logger.error(
                    "[PluginLoader] Spec loader missing for %s", str(plugin_file))
                return

            module = importlib.util.module_from_spec(spec)
            module.__path__ = [str(p_dir)]
            module.__package__ = folder_module_name

            # Register module under folder name BEFORE executing (relative imports rely on this)
            sys.modules[folder_module_name] = module

            # Execute plugin module
            spec.loader.exec_module(module)

            # 1) Class-based Plugin (New System)
            # The loader discovers the first EmbeddrPlugin subclass
            # defined in the module and instantiates it.
            plugin_instance = None
            plugin_candidates = []
            for name, obj in inspect.getmembers(module):
                if (
                    inspect.isclass(obj)
                    and issubclass(obj, EmbeddrPlugin)
                    and obj is not EmbeddrPlugin
                    and obj.__module__ == module.__name__
                ):
                    plugin_candidates.append((name, obj))

            if len(plugin_candidates) > 1:
                names = [n for n, _ in plugin_candidates]
                logger.warning(
                    "[PluginLoader] Multiple EmbeddrPlugin subclasses in %s: %s — using %s. "
                    "Define a single plugin class per module.",
                    folder_module_name, names, names[0],
                )

            if plugin_candidates:
                plugin_instance = plugin_candidates[0][1]()
            else:
                # 1b) Petal-style Plugin — wrap via adapter so the rest of the
                # registration pipeline can treat it as an EmbeddrPlugin.
                from embeddr.core.petal_adapter import (
                    is_petal_plugin_class,
                    PetalEmbeddrAdapter,
                )
                petal_candidates = [
                    (n, obj) for n, obj in inspect.getmembers(module)
                    if is_petal_plugin_class(obj) and obj.__module__ == module.__name__
                ]
                if len(petal_candidates) > 1:
                    names = [n for n, _ in petal_candidates]
                    logger.warning(
                        "[PluginLoader] Multiple petal Plugin subclasses in %s: %s — using %s.",
                        folder_module_name, names, names[0],
                    )
                if petal_candidates:
                    petal_obj = petal_candidates[0][1]()
                    plugin_instance = PetalEmbeddrAdapter(petal_obj)
                    # Register petal @action methods as Lotus capabilities so
                    # /api/lotus/{cap_id} can resolve and dispatch them.
                    try:
                        lotus_reg = get_lotus_registry()
                        registered = 0
                        for cap in plugin_instance.build_lotus_capabilities():
                            lotus_reg.register(cap)
                            registered += 1
                        logger.info(
                            "[PluginLoader] Wrapped petal plugin %s via adapter (+%d lotus caps)",
                            plugin_instance.name, registered,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[PluginLoader] Failed to register lotus caps for %s: %s",
                            plugin_instance.name, exc,
                        )

            if plugin_instance:
                # Canonical module name based on the plugin's declared name
                plugin_name = plugin_instance.name
                plugin_safe = plugin_name.replace("-", "_").replace(".", "_")
                canonical_module_name = f"embeddr_plugins.{plugin_safe}"

                # --- Alias folder_module -> canonical_module ---
                # This is the key fix that makes model paths stable:
                #   embeddr_plugins.<safe(plugin_instance.name)>.models:MyModel
                if canonical_module_name != folder_module_name:
                    sys.modules[canonical_module_name] = module
                    logger.info(
                        "[PluginLoader] Aliased plugin module: %s -> %s (dir=%s)",
                        folder_module_name,
                        canonical_module_name,
                        str(p_dir),
                    )

                    # Alias common submodules if they exist under the folder module name
                    # e.g. embeddr_plugins.folder_name.models -> embeddr_plugins.plugin_name.models
                    folder_prefix = folder_module_name + "."
                    canonical_prefix = canonical_module_name + "."

                    # If the plugin (during exec_module) imported its own submodules,
                    # they will show up as sys.modules entries like:
                    #   embeddr_plugins.<folder_safe>.models
                    # We alias ALL loaded children under that prefix.
                    to_alias = [
                        m for m in list(sys.modules.keys())
                        if m.startswith(folder_prefix)
                    ]
                    for old in to_alias:
                        new = canonical_prefix + old[len(folder_prefix):]
                        if new not in sys.modules:
                            sys.modules[new] = sys.modules[old]
                            logger.debug(
                                "[PluginLoader]   Aliased submodule: %s -> %s", old, new)

                # --- Existing plugin instance check (dedupe) ---
                existing_plugin = next(
                    (p for p in _LOADED_PLUGINS if p.name == plugin_instance.name), None)
                if existing_plugin:
                    plugin_instance = existing_plugin
                    is_new_load = False
                    logger.debug(
                        "[PluginLoader] Reusing existing plugin instance: %s", plugin_instance.name)
                else:
                    _LOADED_PLUGINS.append(plugin_instance)
                    register_plugin_capabilities(plugin_instance)
                    is_new_load = True

                # --- Track source path for static files (your existing logic) ---
                new_has_dist = (p_dir / "dist").exists()
                current_path = getattr(plugin_instance, "_source_path", None)
                current_has_dist = (
                    (current_path / "dist").exists()
                    if current_path and isinstance(current_path, Path)
                    else False
                )

                if not current_path:
                    plugin_instance._source_path = p_dir
                elif new_has_dist:
                    plugin_instance._source_path = p_dir
                elif not current_has_dist:
                    plugin_instance._source_path = p_dir

                # --- Debug summary for load/aliasing ---
                if is_new_load:
                    icons = ["🐍"]
                    extras = []

                    if any(a.ui_component for a in getattr(plugin_instance, "actions", [])):
                        icons.append("⚛️")
                        extras.append("UI")
                    if PluginIntent.ZEN_PANEL in plugin_instance.intents:
                        icons.append("🧘")
                        extras.append("Zen")
                    if PluginIntent.REGISTER_API in plugin_instance.intents:
                        icons.append("🔌")
                        extras.append("API")
                    if PluginIntent.REGISTER_CLI in plugin_instance.intents:
                        icons.append("💻")
                        extras.append("CLI")

                    icon_str = " ".join(icons)
                    extra_str = f"[{', '.join(extras)}]" if extras else ""
                    logger.info(
                        "  %s %s v%s %s",
                        icon_str,
                        plugin_instance.name,
                        plugin_instance.version,
                        extra_str,
                    )

                # --- API Registration (your existing logic, unchanged) ---
                if app and PluginIntent.REGISTER_API in plugin_instance.intents:
                    if not getattr(plugin_instance, "_api_registered", False):
                        try:
                            from embeddr.api.security import check_auth_enabled, get_api_key

                            api_dependencies = (
                                [Depends(get_api_key)
                                 ] if check_auth_enabled() else []
                            )
                            plugin_router = APIRouter(
                                prefix=f"/api/v1/plugins/{plugin_instance.name}",
                                tags=[plugin_instance.name],
                                dependencies=api_dependencies,
                            )
                            plugin_instance.register_api(plugin_router)
                            app.include_router(plugin_router)
                            # Alias without /v1/ for clients using /api/plugins/ base URL
                            alias_router = APIRouter(
                                prefix=f"/api/plugins/{plugin_instance.name}",
                                tags=[plugin_instance.name],
                                dependencies=api_dependencies,
                            )
                            plugin_instance.register_api(alias_router)
                            app.include_router(alias_router)
                            plugin_instance._api_registered = True
                            logger.debug(
                                "  -> Registered API for %s at /api/v1/plugins/%s",
                                plugin_instance.name,
                                plugin_instance.name,
                            )
                        except Exception as e:
                            logger.error(
                                "Failed to register API for %s: %s", plugin_instance.name, e)

                # --- CLI Registration (your existing logic, unchanged) ---
                if cli_app and PluginIntent.REGISTER_CLI in plugin_instance.intents:
                    try:
                        plugin_typer = typer.Typer(
                            help=f"Commands for {plugin_instance.name}",
                            no_args_is_help=True,
                        )
                        plugin_instance.register_cli(plugin_typer)
                        cli_app.add_typer(
                            plugin_typer, name=plugin_instance.name)
                        logger.debug(
                            "  -> Registered CLI group '%s'", plugin_instance.name)
                    except Exception as e:
                        logger.error(
                            "Failed to register CLI for %s: %s", plugin_instance.name, e)

                # --- LOTUS registration (your existing logic, unchanged) ---
                try:
                    if hasattr(plugin_instance, "register_lotus"):
                        caps = plugin_instance.register_lotus() or []
                        for cap in caps:
                            if not cap.plugin:
                                cap.plugin = plugin_instance.name
                            if not cap.version:
                                cap.version = plugin_instance.version
                            if cap.plugin and "." not in cap.id and not cap.id.startswith(f"{cap.plugin}."):
                                cap.id = f"{cap.plugin}.{cap.id}"
                            _LOTUS_REGISTRY.register(cap)
                except Exception as e:
                    logger.error(
                        "Failed to register LOTUS capabilities for %s: %s", plugin_instance.name, e)

                return

            # 2) Legacy Function-based Plugin (unchanged)
            if hasattr(module, "register") and app:
                logger.info("Registering legacy plugin: %s", folder_name)
                router = APIRouter(
                    prefix=f"/api/v1/plugins/{folder_name}", tags=[folder_name])
                module.register(router)
                app.include_router(router)

        except Exception as e:
            logger.error("=" * 72)
            logger.error("PLUGIN LOAD FAILURE")
            logger.error("Plugin: %s", folder_name)
            logger.error("Path: %s", str(plugin_file))
            logger.error("Error: %s", e)
            logger.error("=" * 72)
            logger.debug(
                "Plugin load failure details (plugin=%s path=%s)",
                folder_name,
                str(plugin_file),
                exc_info=True,
            )

    for plugin_dir in iter_plugin_dirs(plugins_dir):
        process_plugin_dir(plugin_dir)


_INITIALIZED = False


def initialize_all_plugins() -> None:
    """
    Phase 1: Discovery & Initialization.
    Trigger on_load for all plugins with the full capability registry available.
    """
    global _INITIALIZED
    if _INITIALIZED:
        logger.debug("Plugins already initialized, skipping.")
        return

    logger.info(
        "Initializing %d plugins...", len(_LOADED_PLUGINS))

    # After discovery and registration, initialize all plugins with the full capability registry
    from embeddr.db.session import get_engine
    from contextlib import contextmanager
    from sqlmodel import Session

    @contextmanager
    def _make_session():
        with Session(get_engine()) as s:
            yield s

    context = PluginContext(
        bus=_EVENT_BUS,
        capability_registry=_PLUGIN_CAPABILITY_REGISTRY,
        resources=resource_manager,
        db_session_factory=_make_session,
    )

    # Sort plugins so providers (search, storage, indexer) load before consumers
    # This is a heuristic until we have a proper dependency graph
    _PROVIDER_INTENTS = {
        PluginIntent.PROVIDE_SEARCH,
        PluginIntent.PROVIDE_INDEXER,
        PluginIntent.PROVIDE_STORAGE,
    }

    def sort_key(p):
        return 0 if _PROVIDER_INTENTS & set(p.intents) else 1

    sorted_plugins = sorted(_LOADED_PLUGINS, key=sort_key)

    for plugin in sorted_plugins:
        try:
            plugin.on_load(context)
        except Exception as e:
            logger.error(f"Failed to initialize plugin {plugin.name}: {e}")

    _INITIALIZED = True

    # Phase 1b: Register Lotus Async Handlers
    _register_lotus_async_handlers()
    try:
        get_lotus_registry().recheck_dependencies()
    except Exception:
        logger.debug("Lotus dependency recheck failed", exc_info=True)


def _register_lotus_async_handlers() -> None:
    """
    Iterate through registered Lotus Capabilities.
    If a capability is an 'action' with 'exec.mode' == 'async',
    automatically register a handler in ExecutionSpine that routes to plugin.execute.
    """
    registry = get_lotus_registry()
    actions = registry.list(kind=LotusKind.action)
    features = registry.list(kind=LotusKind.feature)
    caps = actions + features

    count = 0
    registered: set[str] = set()
    for cap in caps:
        data = cap.data or {}
        action_def = cap.action
        exec_conf = (action_def.exec if action_def else None) or data.get(
            "exec", {})
        # Default to async if not specified? Or check sync=False logic
        mode = exec_conf.get("mode", "async")

        # All Lotus actions must be runnable via ExecutionSpine.
        # We still keep exec.mode for UI/metadata, but always register handlers.
        action_name = action_def.action if action_def else data.get("action")
        job_type = action_def.job_type if action_def else data.get("job_type")
        plugin_name = cap.plugin

        if not action_name or not plugin_name:
            continue

        # Define the handler adapter
        def make_handler(p_name, a_name):
            def lotus_adapter(context):  # Receives JobContext
                plugin = get_plugin_instance(p_name)
                if not plugin:
                    raise ValueError(
                        f"Plugin {p_name} not found for action {a_name}")

                # Ensure event bus is available for UI actions that emit events.
                if not hasattr(context, "bus"):
                    setattr(context, "bus", _EVENT_BUS)
                if not hasattr(context, "config"):
                    setattr(
                        context,
                        "config",
                        resolve_plugin_config_for_plugin(plugin_name=p_name),
                    )
                if not hasattr(context, "lotus"):
                    setattr(
                        context,
                        "lotus",
                        LotusContext(context=context),
                    )

                auth_payload = {}
                if isinstance(getattr(context, "inputs", None), dict):
                    auth_payload = context.inputs.get("__embeddr_auth") or {}

                if isinstance(auth_payload, dict) and auth_payload:
                    if not hasattr(context, "user_id"):
                        setattr(context, "user_id",
                                auth_payload.get("user_id"))
                    if not hasattr(context, "operator_id"):
                        setattr(context, "operator_id",
                                auth_payload.get("operator_id"))
                    if not hasattr(context, "is_admin"):
                        setattr(context, "is_admin", bool(
                            auth_payload.get("is_admin", False)))
                    if not hasattr(context, "is_root"):
                        setattr(context, "is_root", bool(
                            auth_payload.get("is_root", False)))
                    if not hasattr(context, "is_open"):
                        setattr(context, "is_open", bool(
                            auth_payload.get("is_open", False)))
                    if not hasattr(context, "permissions"):
                        setattr(context, "permissions", set(
                            auth_payload.get("permissions") or []))
                    # Propagate the originating client ID so UI actions can
                    # target events back to the specific client that invoked them.
                    if not hasattr(context, "caller_client_id"):
                        setattr(context, "caller_client_id",
                                auth_payload.get("client_id") or None)

                # Pass context as the 4th arg.
                # We rely on plugin.execute handling DuckTyped context (inputs, log, etc)
                return plugin.execute(a_name, context.execution_id, context.inputs, context=context)
            return lotus_adapter

        # Register action name + job_type (if provided)
        for handler_key in {action_name, job_type}:
            if not handler_key or handler_key in registered:
                continue
            if handler_key in ExecutionSpine._handlers:
                logger.debug(
                    "Skipping Lotus handler for %s (already registered)",
                    handler_key,
                )
                registered.add(handler_key)
                continue
            ExecutionSpine.register_handler(
                handler_key, make_handler(plugin_name, action_name))
            registered.add(handler_key)
            count += 1
            logger.debug(
                "Registered Lotus handler %s.%s (mode=%s, job_type=%s)",
                plugin_name,
                action_name,
                mode,
                handler_key,
            )

    if count > 0:
        logger.info(
            f"Registered {count} async handlers from Lotus capabilities.")


def startup_all_plugins() -> None:
    """
    Phase 2: System Start.
    Triggers on_startup hooks after DB and other core services are ready.
    """
    logger.info("Triggering on_startup for all plugins...")
    _reconcile_artifact_types()
    from embeddr.db.session import get_engine
    from contextlib import contextmanager
    from sqlmodel import Session

    @contextmanager
    def _make_session():
        with Session(get_engine()) as s:
            yield s

    context = PluginContext(
        bus=_EVENT_BUS,
        capability_registry=_PLUGIN_CAPABILITY_REGISTRY,
        resources=resource_manager,
        db_session_factory=_make_session,
    )

    # Sort plugins so providers (search, storage, indexer) load before consumers
    _PROVIDER_INTENTS = {
        PluginIntent.PROVIDE_SEARCH,
        PluginIntent.PROVIDE_INDEXER,
        PluginIntent.PROVIDE_STORAGE,
    }

    def sort_key(p):
        return 0 if _PROVIDER_INTENTS & set(p.intents) else 1

    sorted_plugins = sorted(_LOADED_PLUGINS, key=sort_key)

    for plugin in sorted_plugins:
        try:
            if hasattr(plugin, 'on_startup'):
                result = plugin.on_startup(context)
                if inspect.iscoroutine(result):
                    from embeddr_core.async_compat import run_sync
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(result)
                    except RuntimeError:
                        run_sync(result)
        except Exception as e:
            logger.error(f"Failed to startup plugin {plugin.name}: {e}")


def _emit_schema_event(event_type: str, payload: Dict[str, Any]) -> None:
    try:
        _EVENT_BUS.emit(event_type, payload, source="schema")
    except Exception:
        logger.debug("Schema event emit failed", exc_info=True)


def _extract_artifact_type_specs() -> List[Dict[str, Any]]:
    specs: List[Dict[str, Any]] = []
    reg = get_lotus_registry()
    for cap in reg.list(kind=LotusKind.artifact_type):
        data = cap.data or {}
        raw = data.get("types") or data.get("artifact_types") or []
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            continue
        for item in raw:
            if isinstance(item, dict):
                specs.append({"spec": item, "plugin": cap.plugin})
    return specs


def _reconcile_artifact_types() -> None:
    items = _extract_artifact_type_specs()
    if not items:
        return

    pending: List[Dict[str, Any]] = []
    for entry in items:
        try:
            spec = ArtifactTypeSpec.model_validate(entry["spec"])
            pending.append({"spec": spec, "plugin": entry.get("plugin")})
        except Exception as e:
            logger.warning(
                "Invalid artifact type spec from plugin %s: %s (keys: %s)",
                entry.get("plugin", "unknown"),
                e,
                list(entry["spec"].keys()) if isinstance(entry.get("spec"), dict) else "?",
            )

    if not pending:
        return

    created_count = 0
    existing_count = 0
    conflict_count = 0
    skipped_count = 0

    with Session(get_engine()) as session:
        created: set[str] = set()
        for _ in range(len(pending) + 1):
            next_pending: List[Dict[str, Any]] = []
            progress = False

            for entry in pending:
                spec: ArtifactTypeSpec = entry["spec"]
                plugin = entry.get("plugin")

                if spec.parent_name:
                    parent = session.get(ArtifactType, spec.parent_name)
                    if not parent and spec.parent_name not in created:
                        next_pending.append(entry)
                        continue

                existing = session.get(ArtifactType, spec.name)
                if existing:
                    # Stamp registered_by + un-deprecate when a plugin re-registers
                    # one of its types. Catches existing rows from before this
                    # tracking was added, so the orphan-detection pass below
                    # doesn't deprecate types that ARE actively claimed.
                    existing_meta = dict(existing.metadata_json or {})
                    meta_dirty = False
                    if existing_meta.pop("deprecated", None):
                        meta_dirty = True
                    if plugin and existing_meta.get("registered_by") != plugin:
                        existing_meta["registered_by"] = plugin
                        meta_dirty = True
                    if meta_dirty:
                        existing.metadata_json = existing_meta
                        session.add(existing)
                    # Mark as claimed-this-run so the orphan pass below leaves it alone.
                    created.add(spec.name)
                    if spec.parent_name and existing.parent_name != spec.parent_name:
                        conflict_count += 1
                        _emit_schema_event(
                            "schema.artifact_type.conflict",
                            {
                                "name": spec.name,
                                "plugin": plugin,
                                "existing_parent": existing.parent_name,
                                "declared_parent": spec.parent_name,
                            },
                        )
                    else:
                        existing_count += 1
                        _emit_schema_event(
                            "schema.artifact_type.exists",
                            {"name": spec.name, "plugin": plugin},
                        )
                    continue

                # Stamp registered_by so we can detect orphans later when the
                # plugin isn't loaded anymore.
                spec_dict = spec.model_dump()
                spec_meta = dict(spec_dict.get("metadata_json") or {})
                if plugin and "registered_by" not in spec_meta:
                    spec_meta["registered_by"] = plugin
                spec_dict["metadata_json"] = spec_meta
                session.add(ArtifactType(**spec_dict))
                created.add(spec.name)
                progress = True
                created_count += 1
                _emit_schema_event(
                    "schema.artifact_type.created",
                    {"name": spec.name, "plugin": plugin},
                )

            session.commit()

            if not next_pending or not progress:
                pending = next_pending
                break
            pending = next_pending

        for entry in pending:
            spec: ArtifactTypeSpec = entry["spec"]
            skipped_count += 1
            _emit_schema_event(
                "schema.artifact_type.skipped",
                {"name": spec.name, "reason": "missing_parent"},
            )

    # Deprecate orphaned types: ones that no currently-loaded plugin claimed
    # in this reconcile pass. We don't delete — artifacts may still reference
    # them and they need to render. The frontend hides deprecated types from
    # pickers and trees by default.
    #
    # `created` collects type names just-now-created or just-re-registered
    # (the existing-type branch above adds to created when it stamps).
    # We use that as "claimed-this-run" and deprecate everything else.
    deprecated_count = 0
    with Session(get_engine()) as session:
        all_types = session.exec(select(ArtifactType)).all()
        for t in all_types:
            meta = dict(t.metadata_json or {})
            if meta.get("deprecated"):
                continue
            if t.name in created:
                continue
            # Not claimed this run — orphan. Mark deprecated; if its plugin
            # comes back later, the main loop will revive it.
            meta["deprecated"] = True
            t.metadata_json = meta
            session.add(t)
            deprecated_count += 1
        session.commit()

    # Summary line instead of per-type noise
    parts = []
    if created_count:
        parts.append(f"{created_count} created")
    if existing_count:
        parts.append(f"{existing_count} unchanged")
    if conflict_count:
        parts.append(f"{conflict_count} conflicts")
    if skipped_count:
        parts.append(f"{skipped_count} skipped")
    if deprecated_count:
        parts.append(f"{deprecated_count} deprecated")
    if parts:
        logger.info("Artifact types reconciled: %s", ", ".join(parts))
