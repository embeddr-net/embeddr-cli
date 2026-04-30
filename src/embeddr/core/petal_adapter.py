"""Adapter: wrap a petal-style Plugin instance as an EmbeddrPlugin.

The CLI's plugin loader was built around the EmbeddrPlugin ABC. Plugins
written against the petal SDK (`embeddr_petal.Plugin`) use a different
shape — class-attribute declarations and `@action`-decorated methods
instead of `@property` lists. This adapter translates one to the other
so both contracts can coexist in the loader without porting every plugin.
"""
from __future__ import annotations

import inspect
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from embeddr_core.plugin_interface import (
    DockComponent,
    EmbeddrPlugin,
    FrontendComponent,
    PageComponent,
    PanelComponent,
    PanelUI,
    PluginAction,
    WidgetComponent,
)
from embeddr_core.models.artifact_type import ArtifactTypeSpec
from embeddr_core.models.lotus import LotusAction, LotusCapability, LotusKind

logger = logging.getLogger("embeddr.core.petal_adapter")


def _inline_schema_refs(schema: Any) -> Any:
    """Inline `$ref` references in a Pydantic-generated JSON Schema.

    Pydantic's `model_json_schema()` emits nested types as `{"$ref": "#/$defs/X"}`
    with a top-level `$defs` map. The frontend's form renderer doesn't follow
    refs — it just sees an opaque pointer and renders nothing. Inlining produces
    a flat, self-contained schema the renderer understands.
    """
    if not isinstance(schema, dict):
        return schema
    defs = schema.get("$defs") or schema.get("definitions") or {}
    if not defs:
        return schema

    def _resolve(node: Any, seen: set[str]) -> Any:
        if isinstance(node, dict):
            if "$ref" in node and isinstance(node["$ref"], str):
                ref = node["$ref"]
                # Only handle internal refs.
                for prefix in ("#/$defs/", "#/definitions/"):
                    if ref.startswith(prefix):
                        name = ref[len(prefix):]
                        if name in seen:
                            # Cycle — leave the ref in place rather than recurse forever.
                            return {"type": "object"}
                        target = defs.get(name)
                        if target is None:
                            return {}
                        return _resolve(target, seen | {name})
                return node
            return {k: _resolve(v, seen) for k, v in node.items() if k not in ("$defs", "definitions")}
        if isinstance(node, list):
            return [_resolve(item, seen) for item in node]
        return node

    return _resolve(schema, set())


class PetalContextShim:
    """Petal-shaped context wrapper around the CLI's PluginContext.

    Petal `@action` methods are written assuming the plugin runs in a separate
    process and talks to the server via `ctx.client` (an EmbeddrClient). In the
    in-process CLI, there's no separate process — but we keep the same authoring
    contract by giving `ctx.client` a real EmbeddrClient pointed at the running
    server's HTTP layer (localhost). Plugins make actual HTTP calls to our own
    server. Wasteful in CPU (loopback) but architecturally clean: the same
    plugin code runs identically in-process or remote.

    Other attribute reads fall through to the underlying CLI PluginContext.
    """

    _client_singleton: Any = None

    def __init__(
        self,
        cli_ctx: Any,
        plugin_name: str,
        config: Optional[Dict[str, Any]] = None,
    ) -> None:
        self._cli_ctx = cli_ctx
        self._plugin_name = plugin_name
        self.config: Dict[str, Any] = dict(config or {})

    @property
    def client(self) -> Any:
        """Lazy EmbeddrClient pointed at the running server."""
        if PetalContextShim._client_singleton is None:
            try:
                from embeddr_client import EmbeddrClient
            except ImportError as exc:
                raise RuntimeError(
                    "petal plugin uses ctx.client but embeddr_client is not "
                    "installed. Run: uv pip install -e core/embeddr-client-python"
                ) from exc
            host = os.environ.get("EMBEDDR_HOST", "127.0.0.1")
            port = os.environ.get("EMBEDDR_PORT", "8003")
            api_key = os.environ.get("EMBEDDR_API_KEY", "")
            PetalContextShim._client_singleton = EmbeddrClient(
                f"http://{host}:{port}",
                api_key=(api_key or None),
            )
        return PetalContextShim._client_singleton

    @property
    def bus(self) -> Any:
        return getattr(self._cli_ctx, "bus", None)

    def emit(
        self,
        event_type: str,
        payload: Optional[Dict[str, Any]] = None,
        source: str = "plugin",
    ) -> None:
        bus = self.bus
        if bus is None:
            return
        try:
            bus.emit(event_type, payload, source)
        except TypeError:
            # Some buses take (event_type, payload) only.
            bus.emit(event_type, payload)

    def __getattr__(self, name: str) -> Any:
        # Falls through for anything we don't override (db_session_factory,
        # capability_registry, resources, etc.) so legacy-shaped accesses keep working.
        cli_ctx = object.__getattribute__(self, "_cli_ctx")
        return getattr(cli_ctx, name)


