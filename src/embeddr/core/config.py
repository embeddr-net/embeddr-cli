import os
import platform
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


def get_data_dir() -> Path:
    """
    Return the data directory path.

    Priority: EMBEDDR_DATA_DIR env var > sensible OS-specific default.
    Linux: ~/.local/share/embeddr
    macOS: ~/Library/Application Support/embeddr
    Windows: %LOCALAPPDATA%/embeddr or %APPDATA%/embeddr
    Fallback: ~/embeddr
    """
    if env := os.environ.get("EMBEDDR_DATA_DIR"):
        return Path(env)

    home = Path.home()
    system = platform.system()

    if system == "Linux":
        xdg = os.environ.get("XDG_DATA_HOME")
        return Path(xdg) / "embeddr" if xdg else home / ".local" / "share" / "embeddr"

    if system == "Darwin":
        return home / "Library" / "Application Support" / "embeddr"
    if system == "Windows":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA")
        return Path(base) / "embeddr" if base else home / "embeddr"

    return home / "embeddr"


def get_db_url():
    data_dir = get_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    return f"sqlite:///{os.path.join(data_dir, 'embeddr.db')}"


def get_vector_storage_dir() -> Path:
    data_dir = get_data_dir()
    vector_dir = os.path.join(data_dir, "vector_storage")
    os.makedirs(vector_dir, exist_ok=True)
    return Path(vector_dir)


def is_dev_mode() -> bool:
    """Check if the server is running in development mode.

    Set EMBEDDR_DEV=1 (or true/yes) in the environment to enable.
    Dev mode relaxes CORS, exposes extra diagnostics, etc.
    """
    return os.environ.get("EMBEDDR_DEV", "").lower() in ("1", "true", "yes")


class Settings(BaseSettings):
    DATA_DIR: Path = Field(default_factory=get_data_dir)
    DATABASE_URL: str = Field(default_factory=get_db_url)
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    DB_POOL_TIMEOUT: int = 60
    VECTOR_STORAGE_DIR: Path = Field(default_factory=get_vector_storage_dir)

    class Config:
        case_sensitive = True
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings() -> Settings:
    s = Settings()
    # Set environment variables for embeddr-core
    os.environ["EMBEDDR_VECTOR_STORAGE_DIR"] = str(s.VECTOR_STORAGE_DIR)
    return s


def refresh_settings():
    get_settings.cache_clear()


class SettingsProxy:
    def __getattr__(self, name):
        return getattr(get_settings(), name)


settings = SettingsProxy()
