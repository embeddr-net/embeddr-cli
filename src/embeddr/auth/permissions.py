"""Centralized permission constants for the Embeddr platform.

All permission strings should be defined here to avoid typos and
enable IDE auto-complete. Permission format is `domain:action` with
optional wildcard support (`domain:*`, `*`).

Usage:
    from embeddr.auth.permissions import Permissions

    @router.get("/items", dependencies=[Depends(require_permission(Permissions.ARTIFACTS_READ))])
    def list_items(): ...
"""

from __future__ import annotations


class Permissions:
    """All known permission strings in a single place."""

    # ── Wildcard ────────────────────────────────────────────────
    ALL = "*"

    # ── Artifacts ───────────────────────────────────────────────
    ARTIFACTS_READ = "artifacts:read"
    ARTIFACTS_WRITE = "artifacts:write"

    # ── Collections ─────────────────────────────────────────────
    COLLECTIONS_READ = "collections:read"
    COLLECTIONS_WRITE = "collections:write"

    # ── Executions ──────────────────────────────────────────────
    EXECUTIONS_READ = "executions:read"
    EXECUTIONS_WRITE = "executions:write"

    # ── Workflows ───────────────────────────────────────────────
    WORKFLOWS_READ = "workflows:read"
    WORKFLOWS_WRITE = "workflows:write"

    # ── Config (analysis + plugin config) ───────────────────────
    CONFIG_READ = "config:read"
    CONFIG_WRITE = "config:write"

    # ── Maintenance ─────────────────────────────────────────────
    MAINTENANCE_READ = "maintenance:read"
    MAINTENANCE_WRITE = "maintenance:write"

    # ── Resources ───────────────────────────────────────────────
    RESOURCES_READ = "resources:read"
    RESOURCES_WRITE = "resources:write"

    # ── Projections (UMAP / embeddings) ─────────────────────────
    PROJECTIONS_READ = "projections:read"

    # ── Plugins ─────────────────────────────────────────────────
    PLUGINS_READ = "plugins:read"
    PLUGINS_WRITE = "plugins:write"

    # ── Actions (dispatch) ──────────────────────────────────────
    ACTIONS_READ = "actions:read"
    ACTIONS_WRITE = "actions:write"

    # ── System ──────────────────────────────────────────────────
    SYSTEM_READ = "system:read"
    SYSTEM_WRITE = "system:write"

    # ── Panels ──────────────────────────────────────────────────
    PANELS_READ = "panels:read"
    PANELS_WRITE = "panels:write"

    # ── Themes ──────────────────────────────────────────────────
    THEMES_READ = "themes:read"

    # ── Workers ─────────────────────────────────────────────────
    WORKERS_READ = "workers:read"
    WORKERS_WRITE = "workers:write"

    # ── Security / Auth management ──────────────────────────────
    SECURITY_READ = "security:read"
    SECURITY_WRITE = "security:write"

    # ── Keys (self-service) ─────────────────────────────────────
    KEYS_CREATE_SELF = "keys:create:self"

    # ── Lotus ───────────────────────────────────────────────────
    LOTUS_LIST = "lotus:list"
    LOTUS_DISPATCH = "lotus:dispatch"
    LOTUS_ALL = "lotus:*"

    @staticmethod
    def lotus_capability(cap_id: str) -> str:
        """Dynamic per-capability permission."""
        return f"lotus:capability:{cap_id}"

    # ── Preset role definitions ─────────────────────────────────

    @classmethod
    def admin_permissions(cls) -> set[str]:
        """All permissions — the admin wildcard."""
        return {cls.ALL}

    @classmethod
    def viewer_permissions(cls) -> set[str]:
        """Read-only across most domains."""
        return {
            cls.ARTIFACTS_READ,
            cls.COLLECTIONS_READ,
            cls.EXECUTIONS_READ,
            cls.WORKFLOWS_READ,
            cls.CONFIG_READ,
            cls.RESOURCES_READ,
            cls.PROJECTIONS_READ,
            cls.PLUGINS_READ,
            cls.SYSTEM_READ,
            cls.PANELS_READ,
            cls.THEMES_READ,
            cls.LOTUS_LIST,
        }

    @classmethod
    def editor_permissions(cls) -> set[str]:
        """Read + write for content domains, read for system."""
        return cls.viewer_permissions() | {
            cls.ARTIFACTS_WRITE,
            cls.COLLECTIONS_WRITE,
            cls.EXECUTIONS_WRITE,
            cls.WORKFLOWS_WRITE,
            cls.CONFIG_WRITE,
            cls.PANELS_WRITE,
            cls.ACTIONS_WRITE,
            cls.LOTUS_DISPATCH,
            cls.KEYS_CREATE_SELF,
        }

    @classmethod
    def all_permissions(cls) -> list[str]:
        """Return every defined permission constant (useful for docs/debug)."""
        return sorted(
            v for k, v in vars(cls).items()
            if isinstance(v, str) and not k.startswith("_") and k == k.upper()
        )