def is_petal_plugin_class(obj: Any) -> bool:
    """Return True if obj is a subclass of embeddr_petal.Plugin (and not the base)."""
    try:
        from embeddr_petal import Plugin as PetalPlugin
    except Exception:
        return False
    return (
        inspect.isclass(obj)
        and issubclass(obj, PetalPlugin)
        and obj is not PetalPlugin
    )


class PetalEmbeddrAdapter(EmbeddrPlugin):
    """Wraps an instantiated petal Plugin and exposes the EmbeddrPlugin interface."""

    def __init__(self, petal_plugin: Any) -> None:
        self._petal = petal_plugin
        self._action_map: Dict[str, Callable] = {}
        # Discover @action / @query decorated methods (they carry _embeddr_action)
        for _attr_name, attr in inspect.getmembers(petal_plugin):
            meta = getattr(attr, "_embeddr_action", None)
            if meta is not None and getattr(meta, "cap_id", None):
                self._action_map[meta.cap_id] = attr

    # ── Identity ────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return getattr(self._petal, "name", "") or self._petal.__class__.__name__

    @property
    def version(self) -> str:
        return getattr(self._petal, "version", "0.0.0") or "0.0.0"

    @property
    def description(self) -> str:
        return getattr(self._petal, "description", "") or ""

    # ── Type ontology ──────────────────────────────────────────────

    @property
    def provided_artifact_types(self) -> List[ArtifactTypeSpec]:
        result: List[ArtifactTypeSpec] = []
        for td in getattr(self._petal, "types", None) or []:
            metadata: Dict[str, Any] = {}
            if getattr(td, "metadata_schema", None):
                metadata["schema"] = td.metadata_schema
            if getattr(td, "preview_component", ""):
                metadata["preview_component"] = td.preview_component
            if getattr(td, "detail_component", ""):
                metadata["detail_component"] = td.detail_component
            if getattr(td, "icon", ""):
                metadata["icon"] = td.icon
            if getattr(td, "embedding_strategy", ""):
                metadata["embedding_strategy"] = td.embedding_strategy
            if getattr(td, "tags", None):
                metadata["tags"] = list(td.tags)
            result.append(
                ArtifactTypeSpec(
                    name=td.name,
                    parent_name=(td.parent or None),
                    description=(td.description or None),
                    default_capabilities=list(getattr(td, "capabilities", []) or []),
                    metadata_json=metadata,
                )
            )
        return result

    # ── UI surfaces ────────────────────────────────────────────────

    @property
    def panels(self) -> List[PanelComponent]:
        result: List[PanelComponent] = []
        for p in getattr(self._petal, "panels", None) or []:
            options = None
            opts_raw = getattr(p, "options", None)
            if isinstance(opts_raw, dict) and opts_raw:
                allowed = {
                    k: v for k, v in opts_raw.items()
                    if k in PanelUI.model_fields
                }
                if allowed:
                    try:
                        options = PanelUI(**allowed)
                    except Exception as exc:
                        logger.debug(
                            "PetalAdapter: dropped invalid panel options for %s: %s",
                            getattr(p, "id", "?"), exc,
                        )
            result.append(
                PanelComponent(
                    name=p.id,
                    component=p.component,
                    label=(p.title or None),
                    icon=(p.icon or None),
                    props=dict(p.props or {}),
                    options=options,
                )
            )
        return result

    @property
    def widgets(self) -> List[WidgetComponent]:
        result: List[WidgetComponent] = []
        for w in getattr(self._petal, "widgets", None) or []:
            result.append(
                WidgetComponent(
                    name=w.id,
                    component=w.component,
                    label=(w.title or None),
                    icon=(w.icon or None),
                    slot=getattr(w, "slot", None) or None,
                    props=dict(w.props or {}),
                )
            )
        return result

    @property
    def docks(self) -> List[DockComponent]:
        result: List[DockComponent] = []
        for d in getattr(self._petal, "docks", None) or []:
            result.append(
                DockComponent(
                    name=d.id,
                    component=d.component,
                    label=(d.title or None),
                    icon=(d.icon or None),
                    props=dict(d.props or {}),
                )
            )
        return result

    @property
    def renderables(self) -> List[Dict[str, Any]]:
        """Petal `renderables` exposed as serializable dicts.

        zen-shell's plugin loader reads `manifest.renderables` and registers
        BOTH the renderable component and a reactive context derived from
        the same fields (sources/event_types/uri_prefix/match_type/etc.).
        Without exposing these, dragging a reactive URI into MediaFrame
        finds no matching context and renders nothing.
        """
        result: List[Dict[str, Any]] = []
        for r in getattr(self._petal, "renderables", None) or []:
            entry: Dict[str, Any] = {
                "id": r.id,
                "component": r.component,
                "thumbnail_component": getattr(r, "thumbnail_component", "") or "",
                "match_type": getattr(r, "match_type", "") or "",
                "uri_prefix": getattr(r, "uri_prefix", "") or "",
                "aliases": list(getattr(r, "aliases", []) or []),
                "priority": getattr(r, "priority", 50),
                "sources": list(getattr(r, "sources", []) or []),
                "event_types": list(getattr(r, "event_types", []) or []),
                "processing_types": list(getattr(r, "processing_types", []) or []),
                "done_types": list(getattr(r, "done_types", []) or []),
                "error_types": list(getattr(r, "error_types", []) or []),
                "artifact_id_paths": list(getattr(r, "artifact_id_paths", []) or []),
                "preview_paths": list(getattr(r, "preview_paths", []) or []),
                "prefer_final_artifact_only": bool(getattr(r, "prefer_final_artifact_only", False)),
                "presentation": dict(getattr(r, "presentation", {}) or {}),
            }
            result.append(entry)
        return result

    @property
    def frontend_components(self) -> List[FrontendComponent]:
        """Petal `components` (effects/overlays/sidebar items) → frontend_components.

        Petal's Component is the generic location-based UI registration: an
        effect lives at `location="zen-effect"`, a sidebar item at
        `"zen-sidebar"`, etc. The frontend already enumerates
        `plugin.frontend_components` to mount these.
        """
        result: List[FrontendComponent] = []
        for c in getattr(self._petal, "components", None) or []:
            result.append(
                FrontendComponent(
                    name=c.id,
                    component=c.component,
                    location=c.location or "",
                    label=(c.label or None),
                    icon=(c.icon or None),
                    props=dict(c.props or {}),
                )
            )
        return result

    @property
    def pages(self) -> List[PageComponent]:
        result: List[PageComponent] = []
        for pg in getattr(self._petal, "pages", None) or []:
            result.append(
                PageComponent(
                    name=pg.id,
                    component=pg.component,
                    route=pg.route,
                    label=(pg.title or None),
                    icon=(pg.icon or None),
                    props=dict(pg.props or {}),
                )
            )
        return result

    @property
    def actions(self) -> List[PluginAction]:
        result: List[PluginAction] = []
        for cap_id, fn in self._action_map.items():
            meta = fn._embeddr_action
            tail = cap_id.split(".")[-1]
            label = tail.replace("_", " ").replace("-", " ").strip().title() or cap_id
            result.append(
                PluginAction(
                    name=cap_id,
                    label=label,
                    description=meta.description or "",
                    payload_schema=(meta.inputs or None),
                    requires_confirmation=False,
                    tier="user",
                )
            )
        return result

    # ── Dispatch ───────────────────────────────────────────────────

    def execute(
        self,
        action_name: str,
        execution_id: Any = None,
        inputs: Optional[Dict[str, Any]] = None,
        context: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Dispatch a Lotus action to the underlying petal @action method.

        The incoming `context` is the CLI's PluginContext; we wrap it in a
        PetalContextShim so the petal action sees the petal-shaped attrs
        (`.client`, `.emit`, `.config`) it expects.
        """
        fn = self._action_map.get(action_name)
        if fn is None:
            return {"ok": False, "error": f"No action registered: {action_name}"}

        # Use the same shim instance across lifecycle + execute, so any
        # context reference the plugin stashed in on_load/on_startup
        # remains valid here and vice versa. Refresh config on each call
        # in case it changed.
        petal_ctx = self._ensure_shim(context)
        try:
            petal_ctx.config = self._resolve_plugin_config()
        except Exception:
            pass

        try:
            return fn(petal_ctx, **(inputs or {}))
        except TypeError as exc:
            logger.error(
                "PetalAdapter: signature mismatch invoking %s on %s: %s",
                action_name, self.name, exc,
            )
            return {"ok": False, "error": f"Bad inputs for {action_name}: {exc}"}

    def _resolve_plugin_config(self) -> Dict[str, Any]:
        """Look up this plugin's persisted config, fall back to model defaults."""
        try:
            from embeddr_core.services.config_service import resolve_plugin_config
            from embeddr.db.session import get_engine
            from sqlmodel import Session
            with Session(get_engine()) as session:
                cfg = resolve_plugin_config(
                    session=session,
                    plugin_name=self.name,
                    config_id=f"{self.name}.config",
                )
                if isinstance(cfg, dict) and cfg:
                    return cfg
        except Exception as exc:
            logger.debug("PetalAdapter: config lookup failed for %s: %s", self.name, exc)

        # Fall back to config_model defaults if the plugin declares one.
        config_model = getattr(self._petal, "config_model", None)
        if config_model is not None:
            try:
                return config_model().model_dump(mode="json")
            except Exception:
                pass
        return {}

    # ── Lotus capability registration ──────────────────────────────

    def build_lotus_capabilities(self) -> List[LotusCapability]:
        """Translate petal declarations into Lotus capabilities.

        Two kinds get produced:
          - One `kind=action` per @action method (so /api/lotus/{cap_id} resolves)
          - One `kind=artifact_type` per plugin (so the reconciler picks up TypeDefs)
        """
        caps: List[LotusCapability] = []
        plugin_name = self.name

        # Capabilities — one per @action / @query / @resolver method.
        # Resolvers need a different LotusKind + slot + adapter match data so
        # the resource adapter selector at /api/v1/resources/resolve can find
        # them. Plain actions and queries register as kind=action.
        for cap_id, fn in self._action_map.items():
            meta = fn._embeddr_action
            tail = cap_id.split(".")[-1]
            title = tail.replace("_", " ").replace("-", " ").strip().title() or cap_id
            petal_kind = getattr(meta, "kind", "action") or "action"
            tags = list(getattr(meta, "tags", []) or [])

            if petal_kind == "resolver":
                # cap_id format from @resolver: "resolve:<uri_prefix>" — extract
                # the prefix so the adapter selector can match URLs.
                uri_prefix = (
                    cap_id[len("resolve:"):] if cap_id.startswith("resolve:") else ""
                )
                if "resolver" not in tags:
                    tags.append("resolver")
                caps.append(
                    LotusCapability(
                        id=cap_id,
                        kind=LotusKind.resolver,
                        slot="resource.adapter",
                        title=title or f"Resolve {uri_prefix}",
                        description=meta.description or "",
                        plugin=plugin_name,
                        version=self.version,
                        tags=tags,
                        action=LotusAction(
                            action=cap_id,
                            exec={"mode": "sync"},
                            expose={"lotus": True, "api": True},
                            input=(meta.inputs or None),
                            output=(meta.outputs or None),
                        ),
                        data={
                            "expose": {"lotus": True, "api": True},
                            "adapter": {
                                "match": {
                                    "uri_prefixes": [uri_prefix] if uri_prefix else [],
                                    "url_prefixes": [uri_prefix] if uri_prefix else [],
                                },
                            },
                        },
                    )
                )
                continue

            caps.append(
                LotusCapability(
                    id=cap_id,
                    kind=LotusKind.action,
                    title=title,
                    description=meta.description or "",
                    plugin=plugin_name,
                    version=self.version,
                    tags=tags,
                    action=LotusAction(
                        action=cap_id,
                        exec={"mode": "sync"},
                        expose={"lotus": True, "api": True},
                        input=(meta.inputs or None),
                        output=(meta.outputs or None),
                    ),
                )
            )

        # Artifact-type capability — bundles every TypeDef the plugin declares.
        # The reconciler reads `data["types"]` from kind=artifact_type capabilities
        # and creates the rows in the artifact_types table.
        type_specs: List[Dict[str, Any]] = []
        for td in getattr(self._petal, "types", None) or []:
            metadata: Dict[str, Any] = {}
            if getattr(td, "metadata_schema", None):
                metadata["schema"] = td.metadata_schema
            if getattr(td, "preview_component", ""):
                metadata["preview_component"] = td.preview_component
            if getattr(td, "detail_component", ""):
                metadata["detail_component"] = td.detail_component
            if getattr(td, "icon", ""):
                metadata["icon"] = td.icon
            if getattr(td, "embedding_strategy", ""):
                metadata["embedding_strategy"] = td.embedding_strategy
            if getattr(td, "tags", None):
                metadata["tags"] = list(td.tags)
            type_specs.append({
                "name": td.name,
                "parent_name": (td.parent or None),
                "description": (td.description or None),
                "default_capabilities": list(getattr(td, "capabilities", []) or []),
                "metadata_json": metadata,
            })

        if type_specs:
            caps.append(
                LotusCapability(
                    id=f"{plugin_name}.types",
                    kind=LotusKind.artifact_type,
                    title=f"{plugin_name} artifact types",
                    description=f"Artifact types declared by {plugin_name}",
                    plugin=plugin_name,
                    version=self.version,
                    data={"types": type_specs},
                )
            )

        # Config capability — built from petal's `config_model` Pydantic class.
        # Without this the frontend's plugin config UI sees nothing and the
        # plugin can't load/save its settings.
        config_model = getattr(self._petal, "config_model", None)
        if config_model is not None:
            try:
                schema = _inline_schema_refs(config_model.model_json_schema())
            except Exception as exc:
                logger.warning(
                    "PetalAdapter: failed to derive JSON schema from config_model on %s: %s",
                    plugin_name, exc,
                )
                schema = None

            if schema:
                # Defaults: instantiate the model with no args; if any field is
                # required, fall back to {} so the UI still has something to bind.
                defaults: Dict[str, Any] = {}
                try:
                    defaults = config_model().model_dump(mode="json")
                except Exception:
                    defaults = {}

                config_ui = getattr(self._petal, "config_ui", {}) or {}
                config_renderer = getattr(self._petal, "config_renderer", "") or ""
                config_scope = getattr(self._petal, "config_scope", "global") or "global"

                config_id = f"{plugin_name}.config"
                config_data: Dict[str, Any] = {
                    "type": "config",
                    "plugin": plugin_name,
                    "scope": config_scope,
                    "input": {
                        "schema": schema,
                        "defaults": defaults,
                        "ui": config_ui,
                    },
                }
                if config_renderer:
                    config_data["renderer"] = config_renderer

                caps.append(
                    LotusCapability(
                        id=config_id,
                        kind=LotusKind.config,
                        title=f"{plugin_name} configuration",
                        description=f"Configuration for {plugin_name}",
                        plugin=plugin_name,
                        version=self.version,
                        tags=["config", plugin_name],
                        data=config_data,
                    )
                )

        return caps

    # ── Lifecycle pass-through ─────────────────────────────────────
    #
    # Petal plugins frequently save the context reference they receive in
    # on_load / on_startup and call `.emit()` / `.client.*` on it from
    # background tasks much later (e.g. comfyui's WebSocket monitor). The
    # context they save MUST be the petal-shaped shim, otherwise those
    # later calls hit the raw CLI PluginContext (which has no `.emit`,
    # `.client`, etc.) and crash. We persist a single shim per adapter
    # instance and reuse it across all lifecycle hooks + execute() calls.

    def _ensure_shim(self, cli_ctx: Optional[Any]) -> "PetalContextShim":
        existing = getattr(self, "_petal_ctx_shim", None)
        if existing is not None:
            # Update the wrapped CLI context in case a different one is
            # passed later (most code paths reuse the same instance, but
            # be defensive).
            existing._cli_ctx = cli_ctx if cli_ctx is not None else existing._cli_ctx
            return existing
        shim = PetalContextShim(
            cli_ctx=cli_ctx,
            plugin_name=self.name,
            config=self._resolve_plugin_config(),
        )
        self._petal_ctx_shim = shim
        return shim

    def on_load(self, context: Optional[Any] = None) -> None:
        if hasattr(self._petal, "on_load"):
            try:
                self._petal.on_load(self._ensure_shim(context))
            except Exception as exc:
                logger.warning(
                    "PetalAdapter: on_load failed for %s: %s", self.name, exc,
                )

    def on_startup(self, context: Optional[Any] = None) -> None:
        if hasattr(self._petal, "on_startup"):
            try:
                self._petal.on_startup(self._ensure_shim(context))
            except Exception as exc:
                logger.warning(
                    "PetalAdapter: on_startup failed for %s: %s", self.name, exc,
                )

    def on_shutdown(self) -> None:
        if hasattr(self._petal, "on_shutdown"):
            try:
                self._petal.on_shutdown()
            except Exception as exc:
                logger.warning(
                    "PetalAdapter: on_shutdown failed for %s: %s", self.name, exc,
                )
