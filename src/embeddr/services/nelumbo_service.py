from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
import tempfile
import zipfile
from typing import Any, Dict, List, Optional, cast

from embeddr.core.project import CONFIG_FILENAME, create_default_config, load_project_config
from embeddr.core.plugin_loader import (
    get_loaded_plugins,
    get_lotus_registry,
    load_python_plugins,
)
from embeddr.lotus.core_caps import register_core_lotus_capabilities
from embeddr.db.adapters import get_adapter
from sqlalchemy import text
from sqlalchemy.engine.url import make_url
from sqlalchemy.exc import SQLAlchemyError
import httpx


@dataclass(frozen=True)
class RegistryItem:
    id: str
    name: str
    description: Optional[str]
    author: Optional[str]
    author_url: Optional[str]
    author_profile_url: Optional[str]
    author_logo_url: Optional[str]
    author_icon_url: Optional[str]
    homepage_url: Optional[str]
    repository_url: Optional[str]
    license: Optional[str]
    screenshots: Optional[List[str]]
    logo_url: Optional[str]
    icon_url: Optional[str]
    banner_url: Optional[str]
    tokens: Optional[Dict[str, Dict[str, str]]]
    source: str
    tags: List[str]
    version: Optional[str] = None
    intents: Optional[List[str]] = None
    actions: Optional[List[Dict[str, Any]]] = None
    frontend_actions: Optional[List[Dict[str, Any]]] = None
    frontend_components: Optional[List[Dict[str, Any]]] = None
    panels: Optional[List[Dict[str, Any]]] = None
    config_renderers: Optional[List[Dict[str, Any]]] = None
    pages: Optional[List[Dict[str, Any]]] = None
    widgets: Optional[List[Dict[str, Any]]] = None
    lotus_capabilities: Optional[List[Dict[str, Any]]] = None
    url: Optional[str] = None
    css_url: Optional[str] = None


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[4]


def _local_plugins_dir() -> Path:
    return _repo_root() / "embeddr-plugins" / "dist" / "plugins"


def _local_themes_dir() -> Path:
    return _repo_root() / "embeddr-themes" / "themes"


_REGISTRY_OVERRIDE: Optional[str] = None
_REGISTRY_DISABLED: bool = False


def _registry_base_url() -> Optional[str]:
    if _REGISTRY_DISABLED:
        return None
    if _REGISTRY_OVERRIDE is not None:
        return _REGISTRY_OVERRIDE or None
    raw = os.environ.get("NELUMBO_REGISTRY_URL", "").strip()
    if not raw:
        return None
    return raw.rstrip("/")


def _registry_ui_base_url(base_url: str) -> str:
    override = os.environ.get("NELUMBO_REGISTRY_UI_URL", "").strip()
    if override:
        return override.rstrip("/")
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/ui"):
        return trimmed
    return trimmed


def _prefix_registry_url(value: Optional[str], base_url: Optional[str]) -> Optional[str]:
    if not value:
        return None
    if value.startswith("http://") or value.startswith("https://"):
        return value
    if not base_url:
        return value
    if value.startswith("/"):
        return f"{base_url}{value}"
    return f"{base_url}/{value}"


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower())
    return normalized.strip("-") or "publisher"


def get_registry_config() -> Dict[str, Any]:
    base_url = _registry_base_url()
    return {
        "enabled": bool(base_url),
        "base_url": base_url,
    }


def set_registry_config(*, enabled: bool, base_url: Optional[str]) -> Dict[str, Any]:
    global _REGISTRY_OVERRIDE, _REGISTRY_DISABLED
    if not enabled:
        _REGISTRY_DISABLED = True
        _REGISTRY_OVERRIDE = None
        return get_registry_config()
    _REGISTRY_DISABLED = False
    _REGISTRY_OVERRIDE = (base_url or "").strip().rstrip("/")
    return get_registry_config()


def _fetch_registry_items(path: str) -> List[Dict[str, Any]]:
    base_url = _registry_base_url()
    if not base_url:
        return []
    try:
        with httpx.Client(timeout=5.0) as client:
            response = client.get(f"{base_url}{path}")
            response.raise_for_status()
            data = response.json()
            return data if isinstance(data, list) else []
    except Exception:
        return []


