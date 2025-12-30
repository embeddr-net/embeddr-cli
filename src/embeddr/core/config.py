from functools import lru_cache
import os
import platform
from pydantic_settings import BaseSettings
from pydantic import Field


from pathlib import Path


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


def get_thumbnails_dir() -> Path:
    data_dir = get_data_dir()
    thumbnails_dir = os.path.join(data_dir, "thumbnails")
    os.makedirs(thumbnails_dir, exist_ok=True)
    return Path(thumbnails_dir)


def get_vector_storage_dir() -> Path:
    data_dir = get_data_dir()
    vector_dir = os.path.join(data_dir, "vector_storage")
    os.makedirs(vector_dir, exist_ok=True)
    return Path(vector_dir)


class Settings(BaseSettings):
    DEV_MODE: bool = True
    PROJECT_NAME: str = "Embeddr Local API"
    DATA_DIR: Path = Field(default_factory=get_data_dir)
    DATABASE_URL: str = Field(default_factory=get_db_url)
    THUMBNAILS_DIR: Path = Field(default_factory=get_thumbnails_dir)
    VECTOR_STORAGE_DIR: Path = Field(default_factory=get_vector_storage_dir)
    API_V1_STR: str = "/api/v1"

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
