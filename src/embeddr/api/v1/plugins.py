from fastapi import APIRouter, HTTPException, Body, Depends
from fastapi.responses import FileResponse
from embeddr.core.plugin_loader import (
    get_loaded_plugins,
    get_plugin_instance,
    get_all_plugin_instances,
)
from typing import Dict, Any, Optional
import os
from pathlib import Path
from embeddr.api.security import require_permission

router = APIRouter()


_LOGO_CANDIDATES = [
    "logo.svg",
    "logo.png",
    "logo.webp",
    "logo.avif",
    "logo.jpg",
    "logo.jpeg",
    "logo.ico",
    "icon.svg",
    "icon.png",
    "icon.webp",
    "icon.avif",
    "icon.jpg",
    "icon.jpeg",
    "icon.ico",
]

_LOGO_EXTENSIONS = {".svg", ".png", ".webp", ".avif", ".jpg", ".jpeg", ".ico"}


def _resolve_logo_url(plugin_name: str, source_path: Optional[Path]) -> Optional[str]:
    if not source_path or not source_path.exists():
        return None

    candidate_dirs = [
        (source_path / "assets", "assets"),
        (source_path / "dist" / "assets", "assets"),
    ]

    for assets_dir, url_prefix in candidate_dirs:
        if not assets_dir.exists():
            continue
        for filename in _LOGO_CANDIDATES:
            candidate = assets_dir / filename
            if candidate.exists() and candidate.is_file():
                return f"/plugins/{plugin_name}/{url_prefix}/{filename}"
        fallback = sorted(
            [
                path
                for path in assets_dir.iterdir()
                if path.is_file() and path.suffix.lower() in _LOGO_EXTENSIONS
            ],
            key=lambda path: path.name.lower(),
        )
        if fallback:
            filename = fallback[0].name
            return f"/plugins/{plugin_name}/{url_prefix}/{filename}"
    return None


def _find_repo_root() -> Optional[Path]:
    current = Path(__file__).resolve()
    for parent in current.parents:
        if parent.name == "embeddr-cli":
            return parent.parent
    return None


def _find_plugin_source_path(plugin_name: str) -> Optional[Path]:
    instance = get_plugin_instance(plugin_name)
    if instance:
        source_path = getattr(instance, "_source_path", None)
        if isinstance(source_path, str):
            source_path = Path(source_path)
        if isinstance(source_path, Path) and source_path.exists():
            return source_path

    env_dir = os.environ.get("EMBEDDR_PLUGINS_DIR") or os.environ.get(
        "EMBEDDR_PLUGIN_DIR"
    )
    if env_dir:
        candidate = Path(env_dir) / plugin_name
        if candidate.exists():
            return candidate

    repo_root = _find_repo_root()
    if repo_root:
        dist_candidate = repo_root / "embeddr-plugins" / "dist" / plugin_name
        if dist_candidate.exists():
            return dist_candidate
        plugins_root = repo_root / "embeddr-plugins" / "plugins"
        if plugins_root.exists():
            for plugin_file in plugins_root.rglob(f"{plugin_name}/plugin.py"):
                return plugin_file.parent

    return None


@router.get("")
def list_plugins(auth=Depends(require_permission("plugins:read"))):
    return get_loaded_plugins()


@router.get("/logos")
def list_plugin_logos(auth=Depends(require_permission("plugins:read"))):
    logos: Dict[str, Optional[str]] = {}
    for plugin in get_all_plugin_instances():
        source_path = getattr(plugin, "_source_path", None)
        if isinstance(source_path, str):
            source_path = Path(source_path)
        logo_url = _resolve_logo_url(plugin.name, source_path)
        logos[plugin.name] = logo_url
    return {"logos": logos}