def get_registry_status() -> Dict[str, Any]:
    base_url = _registry_base_url()
    if not base_url:
        return {
            "enabled": False,
            "base_url": None,
            "ok": False,
            "error": "Registry URL not configured",
            "plugins": 0,
            "themes": 0,
        }

    try:
        with httpx.Client(timeout=4.0) as client:
            response = client.get(f"{base_url}/health")
            response.raise_for_status()
    except Exception as exc:
        return {
            "enabled": True,
            "base_url": base_url,
            "ok": False,
            "error": str(exc),
            "plugins": 0,
            "themes": 0,
        }

    plugins = len(list_registry_plugins())
    themes = len(list_registry_themes())
    return {
        "enabled": True,
        "base_url": base_url,
        "ok": True,
        "error": None,
        "plugins": plugins,
        "themes": themes,
    }


def list_local_plugins() -> List[RegistryItem]:
    plugins_dir = _local_plugins_dir()
    if not plugins_dir.exists():
        return []

    items: List[RegistryItem] = []
    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir():
            continue
        if not (entry / "plugin.py").exists():
            continue
        plugin_id = entry.name
        logo_url = _find_plugin_logo(entry, plugin_id)
        items.append(
            RegistryItem(
                id=plugin_id,
                name=plugin_id.replace("-", " ").title(),
                description=f"Local plugin: {plugin_id}",
                author=None,
                author_url=None,
                author_profile_url=None,
                author_logo_url=None,
                author_icon_url=None,
                homepage_url=None,
                repository_url=None,
                license=None,
                screenshots=None,
                logo_url=logo_url,
                icon_url=None,
                banner_url=None,
                tokens=None,
                source="local",
                tags=["local"],
                version=None,
                intents=None,
                actions=None,
                frontend_actions=None,
                frontend_components=None,
                panels=None,
                config_renderers=None,
                pages=None,
                widgets=None,
                lotus_capabilities=None,
                url=None,
                css_url=None,
            )
        )
    return items


def _find_plugin_logo(plugin_dir: Path, plugin_id: str) -> Optional[str]:
    assets_dir = plugin_dir / "assets"
    if not assets_dir.exists():
        return None
    for filename in ("logo.svg", "logo.png", "logo.ico"):
        path = assets_dir / filename
        if path.exists():
            return f"/nelumbo/assets/plugins/{plugin_id}/{filename}"
    return None


def get_local_plugin_asset_path(plugin_id: str, asset_name: str) -> Optional[Path]:
    plugins_dir = _local_plugins_dir()
    plugin_dir = plugins_dir / plugin_id
    assets_dir = plugin_dir / "assets"
    if not assets_dir.exists():
        return None
    asset_path = assets_dir / asset_name
    if not asset_path.exists():
        return None
    return asset_path


def list_local_themes() -> List[RegistryItem]:
    themes_dir = _local_themes_dir()
    if not themes_dir.exists():
        return []

    items: List[RegistryItem] = []
    for entry in sorted(themes_dir.iterdir()):
        if not entry.is_dir():
            continue
        theme_id = entry.name
        theme_name = theme_id.replace("-", " ").title()
        description = f"Local theme: {theme_id}"
        icon_url = None
        banner_url = None
        tokens: Optional[Dict[str, Dict[str, str]]] = None

        theme_file = entry / "theme.json"
        if theme_file.exists():
            try:
                data = json.loads(theme_file.read_text(encoding="utf-8"))
                theme_id = data.get("id") or theme_id
                theme_name = data.get("name") or theme_name
                description = data.get("description") or description
                tokens = data.get("tokens") if isinstance(
                    data.get("tokens"), dict) else None

                icon = data.get("icon") or data.get("iconUrl")
                if icon:
                    icon_url = f"/themes/{entry.name}/{icon}"

                banner = data.get("banner") or data.get("bannerUrl")
                if banner:
                    banner_url = f"/themes/{entry.name}/{banner}"
            except Exception:
                pass
        items.append(
            RegistryItem(
                id=theme_id,
                name=theme_name,
                description=description,
                author=None,
                author_url=None,
                author_profile_url=None,
                author_logo_url=None,
                author_icon_url=None,
                homepage_url=None,
                repository_url=None,
                license=None,
                screenshots=None,
                logo_url=None,
                icon_url=icon_url,
                banner_url=banner_url,
                tokens=tokens,
                source="local",
                tags=["local"],
                version=None,
                intents=None,
                actions=None,
                frontend_actions=None,
                frontend_components=None,
                panels=None,
                config_renderers=None,
                pages=None,
                widgets=None,
                lotus_capabilities=None,
                url=None,
                css_url=None,
            )
        )
    return items


