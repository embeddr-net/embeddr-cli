import os
from pathlib import Path
from typing import List

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter()


class PluginFile(BaseModel):
    filename: str
    url: str


@router.get("", response_model=List[PluginFile])
async def list_plugins():
    """List all available plugins in the plugins directory."""
    # We need to get the plugins directory.
    # Since we don't have global config easily accessible here without circular imports,
    # we'll rely on the environment variable set in serve.py or default.

    plugins_dir = os.environ.get("EMBEDDR_PLUGINS_DIR")
    if not plugins_dir:
        # Fallback to default location relative to CWD if not set
        plugins_dir = str(Path.cwd() / "plugins")

    path = Path(plugins_dir)
    if not path.exists():
        return []

    plugins = []
    # Look for index.js in immediate subdirectories
    for plugin_dir in path.iterdir():
        if plugin_dir.is_dir():
            index_file = plugin_dir / "index.js"
            if index_file.exists():
                plugins.append(
                    PluginFile(
                        filename=plugin_dir.name,
                        url=f"/plugins/{plugin_dir.name}/index.js",
                    )
                )

    return plugins