@router.get("/docks")
def list_plugin_docks(auth=Depends(require_permission("plugins:read"))):
    docks: list[dict] = []
    for plugin in get_all_plugin_instances():
        for dock in getattr(plugin, "docks", []) or []:
            payload = dock.model_dump()
            payload["plugin_id"] = plugin.name
            payload["plugin_name"] = plugin.name
            docks.append(payload)
    return {"items": docks}


@router.get("/{plugin_id}/static/{path:path}")
def get_plugin_static(
    plugin_id: str,
    path: str,
):
    source_path = _find_plugin_source_path(plugin_id)
    if not source_path or not source_path.exists():
        raise HTTPException(status_code=404, detail="Plugin not found")

    candidate = (source_path / path).resolve()
    if source_path not in candidate.parents and candidate != source_path:
        raise HTTPException(status_code=404, detail="Not Found")
    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Not Found")
    return FileResponse(str(candidate))


@router.get("/{plugin_id}/assets/{path:path}")
def get_plugin_asset(
    plugin_id: str,
    path: str,
):
    """Serve plugin assets (logos, icons, images).

    Mounted at /api/plugins/{id}/assets/ and /api/v1/plugins/{id}/assets/.
    Also available at /plugins/{id}/assets/ via StaticFiles mount.
    """
    source_path = _find_plugin_source_path(plugin_id)
    if not source_path or not source_path.exists():
        raise HTTPException(status_code=404, detail="Plugin not found")

    # Check assets dir in source root, then dist/assets
    for assets_dir in [source_path / "assets", source_path / "dist" / "assets"]:
        candidate = (assets_dir / path).resolve()
        if (
            assets_dir.exists()
            and assets_dir in candidate.parents
            and candidate.exists()
            and candidate.is_file()
        ):
            return FileResponse(str(candidate))

    raise HTTPException(status_code=404, detail="Not Found")


@router.get("/{plugin_id}/config")
def get_plugin_config(
    plugin_id: str,
    auth=Depends(require_permission("plugins:read")),
):
    instance = get_plugin_instance(plugin_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Plugin not found")
    return {
        "config": instance.get_config(),
        "schema": instance.get_config_schema()
    }


@router.post("/{plugin_id}/config")
def update_plugin_config(
    plugin_id: str,
    config: Dict[str, Any] = Body(...),
    auth=Depends(require_permission("plugins:write")),
):
    instance = get_plugin_instance(plugin_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Plugin not found")
    try:
        instance.update_config(config)
        return {"status": "ok", "config": instance.get_config()}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{plugin_id}/execute/{action_name}")
def execute_plugin_action(
    plugin_id: str,
    action_name: str,
    payload: Dict[str, Any] = Body(...),
    auth=Depends(require_permission("plugins:write")),
):
    """
    Directly execute a plugin action via API.
    Does NOT require CLI registration, allows complex inputs.
    """
    instance = get_plugin_instance(plugin_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Plugin not found")

    # Verify action exists — check both the legacy PluginAction list
    # AND the @action decorator registry (ActionPlugin).
    # If neither knows the action, still attempt execution since many
    # plugins route actions inside their execute() method directly.
    found = False
    if instance.actions:
        for act in instance.actions:
            if act.name == action_name:
                found = True
                break

    # ActionPlugin stores @action-decorated methods in an internal registry
    # that isn't exposed via the .actions property.
    if not found:
        handler = getattr(instance, "_get_action_handler", None)
        if callable(handler) and handler(action_name) is not None:
            found = True

    # Allow execution even if not in registry — the plugin's execute()
    # method may handle routing internally and will raise NotImplementedError
    # or return an error if the action is truly unsupported.

    try:
        # Use simple execution ID
        from uuid import uuid4
        exec_id = str(uuid4())

        # Execute
        # Note: This is synchronous. Long running tasks should ideally be backgrounded.
        # But for 'reanalyze_collection' which just queues stuff, it's fast.
        result = instance.execute(action_name, exec_id, payload)
        return {"status": "success", "result": result}
    except NotImplementedError:
        raise HTTPException(status_code=501, detail="Action not implemented")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
