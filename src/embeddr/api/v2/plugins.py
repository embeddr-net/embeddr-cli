from fastapi import APIRouter, HTTPException, Body
from embeddr.core.plugin_loader import (
    get_loaded_plugins,
    get_plugin_instance,
    get_all_plugin_instances,
)
from typing import Dict, Any, Optional
from pathlib import Path

router = APIRouter()


_LOGO_CANDIDATES = [
    "logo.svg",
    "logo.png",
    "logo.webp",
    "logo.jpg",
    "logo.jpeg",
    "logo.ico",
]


def _resolve_logo_url(plugin_name: str, source_path: Optional[Path]) -> Optional[str]:
    if not source_path or not source_path.exists():
        return None
    assets_dir = source_path / "assets"
    if not assets_dir.exists():
        return None
    for filename in _LOGO_CANDIDATES:
        candidate = assets_dir / filename
        if candidate.exists() and candidate.is_file():
            return f"/api/v2/plugins/{plugin_name}/static/assets/{filename}"
    return None


@router.get("")
def list_plugins():
    return get_loaded_plugins()


@router.get("/logos")
def list_plugin_logos():
    logos: Dict[str, Optional[str]] = {}
    for plugin in get_all_plugin_instances():
        source_path = getattr(plugin, "_source_path", None)
        if isinstance(source_path, str):
            source_path = Path(source_path)
        logo_url = _resolve_logo_url(plugin.name, source_path)
        logos[plugin.name] = logo_url
    return {"logos": logos}


@router.get("/{plugin_id}/config")
def get_plugin_config(plugin_id: str):
    instance = get_plugin_instance(plugin_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Plugin not found")
    return {
        "config": instance.get_config(),
        "schema": instance.get_config_schema()
    }


@router.post("/{plugin_id}/config")
def update_plugin_config(plugin_id: str, config: Dict[str, Any] = Body(...)):
    instance = get_plugin_instance(plugin_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Plugin not found")
    try:
        instance.update_config(config)
        return {"status": "ok", "config": instance.get_config()}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/{plugin_id}/execute/{action_name}")
def execute_plugin_action(plugin_id: str, action_name: str, payload: Dict[str, Any] = Body(...)):
    """
    Directly execute a plugin action via API.
    Does NOT require CLI registration, allows complex inputs.
    """
    instance = get_plugin_instance(plugin_id)
    if not instance:
        raise HTTPException(status_code=404, detail="Plugin not found")

    # Verify action exists
    found = False
    if instance.actions:
        for act in instance.actions:
            if act.name == action_name:
                found = True
                break

    if not found:
        raise HTTPException(
            status_code=404, detail=f"Action '{action_name}' not found on plugin '{plugin_id}'")

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