_PLUGINS_INTROSPECTED = False
_EMBEDDR_SERVER_PROCESS: Optional[subprocess.Popen] = None
_EMBEDDR_SERVER_META: Dict[str, Any] = {}


def _ensure_plugins_introspected() -> None:
    global _PLUGINS_INTROSPECTED
    if _PLUGINS_INTROSPECTED:
        return

    plugins_dir = _local_plugins_dir()
    if plugins_dir.exists():
        load_python_plugins(plugins_dir)
        register_core_lotus_capabilities()

    _PLUGINS_INTROSPECTED = True


def list_local_plugins_introspected() -> List[RegistryItem]:
    base_items = {item.id: item for item in list_local_plugins()}
    _ensure_plugins_introspected()

    lotus_caps = get_lotus_registry().list_capabilities()
    caps_by_plugin: Dict[str, List[Dict[str, Any]]] = {}
    for cap in lotus_caps:
        cap_plugin_id = cap.plugin or "unknown"
        caps_by_plugin.setdefault(cap_plugin_id, []).append(
            {
                "id": cap.id,
                "kind": cap.kind.value if hasattr(cap.kind, "value") else str(cap.kind),
                "title": cap.title,
                "description": cap.description,
                "tags": list(cap.tags or []),
                "slot": cap.slot,
                "version": cap.version,
            }
        )

    results: List[RegistryItem] = []
    for plugin in get_loaded_plugins():
        raw_plugin_id = cast(Optional[str], plugin.get(
            "id") or plugin.get("name"))
        plugin_id = str(raw_plugin_id).strip() if raw_plugin_id else ""
        if not plugin_id:
            continue
        base = base_items.get(plugin_id)
        results.append(
            RegistryItem(
                id=plugin_id,
                name=plugin.get("name") or (base.name if base else plugin_id),
                description=plugin.get("description")
                or (base.description if base else None),
                author=plugin.get("author") or (base.author if base else None),
                author_url=plugin.get("author_url")
                or (base.author_url if base else None),
                author_profile_url=None,
                author_logo_url=None,
                author_icon_url=None,
                homepage_url=plugin.get("homepage_url")
                or (base.homepage_url if base else None),
                repository_url=plugin.get("repository_url")
                or (base.repository_url if base else None),
                license=plugin.get("license") or (
                    base.license if base else None),
                screenshots=plugin.get("screenshots")
                or (base.screenshots if base else None),
                logo_url=base.logo_url if base else None,
                icon_url=base.icon_url if base else None,
                banner_url=base.banner_url if base else None,
                tokens=base.tokens if base else None,
                source=base.source if base else "local",
                tags=base.tags if base else ["local"],
                version=plugin.get("version"),
                intents=plugin.get("intents"),
                actions=plugin.get("actions"),
                frontend_actions=plugin.get("frontend_actions"),
                frontend_components=plugin.get("frontend_components"),
                panels=plugin.get("panels"),
                config_renderers=plugin.get("config_renderers"),
                pages=plugin.get("pages"),
                widgets=plugin.get("widgets"),
                lotus_capabilities=caps_by_plugin.get(plugin_id, []),
                url=plugin.get("url"),
                css_url=plugin.get("css_url"),
            )
        )

    for plugin_id, base in base_items.items():
        if any(item.id == plugin_id for item in results):
            continue
        results.append(base)

    return results


