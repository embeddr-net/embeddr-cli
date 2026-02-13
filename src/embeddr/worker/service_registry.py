"""
Worker Service Registry — generic endpoint registration for plugins.

Plugins register their own HTTP service endpoints during ``on_load()``.
The worker app mounts them after plugin initialisation.  The main instance
can introspect available services via the manifest advertised in the
worker registration message.

This keeps the worker app completely plugin-agnostic: it never knows
*what* a plugin serves, only *that* a plugin has something to serve.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("embeddr.worker.service_registry")


@dataclass
class ServiceEndpoint:
    """A single HTTP endpoint registered by a plugin."""

    capability: str    # e.g. "features.embeddings.generate"
    path: str          # e.g. "/embed"
    method: str        # HTTP verb — "POST", "GET", …
    handler: Callable  # The (sync or async) handler function
    summary: str = ""  # OpenAPI summary for docs
    plugin_name: str = ""


class WorkerServiceRegistry:
    """Singleton registry of plugin-provided HTTP service endpoints.

    Lifecycle
    ---------
    1. **Plugin ``on_load()``** — calls ``register()`` for each endpoint.
    2. **Worker ``app.py``** — calls ``mount_routes(app)`` after all plugins
       have loaded, adding every registered endpoint to the FastAPI app.
    3. **Worker registration** — ``get_service_manifest()`` is included in the
       ``worker:register`` WebSocket message so the main instance knows which
       service paths are available on each worker.
    """

    _instance: WorkerServiceRegistry | None = None

    def __init__(self) -> None:
        self._endpoints: list[ServiceEndpoint] = []

    # ── Singleton access ──────────────────────────────────────────────

    @classmethod
    def get(cls) -> WorkerServiceRegistry:
        """Return (or create) the singleton instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Tear down the singleton (useful for tests)."""
        cls._instance = None

    # ── Plugin-facing API ─────────────────────────────────────────────

    def register(
        self,
        *,
        capability: str,
        path: str,
        handler: Callable,
        method: str = "POST",
        summary: str = "",
        plugin_name: str = "",
    ) -> None:
        """Register an HTTP endpoint provided by a plugin.

        Parameters
        ----------
        capability:
            The logical capability string that maps to this endpoint,
            e.g. ``"features.embeddings.generate"``.
        path:
            The URL path the endpoint should be mounted at (e.g. ``"/embed"``).
        handler:
            Sync or async function to handle the request.  FastAPI signature
            rules apply (Pydantic body models, dependency injection, etc.).
        method:
            HTTP verb.  Defaults to ``"POST"``.
        summary:
            Optional OpenAPI summary string.
        plugin_name:
            Name of the plugin that owns this endpoint.
        """
        ep = ServiceEndpoint(
            capability=capability,
            path=path,
            method=method,
            handler=handler,
            summary=summary,
            plugin_name=plugin_name,
        )
        self._endpoints.append(ep)
        logger.info(
            "Registered service endpoint: %s %s  (capability=%s, plugin=%s)",
            method, path, capability, plugin_name,
        )

    # ── Worker-app-facing API ─────────────────────────────────────────

    def mount_routes(self, app: Any) -> int:
        """Mount all registered endpoints on a FastAPI *app*.

        Returns the number of routes mounted.
        """
        count = 0
        for ep in self._endpoints:
            app.add_api_route(
                ep.path,
                ep.handler,
                methods=[ep.method],
                summary=ep.summary or f"Worker service: {ep.capability}",
                name=f"worker_svc_{ep.capability.replace('.', '_')}",
            )
            count += 1
            logger.info("Mounted worker route: %s %s", ep.method, ep.path)
        return count

    # ── Introspection ─────────────────────────────────────────────────

    def get_service_manifest(self) -> dict[str, dict[str, str]]:
        """Return a manifest mapping capability → endpoint metadata.

        Included in the ``worker:register`` message so the main instance
        can discover which HTTP paths each worker exposes.
        """
        manifest: dict[str, dict[str, str]] = {}
        for ep in self._endpoints:
            manifest[ep.capability] = {
                "path": ep.path,
                "method": ep.method,
                "plugin": ep.plugin_name,
            }
        return manifest

    @property
    def endpoints(self) -> list[ServiceEndpoint]:
        """All registered endpoints (read-only snapshot)."""
        return list(self._endpoints)
