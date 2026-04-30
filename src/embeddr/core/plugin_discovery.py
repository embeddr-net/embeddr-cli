from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from embeddr.core.config import get_data_dir

logger = logging.getLogger("embeddr.core.plugin_discovery")


PLUGIN_CATEGORY_FOLDERS: set[str] = {
    "core",
    "editors",
    "examples",
    "experimental",
    "features",
    "integrations",
    "development",
    "pages",
    "private",
    "plugins",
    "private_plugins",
    "services",
    "storage",
    "tools",
    "transports",
    "types",
    "widgets",
    "effects",
}


# Built-in plugin profiles — no embeddr.toml needed.
# Plugins are matched by directory name.
BUILTIN_PROFILES: dict[str, set[str]] = {
    "release": {
        "embeddr-core",
        "embeddr-lotus",
        "embeddr-search",
        "embeddr-embeddings",
        "embeddr-llm",
        "embeddr-thumbnailer",
        "embeddr-storage-local",
        "embeddr-storage-s3",
        "embeddr-editor-types",
        "embeddr-comfyui",
        "embeddr-transport-mcp",
        "embeddr-transport-graphql",
        "embeddr-media-tools",
    },
    "minimal": {
        "embeddr-core",
        "embeddr-lotus",
        "embeddr-storage-local",
    },
}


@dataclass
class PluginFilter:
    """Determines which plugins should be loaded.

    Modes:
      - "all": load everything (default)
      - "allowlist": only load plugins in the set
      - "denylist": load everything except plugins in the set
    """
    mode: str = "all"
    plugins: set[str] = field(default_factory=set)
    profile_name: str = ""

    def should_load(self, plugin_dir_name: str) -> bool:
        if self.mode == "all":
            return True
        if self.mode == "allowlist":
            return plugin_dir_name in self.plugins
        if self.mode == "denylist":
            return plugin_dir_name not in self.plugins
        return True


def _parse_csv(raw: str) -> set[str]:
    return {item.strip() for item in raw.split(",") if item.strip()}


def resolve_plugin_filter() -> PluginFilter:
    """Build a PluginFilter from env vars, embeddr.toml, and disabled_plugins.json.

    Resolution order (first match wins):
      1. EMBEDDR_PLUGIN_PROFILE env var → named profile from embeddr.toml
      2. EMBEDDR_ENABLED_PLUGINS env var → explicit allowlist
      3. EMBEDDR_DISABLED_PLUGINS env var + disabled_plugins.json → denylist
      4. [plugins] section in embeddr.toml (enabled/disabled keys)
      5. Default: load all
    """
    # 1. Named profile from env var or CLI
    profile_name = os.environ.get("EMBEDDR_PLUGIN_PROFILE", "").strip()
    if profile_name:
        # Check embeddr.toml first
        profile_plugins = _load_profile_from_toml(profile_name)
        # Then check built-in profiles
        if profile_plugins is None and profile_name in BUILTIN_PROFILES:
            profile_plugins = set(BUILTIN_PROFILES[profile_name])
        if profile_plugins is not None:
            logger.info(
                "Plugin profile '%s': loading %d plugins",
                profile_name, len(profile_plugins),
            )
            return PluginFilter(
                mode="allowlist",
                plugins=profile_plugins,
                profile_name=profile_name,
            )
        else:
            logger.warning(
                "Plugin profile '%s' not found, loading all plugins",
                profile_name,
            )

    # 2. Explicit allowlist env var
    enabled_raw = os.environ.get("EMBEDDR_ENABLED_PLUGINS", "").strip()
    if enabled_raw:
        plugins = _parse_csv(enabled_raw)
        logger.info("EMBEDDR_ENABLED_PLUGINS: loading %d plugins", len(plugins))
        return PluginFilter(mode="allowlist", plugins=plugins)

    # 3. Denylist: env var + file
    disabled = _load_disabled_set()
    if disabled:
        logger.info("Plugin denylist: %d plugins disabled", len(disabled))
        return PluginFilter(mode="denylist", plugins=disabled)

    # 4. embeddr.toml [plugins] section
    toml_filter = _load_filter_from_toml()
    if toml_filter is not None:
        return toml_filter

    # 5. Default
    return PluginFilter(mode="all")


