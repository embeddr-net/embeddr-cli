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

    # ── Service Clients ───────────────────────────────────────
    SERVICE_CLIENTS_READ = "service-clients:read"
    SERVICE_CLIENTS_WRITE = "service-clients:write"

    # ── Auth Token ────────────────────────────────────────────
    AUTH_TOKEN = "auth:token"

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
    def service_permissions(cls) -> set[str]:
        """Default permissions for service clients (read + write artifacts, execute)."""
        return {
            cls.ARTIFACTS_READ,
            cls.ARTIFACTS_WRITE,
            cls.COLLECTIONS_READ,
            cls.EXECUTIONS_READ,
            cls.EXECUTIONS_WRITE,
            cls.PLUGINS_READ,
            cls.SYSTEM_READ,
            cls.LOTUS_LIST,
            cls.LOTUS_DISPATCH,
        }

    @classmethod
    def all_permissions(cls) -> list[str]:
        """Return every defined permission constant (useful for docs/debug)."""
        return sorted(
            v for k, v in vars(cls).items()
            if isinstance(v, str) and not k.startswith("_") and k == k.upper()
        )


# Human-readable descriptions for OAuth2 consent screen
SCOPE_DESCRIPTIONS: dict[str, str] = {
    "artifacts:read": "View your artifacts and media",
    "artifacts:write": "Create, edit, and delete artifacts",
    "collections:read": "View your collections",
    "collections:write": "Create and edit collections",
    "executions:read": "View execution history",
    "executions:write": "Run actions and workflows",
    "workflows:read": "View workflow definitions",
    "workflows:write": "Create and edit workflows",
    "config:read": "View configuration",
    "config:write": "Change configuration",
    "plugins:read": "View installed plugins",
    "plugins:write": "Manage plugins",
    "system:read": "View system information",
    "system:write": "Change system settings",
    "panels:read": "View panel sessions",
    "panels:write": "Create and manage panels",
    "resources:read": "View resources",
    "resources:write": "Manage resources",
    "maintenance:read": "View maintenance status",
    "maintenance:write": "Run maintenance tasks",
    "workers:read": "View worker status",
    "workers:write": "Manage workers",
    "actions:read": "View available actions",
    "actions:write": "Dispatch actions",
    "security:read": "View security settings",
    "security:write": "Manage security settings",
    "projections:read": "View embedding projections",
    "themes:read": "View themes",
    "lotus:list": "List available capabilities",
    "lotus:dispatch": "Invoke capabilities",
    "lotus:*": "Full capability access",
    "keys:create:self": "Create personal API keys",
    "service-clients:read": "View service clients",
    "service-clients:write": "Manage service clients",
    "*": "Full access to all features",
}