def list_registry_plugins() -> List[RegistryItem]:
    items: List[RegistryItem] = []
    base_url = _registry_base_url()
    orgs = _fetch_registry_items("/orgs")
    orgs_by_slug = {
        str(org.get("slug") or "").strip().lower(): org
        for org in orgs
        if isinstance(org, dict) and org.get("slug")
    }
    orgs_by_name = {
        str(org.get("name") or "").strip().lower(): org
        for org in orgs
        if isinstance(org, dict) and org.get("name")
    }

    for item in _fetch_registry_items("/plugins"):
        plugin_id = str(item.get("id") or "").strip()
        if not plugin_id:
            continue
        author = item.get("author")
        org = None
        if isinstance(author, str) and author.strip():
            author_slug = _slugify(author)
            org = orgs_by_slug.get(author_slug)
            if not org:
                org = orgs_by_name.get(author.strip().lower())

        author_profile_url = item.get("author_profile_url")
        if not author_profile_url and org and base_url:
            org_slug = org.get("slug") or _slugify(author or "")
            author_profile_url = f"{_registry_ui_base_url(base_url)}/creators/{org_slug}"

        author_logo_url = item.get(
            "author_logo_url") or item.get("author_icon_url")
        if not author_logo_url and org:
            author_logo_url = org.get("logo_url") or org.get("icon_url")
        author_logo_url = _prefix_registry_url(author_logo_url, base_url)
        author_icon_url = item.get("author_icon_url") or author_logo_url

        items.append(
            RegistryItem(
                id=plugin_id,
                name=item.get("name") or plugin_id,
                description=item.get("description"),
                author=author,
                author_url=item.get("author_url"),
                author_profile_url=author_profile_url,
                author_logo_url=author_logo_url,
                author_icon_url=author_icon_url,
                homepage_url=item.get("homepage_url"),
                repository_url=item.get("repository_url"),
                license=item.get("license"),
                screenshots=item.get("screenshots"),
                logo_url=item.get("logo_url"),
                icon_url=item.get("icon_url"),
                banner_url=item.get("banner_url"),
                tokens=item.get("tokens"),
                source=item.get("source") or "registry",
                tags=list(item.get("tags") or ["registry"]),
                version=item.get("version"),
                intents=item.get("intents"),
                actions=item.get("actions"),
                frontend_actions=item.get("frontend_actions"),
                frontend_components=item.get("frontend_components"),
                panels=item.get("panels"),
                config_renderers=item.get("config_renderers"),
                pages=item.get("pages"),
                widgets=item.get("widgets"),
                lotus_capabilities=item.get("lotus_capabilities"),
                url=item.get("url"),
                css_url=item.get("css_url"),
            )
        )
    return items


def list_registry_themes() -> List[RegistryItem]:
    items: List[RegistryItem] = []
    for item in _fetch_registry_items("/themes"):
        theme_id = str(item.get("id") or "").strip()
        if not theme_id:
            continue
        items.append(
            RegistryItem(
                id=theme_id,
                name=item.get("name") or theme_id,
                description=item.get("description"),
                author=item.get("author"),
                author_url=item.get("author_url"),
                author_profile_url=item.get("author_profile_url"),
                author_logo_url=item.get("author_logo_url"),
                author_icon_url=item.get("author_icon_url"),
                homepage_url=item.get("homepage_url"),
                repository_url=item.get("repository_url"),
                license=item.get("license"),
                screenshots=item.get("screenshots"),
                logo_url=item.get("logo_url"),
                icon_url=item.get("icon_url"),
                banner_url=item.get("banner_url"),
                tokens=item.get("tokens"),
                source=item.get("source") or "registry",
                tags=list(item.get("tags") or ["registry"]),
                version=item.get("version"),
                intents=item.get("intents"),
                actions=item.get("actions"),
                frontend_actions=item.get("frontend_actions"),
                frontend_components=item.get("frontend_components"),
                panels=item.get("panels"),
                config_renderers=item.get("config_renderers"),
                pages=item.get("pages"),
                widgets=item.get("widgets"),
                lotus_capabilities=item.get("lotus_capabilities"),
                url=item.get("url"),
                css_url=item.get("css_url"),
            )
        )
    return items


def list_registry_plugins_merged() -> List[RegistryItem]:
    local = list_local_plugins_introspected()
    registry = list_registry_plugins()
    merged = {item.id: item for item in registry}
    for item in local:
        merged.setdefault(item.id, item)
    return list(merged.values())


def list_registry_themes_merged() -> List[RegistryItem]:
    local = list_local_themes()
    registry = list_registry_themes()
    merged = {item.id: item for item in registry}
    for item in local:
        merged.setdefault(item.id, item)
    return list(merged.values())


def get_system_check() -> Dict[str, Any]:
    repo_root = _repo_root()
    plugins_dir = _local_plugins_dir()
    themes_dir = _local_themes_dir()
    reported_cwd = os.environ.get("PWD") or str(Path.cwd().resolve())

    return {
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
        "uv_available": shutil.which("uv") is not None,
        "git_available": shutil.which("git") is not None,
        "node_available": shutil.which("node") is not None,
        "cwd": reported_cwd,
        "repo_root": str(repo_root),
        "local_plugins": len(list_local_plugins()),
        "local_themes": len(list_local_themes()),
        "plugins_dir": str(plugins_dir),
        "themes_dir": str(themes_dir),
    }


