import importlib.util
import logging
import sys
from pathlib import Path

from fastapi import FastAPI, APIRouter

logger = logging.getLogger(__name__)


def load_python_plugins(plugins_dir: Path, app: FastAPI) -> None:
    """
    Scans the plugins directory for subdirectories containing a 'plugin.py' file.
    If found, it attempts to load the module and call a 'register(app)' function if it exists.
    """
    if not plugins_dir.exists():
        return

    logger.info(f"Scanning for Python plugins in {plugins_dir}...")

    for plugin_dir in plugins_dir.iterdir():
        if not plugin_dir.is_dir():
            continue

        plugin_file = plugin_dir / "plugin.py"
        if not plugin_file.exists():
            continue

        plugin_name = plugin_dir.name
        logger.info(f"Found Python plugin: {plugin_name}")

        try:
            # Load the module
            spec = importlib.util.spec_from_file_location(
                f"embeddr_plugins.{plugin_name}", plugin_file
            )
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                sys.modules[f"embeddr_plugins.{plugin_name}"] = module
                spec.loader.exec_module(module)

                # Check for register function
                if hasattr(module, "register"):
                    logger.info(f"Registering plugin: {plugin_name}")

                    # Create a router for this plugin with the enforced prefix
                    router = APIRouter(
                        prefix=f"/api/v1/plugins/{plugin_name}", tags=[plugin_name]
                    )

                    # Call register with the router
                    module.register(router)

                    # Include the router in the main app
                    app.include_router(router)
                else:
                    logger.warning(
                        f"Plugin {plugin_name} has no 'register(app)' function."
                    )
        except Exception as e:
            logger.error(f"Failed to load plugin {plugin_name}: {e}")