def _load_disabled_set() -> set[str]:
    """Load disabled plugins from env var and disabled_plugins.json."""
    disabled: set[str] = set()

    raw = os.environ.get("EMBEDDR_DISABLED_PLUGINS", "").strip()
    if raw:
        disabled.update(_parse_csv(raw))

    data_root = Path(os.environ.get("EMBEDDR_DATA_DIR") or get_data_dir())
    disabled_path = data_root / "disabled_plugins.json"
    if not disabled_path.exists():
        return disabled

    try:
        payload = json.loads(disabled_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return disabled

    items = payload.get("disabled_plugins") if isinstance(payload, dict) else None
    if isinstance(items, list):
        disabled.update({str(item).strip() for item in items if str(item).strip()})
    return disabled


def _load_profile_from_toml(profile_name: str) -> set[str] | None:
    """Load a named plugin profile from embeddr.toml [plugins.profiles.<name>]."""
    config = _read_project_toml()
    if not config:
        return None

    plugins_section = config.get("plugins")
    if not isinstance(plugins_section, dict):
        return None

    profiles = plugins_section.get("profiles")
    if not isinstance(profiles, dict):
        return None

    profile = profiles.get(profile_name)
    if not isinstance(profile, dict):
        return None

    enabled = profile.get("enabled")
    if isinstance(enabled, list):
        return {str(p).strip() for p in enabled if str(p).strip()}

    return None


def _load_filter_from_toml() -> PluginFilter | None:
    """Load plugin filter from embeddr.toml [plugins] section."""
    config = _read_project_toml()
    if not config:
        return None

    plugins_section = config.get("plugins")
    if not isinstance(plugins_section, dict):
        return None

    # Direct enabled list in [plugins]
    enabled = plugins_section.get("enabled")
    if isinstance(enabled, list) and enabled:
        plugins = {str(p).strip() for p in enabled if str(p).strip()}
        logger.info("embeddr.toml [plugins].enabled: loading %d plugins", len(plugins))
        return PluginFilter(mode="allowlist", plugins=plugins)

    # Direct disabled list in [plugins]
    disabled = plugins_section.get("disabled")
    if isinstance(disabled, list) and disabled:
        plugins = {str(p).strip() for p in disabled if str(p).strip()}
        logger.info("embeddr.toml [plugins].disabled: %d plugins disabled", len(plugins))
        return PluginFilter(mode="denylist", plugins=plugins)

    return None


def _read_project_toml() -> dict | None:
    """Read embeddr.toml from the project root, if available."""
    try:
        from embeddr.core.project import find_project_root, load_project_config

        root = find_project_root()
        if root:
            return load_project_config(root)
    except Exception:
        pass
    return None


def load_disabled_plugins() -> set[str]:
    """Legacy API — returns the denylist set for backward compatibility."""
    return _load_disabled_set()


def iter_plugin_dirs(plugins_dir: Path) -> Iterable[Path]:
    """Yield plugin directories from a plugins root.

    Auto-detects whether a top-level directory is a plugin or a category:
      - has its own `plugin.py` → it's a plugin, yield it
      - has children with `plugin.py` → it's a category, yield each child
      - falls back to the legacy PLUGIN_CATEGORY_FOLDERS allowlist

    This means users can group plugins under any folder name (apps/, dev/,
    effects/, my-grouping/) without editing a hardcoded list.
    """
    if not plugins_dir.exists() or not plugins_dir.is_dir():
        return

    for item in plugins_dir.iterdir():
        if not item.is_dir():
            continue

        # 1. If this directory is itself a plugin, yield it directly.
        if (item / "plugin.py").exists():
            yield item
            continue

        # 2. If any of its children are plugins, treat it as a category.
        has_plugin_children = any(
            (sub / "plugin.py").exists()
            for sub in item.iterdir()
            if sub.is_dir()
        )
        if has_plugin_children or item.name in PLUGIN_CATEGORY_FOLDERS:
            for sub in item.iterdir():
                if sub.is_dir():
                    yield sub
            continue

        # 3. Otherwise it's neither — skip silently.
