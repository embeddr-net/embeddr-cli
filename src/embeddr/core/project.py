import tomllib
import os
from pathlib import Path
from typing import Any, Dict, Optional

CONFIG_FILENAME = "embeddr.toml"


def find_project_root(start_path: Path = Path.cwd()) -> Optional[Path]:
    """
    Find the project root by looking for embeddr.toml in current and parent directories.
    """
    current = start_path.resolve()
    for parent in [current, *current.parents]:
        if (parent / CONFIG_FILENAME).exists():
            return parent
    return None


def load_project_config(root_path: Path) -> Dict[str, Any]:
    """
    Load configuration from embeddr.toml in the given root path.
    """
    config_path = root_path / CONFIG_FILENAME
    if not config_path.exists():
        return {}

    try:
        with open(config_path, "rb") as f:
            return tomllib.load(f)
    except Exception as e:
        print(f"Warning: Failed to load {CONFIG_FILENAME}: {e}")
        return {}


def create_default_config(root_path: Path, name: str = "my-embeddr-project"):
    """
    Create a default embeddr.toml file.
    """
    config_content = f"""[project]
name = "{name}"
description = "Embeddr project configuration"

[server]
host = "127.0.0.1"
port = 8003

[processing]
batch_size = 32
default_model = "openai/clip-vit-base-patch32"

[paths]
# data_dir = ".embeddr"  # Uncomment to store data locally in the project
"""
    with open(root_path / CONFIG_FILENAME, "w") as f:
        f.write(config_content)
