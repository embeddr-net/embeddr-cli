import tomllib
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


def create_default_config(
    root_path: Path,
    name: str = "my-embeddr-project",
    database_provider: str = "sqlite",
    database_url: str | None = None,
    data_dir: str | None = None,
    plugins_dir: str | None = None,
    auth_mode: str | None = None,
):
    """
    Create a default embeddr.toml file.
    """
    provider = database_provider or "sqlite"
    provider_line = f"provider = \"{provider}\""
    if database_url:
        url_line = f"url = \"{database_url}\""
    else:
        url_line = "# url = \"\"  # Optional: full SQLAlchemy URL"

    if data_dir:
        data_dir_line = f"data_dir = \"{data_dir}\""
    else:
        data_dir_line = "# data_dir = \".embeddr\"  # Uncomment to store data locally in the project"

    if plugins_dir:
        plugins_dir_line = f"plugins_dir = \"{plugins_dir}\""
    else:
        plugins_dir_line = "# plugins_dir = \".embeddr/plugins\""

    if auth_mode:
        auth_mode_line = f"mode = \"{auth_mode}\""
    else:
        auth_mode_line = "# mode = \"open\"  # open | single | multi | db"

    config_content = f"""[project]
name = "{name}"
description = "Embeddr project configuration"

[server]
host = "127.0.0.1"
port = 8003

[cors]
# origins = ["http://localhost:3000"]
# allow_dev_origins = true

[database]
# provider = "sqlite" | "postgresql" | "mysql"
{provider_line}
{url_line}

[paths]
{data_dir_line}
{plugins_dir_line}

[auth]
{auth_mode_line}

[content]
# proxy_enabled = true
# proxy_allowlist = ["*"]  # or specific domains: [".amazonaws.com", ".nynxz.com"]
"""
    with open(root_path / CONFIG_FILENAME, "w") as f:
        f.write(config_content)