def get_nelumbo_context() -> Dict[str, Any]:
    project_root = Path(os.environ.get("PWD") or Path.cwd()).resolve()
    if not (project_root / CONFIG_FILENAME).exists():
        return {
            "workspace_detected": False,
            "project_root": None,
            "api_base_url": None,
        }

    config = load_project_config(project_root)
    server_config = config.get(
        "server", {}) if isinstance(config, dict) else {}
    host = server_config.get("host", "127.0.0.1")
    port = server_config.get("port", 8003)
    resolved_host = "127.0.0.1" if host == "0.0.0.0" else str(host)
    api_base_url = f"http://{resolved_host}:{port}/api/v1"

    return {
        "workspace_detected": True,
        "project_root": str(project_root),
        "api_base_url": api_base_url,
    }


def get_embeddr_server_status() -> Dict[str, Any]:
    global _EMBEDDR_SERVER_PROCESS
    if _EMBEDDR_SERVER_PROCESS is None:
        return {"running": False}
    if _EMBEDDR_SERVER_PROCESS.poll() is not None:
        _EMBEDDR_SERVER_PROCESS = None
        _EMBEDDR_SERVER_META.clear()
        return {"running": False}
    return {
        "running": True,
        "pid": _EMBEDDR_SERVER_PROCESS.pid,
        **_EMBEDDR_SERVER_META,
    }


def get_embeddr_server_logs(*, lines: int = 200) -> Dict[str, Any]:
    log_path = _EMBEDDR_SERVER_META.get("log_path")
    if not log_path:
        return {
            "ok": False,
            "log_path": None,
            "content": None,
            "message": "No log file registered",
        }
    path = Path(log_path)
    if not path.exists():
        return {
            "ok": False,
            "log_path": str(path),
            "content": None,
            "message": "Log file not found",
        }
    try:
        content = path.read_text(encoding="utf-8", errors="ignore")
        if lines > 0:
            tail = "\n".join(content.splitlines()[-lines:])
        else:
            tail = content
        return {
            "ok": True,
            "log_path": str(path),
            "content": tail,
            "message": None,
        }
    except Exception as exc:
        return {
            "ok": False,
            "log_path": str(path),
            "content": None,
            "message": str(exc),
        }


def start_embeddr_server(
    *,
    host: str,
    port: int,
    data_dir: Optional[str],
    plugins_dir: Optional[str],
    enable_docs: bool,
    allow_dev_origins: bool,
    confirm: bool,
) -> Dict[str, Any]:
    if not confirm:
        return {"running": False, "message": "Confirmation required"}

    if get_embeddr_server_status().get("running"):
        return {"running": True, "message": "Embeddr server already running"}

    project_root = Path(os.environ.get("PWD") or Path.cwd()).resolve()
    if not (project_root / CONFIG_FILENAME).exists():
        return {"running": False, "message": "No workspace detected"}

    env = os.environ.copy()
    resolved_data_dir = data_dir or str(project_root / ".embeddr")
    resolved_plugins_dir = plugins_dir or str(
        Path(resolved_data_dir) / "plugins")
    env["EMBEDDR_DATA_DIR"] = resolved_data_dir
    env["EMBEDDR_PLUGINS_DIR"] = resolved_plugins_dir

    cmd = [
        sys.executable,
        "-m",
        "embeddr.cli",
        "serve",
        "--host",
        host,
        "--port",
        str(port),
    ]
    if enable_docs:
        cmd.append("--docs")
    if allow_dev_origins:
        cmd.append("--dev-origins")

    logs_dir = (Path(resolved_data_dir).expanduser().resolve()) / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    log_path = logs_dir / "embeddr-server.log"
    log_handle = log_path.open("a", encoding="utf-8")

    process = subprocess.Popen(
        cmd,
        cwd=str(project_root),
        env=env,
        stdout=log_handle,
        stderr=log_handle,
    )
    global _EMBEDDR_SERVER_PROCESS
    _EMBEDDR_SERVER_PROCESS = process
    _EMBEDDR_SERVER_META.clear()
    _EMBEDDR_SERVER_META.update(
        {
            "host": host,
            "port": port,
            "base_url": f"http://{host}:{port}",
            "started_at": int(time.time()),
            "log_path": str(log_path),
        }
    )
    return {"running": True, "pid": process.pid, **_EMBEDDR_SERVER_META}


def stop_embeddr_server(confirm: bool) -> Dict[str, Any]:
    if not confirm:
        return {"running": False, "message": "Confirmation required"}
    status = get_embeddr_server_status()
    if not status.get("running"):
        return {"running": False, "message": "Embeddr server not running"}

    process = _EMBEDDR_SERVER_PROCESS
    if process is None:
        return {"running": False, "message": "Embeddr server not running"}

    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
    _EMBEDDR_SERVER_META.clear()
    return {"running": False}


