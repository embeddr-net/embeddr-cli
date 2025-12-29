from functools import lru_cache
import os
from pydantic_settings import BaseSettings
from pydantic import Field


from pathlib import Path


def get_data_dir():
    # Default to ~/.local/share/embeddr on Linux, or ~/embeddr as a fallback
    default_path = Path.home() / ".local" / "share" / "embeddr"
    if (
        not default_path.parent.exists()
    ):  # If .local/share doesn't exist (non-linux or weird setup)
        default_path = Path.home() / "embeddr"

    return os.environ.get("EMBEDDR_DATA_DIR", str(default_path))


def get_db_url():
    data_dir = get_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    return f"sqlite:///{os.path.join(data_dir, 'embeddr.db')}"


def get_thumbnails_dir():
    data_dir = get_data_dir()
    thumbnails_dir = os.path.join(data_dir, "thumbnails")
    os.makedirs(thumbnails_dir, exist_ok=True)
    return thumbnails_dir


def get_vector_storage_dir():
    data_dir = get_data_dir()
    vector_dir = os.path.join(data_dir, "vector_storage")
    os.makedirs(vector_dir, exist_ok=True)
    return vector_dir


class Settings(BaseSettings):
    DEV_MODE: bool = True
    PROJECT_NAME: str = "Embeddr Local API"
    DATA_DIR: str = Field(default_factory=get_data_dir)
    DATABASE_URL: str = Field(default_factory=get_db_url)
    THUMBNAILS_DIR: str = Field(default_factory=get_thumbnails_dir)
    VECTOR_STORAGE_DIR: str = Field(default_factory=get_vector_storage_dir)
    API_V1_STR: str = "/api/v1"

    class Config:
        case_sensitive = True
        env_file = ".env"
        extra = "ignore"


@lru_cache()
def get_settings():
    s = Settings()
    # Set environment variables for embeddr-core
    os.environ["EMBEDDR_VECTOR_STORAGE_DIR"] = s.VECTOR_STORAGE_DIR
    return s


def refresh_settings():
    get_settings.cache_clear()


class SettingsProxy:
    def __getattr__(self, name):
        return getattr(get_settings(), name)


settings = SettingsProxy()