def _write_json_file(path: Path, payload: Dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _disabled_plugins_path(data_dir: Path) -> Path:
    return data_dir / "disabled_plugins.json"


def _resolve_workspace_paths(project_root: Path) -> Dict[str, Path]:
    config = load_project_config(project_root)
    paths = config.get("paths", {}) if isinstance(config, dict) else {}
    data_dir_value = paths.get("data_dir") or ".embeddr"
    plugins_dir_value = paths.get("plugins_dir") or str(
        Path(data_dir_value) / "plugins"
    )

    data_dir = Path(str(data_dir_value))
    if not data_dir.is_absolute():
        data_dir = project_root / data_dir

    plugins_dir = Path(str(plugins_dir_value))
    if not plugins_dir.is_absolute():
        plugins_dir = project_root / plugins_dir

    return {
        "data_dir": data_dir.expanduser().resolve(),
        "plugins_dir": plugins_dir.expanduser().resolve(),
    }


def _install_workspace_plugins(
    *,
    plugins: List[str],
    plugins_dir: Path,
) -> Dict[str, List[str]]:
    installed: List[str] = []
    missing: List[str] = []
    source_root = _local_plugins_dir()

    for plugin_id in plugins:
        if not plugin_id or plugin_id.startswith("("):
            continue
        source_path = source_root / plugin_id
        target_path = plugins_dir / plugin_id
        if not source_path.exists():
            missing.append(plugin_id)
            continue
        try:
            shutil.copytree(source_path, target_path, dirs_exist_ok=True)
            installed.append(plugin_id)
        except Exception:
            missing.append(plugin_id)

    return {"installed": installed, "missing": missing}


def list_workspace_plugins() -> Dict[str, Any]:
    project_root = Path(os.environ.get("PWD") or Path.cwd()).resolve()
    if not (project_root / CONFIG_FILENAME).exists():
        return {
            "ok": False,
            "message": "No workspace detected",
            "plugins": [],
            "plugins_dir": None,
        }

    paths = _resolve_workspace_paths(project_root)
    plugins_dir = paths["plugins_dir"]
    if not plugins_dir.exists():
        return {
            "ok": True,
            "message": "Plugins directory not found",
            "plugins": [],
            "plugins_dir": str(plugins_dir),
        }

    plugins: List[str] = []
    for entry in sorted(plugins_dir.iterdir()):
        if not entry.is_dir():
            continue
        if (entry / "plugin.py").exists():
            plugins.append(entry.name)

    return {
        "ok": True,
        "message": None,
        "plugins": plugins,
        "plugins_dir": str(plugins_dir),
    }


def list_workspace_disabled_plugins() -> Dict[str, Any]:
    project_root = Path(os.environ.get("PWD") or Path.cwd()).resolve()
    if not (project_root / CONFIG_FILENAME).exists():
        return {
            "ok": False,
            "message": "No workspace detected",
            "disabled_plugins": [],
            "data_dir": None,
        }

    paths = _resolve_workspace_paths(project_root)
    data_dir = paths["data_dir"]
    disabled_path = _disabled_plugins_path(data_dir)
    if not disabled_path.exists():
        return {
            "ok": True,
            "message": None,
            "disabled_plugins": [],
            "data_dir": str(data_dir),
        }

    try:
        payload = json.loads(disabled_path.read_text(encoding="utf-8"))
        items = payload.get("disabled_plugins") if isinstance(
            payload, dict) else None
        disabled = [str(item)
                    for item in items] if isinstance(items, list) else []
        return {
            "ok": True,
            "message": None,
            "disabled_plugins": disabled,
            "data_dir": str(data_dir),
        }
    except Exception as exc:
        return {
            "ok": False,
            "message": str(exc),
            "disabled_plugins": [],
            "data_dir": str(data_dir),
        }


def set_workspace_disabled_plugins_destructive(
    *,
    disabled_plugins: List[str],
    confirm: bool,
) -> Dict[str, Any]:
    # DESTRUCTIVE: Writes disabled plugin list to the workspace data directory.
    if not confirm:
        return {
            "ok": False,
            "message": "Confirmation required",
            "disabled_plugins": [],
            "data_dir": None,
        }

    project_root = Path(os.environ.get("PWD") or Path.cwd()).resolve()
    if not (project_root / CONFIG_FILENAME).exists():
        return {
            "ok": False,
            "message": "No workspace detected",
            "disabled_plugins": [],
            "data_dir": None,
        }

    paths = _resolve_workspace_paths(project_root)
    data_dir = paths["data_dir"]
    data_dir.mkdir(parents=True, exist_ok=True)
    disabled_path = _disabled_plugins_path(data_dir)
    cleaned = sorted({item.strip()
                     for item in disabled_plugins if item.strip()})
    _write_json_file(disabled_path, {"disabled_plugins": cleaned})

    return {
        "ok": True,
        "message": None,
        "disabled_plugins": cleaned,
        "data_dir": str(data_dir),
    }


def _download_registry_plugin_archive(
    *,
    plugin_id: str,
    api_key: Optional[str],
    base_url: str,
    target_dir: Path,
) -> Optional[Path]:
    url = f"{base_url}/plugins/{plugin_id}/package"
    headers: Dict[str, str] = {}
    if api_key:
        headers["X-API-Key"] = api_key
    try:
        with httpx.Client(timeout=30.0) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            target_dir.mkdir(parents=True, exist_ok=True)
            archive_path = target_dir / f"{plugin_id}.zip"
            archive_path.write_bytes(response.content)
            return archive_path
    except Exception:
        return None


def _safe_extract_zip(bundle: zipfile.ZipFile, target_dir: Path) -> None:
    target_root = target_dir.resolve()
    for name in bundle.namelist():
        target_path = (target_dir / name).resolve()
        if not str(target_path).startswith(str(target_root)):
            raise ValueError("Invalid archive path")
    bundle.extractall(target_dir)


def install_registry_plugins_destructive(
    *,
    plugin_ids: List[str],
    api_key: Optional[str],
    reinstall: bool,
    confirm: bool,
) -> Dict[str, Any]:
    # DESTRUCTIVE: Writes plugin files into the workspace plugins directory.
    if not confirm:
        return {
            "ok": False,
            "message": "Confirmation required",
            "installed": [],
            "skipped": [],
            "missing": [],
            "errors": {},
        }

    project_root = Path(os.environ.get("PWD") or Path.cwd()).resolve()
    if not (project_root / CONFIG_FILENAME).exists():
        return {
            "ok": False,
            "message": "No workspace detected",
            "installed": [],
            "skipped": [],
            "missing": [],
            "errors": {},
        }

    base_url = _registry_base_url()
    paths = _resolve_workspace_paths(project_root)
    plugins_dir = paths["plugins_dir"]
    plugins_dir.mkdir(parents=True, exist_ok=True)

    installed: List[str] = []
    skipped: List[str] = []
    missing: List[str] = []
    errors: Dict[str, str] = {}
    temp_dir = Path(tempfile.mkdtemp(prefix="nelumbo-plugin-"))

    try:
        for plugin_id in sorted({pid for pid in plugin_ids if pid}):
            if plugin_id.startswith("("):
                continue

            target_path = plugins_dir / plugin_id
            if target_path.exists():
                if not reinstall:
                    skipped.append(plugin_id)
                    continue
                try:
                    shutil.rmtree(target_path)
                except Exception as exc:
                    errors[plugin_id] = f"Failed to remove existing plugin: {exc}"
                    missing.append(plugin_id)
                    continue

            local_source = _local_plugins_dir() / plugin_id
            if local_source.exists():
                try:
                    shutil.copytree(local_source, target_path,
                                    dirs_exist_ok=True)
                    installed.append(plugin_id)
                    continue
                except Exception as exc:
                    errors[plugin_id] = str(exc)
                    missing.append(plugin_id)
                    continue

            if not base_url:
                missing.append(plugin_id)
                errors[plugin_id] = "Registry URL not configured"
                continue

            archive_path = _download_registry_plugin_archive(
                plugin_id=plugin_id,
                api_key=api_key,
                base_url=base_url,
                target_dir=temp_dir,
            )
            if not archive_path:
                missing.append(plugin_id)
                errors[plugin_id] = "Failed to download plugin archive"
                continue

            try:
                with zipfile.ZipFile(archive_path, "r") as bundle:
                    _safe_extract_zip(bundle, plugins_dir)
                if target_path.exists():
                    installed.append(plugin_id)
                else:
                    missing.append(plugin_id)
                    errors[plugin_id] = "Plugin archive missing expected folder"
            except Exception as exc:
                missing.append(plugin_id)
                errors[plugin_id] = str(exc)
    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception:
            pass

    ok = bool(installed or skipped) and not (missing and not installed)
    message = None
    if missing and not installed:
        message = "No plugins were installed"
    return {
        "ok": ok,
        "message": message,
        "installed": installed,
        "skipped": skipped,
        "missing": missing,
        "errors": errors,
    }


def create_workspace_destructive(
    *,
    name: str,
    target_dir: Path,
    auth_mode: str,
    plugins: List[str],
    themes: List[str],
    operator_name: str,
    admin_username: str,
    user_operator_name: Optional[str] = None,
    user_client_name: Optional[str] = None,
    database_provider: str | None = None,
    database_url: str | None = None,
) -> Dict[str, Any]:
    # DESTRUCTIVE: Creates directories and writes config files.
    target_dir = target_dir.expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)

    create_default_config(
        target_dir,
        name=name,
        auth_mode=auth_mode,
        data_dir=".embeddr",
        plugins_dir=".embeddr/plugins",
        database_provider=database_provider or "sqlite",
        database_url=database_url,
    )

    data_dir = (target_dir / ".embeddr").resolve()
    plugins_dir = (data_dir / "plugins").resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    plugins_dir.mkdir(parents=True, exist_ok=True)

    plugin_install = _install_workspace_plugins(
        plugins=plugins,
        plugins_dir=plugins_dir,
    )

    plan_path = target_dir / "nelumbo.plan.json"
    plan_payload = {
        "workspace": {
            "name": name,
            "path": str(target_dir),
            "config_path": str(target_dir / CONFIG_FILENAME),
            "data_dir": str(data_dir),
            "plugins_dir": str(plugins_dir),
        },
        "auth_mode": auth_mode,
        "database": {
            "provider": database_provider or "sqlite",
            "url": database_url,
        },
        "operator": {
            "root_name": operator_name,
            "root_client": admin_username,
            "user_name": user_operator_name,
            "user_client": user_client_name,
        },
        "plugins": plugins,
        "plugins_installed": plugin_install["installed"],
        "plugins_missing": plugin_install["missing"],
        "themes": themes,
    }
    _write_json_file(plan_path, plan_payload)

    return {
        "workspace_path": str(target_dir),
        "config_path": str(target_dir / CONFIG_FILENAME),
        "plan_path": str(plan_path),
        "data_dir": str(data_dir),
        "plugins_dir": str(plugins_dir),
        "installed_plugins": plugin_install["installed"],
        "missing_plugins": plugin_install["missing"],
    }


def test_database_connection(
    *,
    database_provider: str,
    database_url: Optional[str],
    workspace_path: Optional[str],
    confirm: bool,
) -> Dict[str, Any]:
    provider = (database_provider or "sqlite").lower().strip()
    workspace_dir = Path(workspace_path).expanduser(
    ).resolve() if workspace_path else None
    data_dir = workspace_dir / ".embeddr" if workspace_dir else None

    resolved_url = database_url
    sqlite_path: Optional[Path] = None

    if provider == "sqlite" and not resolved_url:
        if not data_dir:
            return {
                "ok": False,
                "message": "Workspace path required for sqlite default",
                "resolved_url": None,
                "sqlite_path": None,
            }
        adapter = get_adapter("sqlite:///:memory:", provider="sqlite")
        resolved_url = adapter.get_default_url(data_dir)

    if not resolved_url:
        return {
            "ok": False,
            "message": "Database URL required",
            "resolved_url": None,
            "sqlite_path": None,
        }

    if provider == "sqlite":
        try:
            url_obj = make_url(resolved_url)
            if url_obj.database:
                sqlite_path = Path(url_obj.database)
        except Exception:
            sqlite_path = None

        if sqlite_path and not sqlite_path.exists():
            if not confirm:
                return {
                    "ok": False,
                    "message": "Confirmation required to create sqlite database file",
                    "resolved_url": resolved_url,
                    "sqlite_path": str(sqlite_path),
                }
            if sqlite_path.parent:
                sqlite_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        adapter = get_adapter(resolved_url, provider=provider)
        engine = adapter.create_engine(resolved_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return {
            "ok": True,
            "message": "Connection succeeded",
            "resolved_url": resolved_url,
            "sqlite_path": str(sqlite_path) if sqlite_path else None,
        }
    except SQLAlchemyError as exc:
        return {
            "ok": False,
            "message": str(exc),
            "resolved_url": resolved_url,
            "sqlite_path": str(sqlite_path) if sqlite_path else None,
        }
