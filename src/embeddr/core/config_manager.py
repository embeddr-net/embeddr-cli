import tomllib
from typing import Any, Dict

from pydantic import BaseModel

from embeddr.core.config import settings


class GeneralConfig(BaseModel):
    model: str = "openai/clip-vit-base-patch32"
    batch_size: int = 32
    theme: str = "system"


class ComfyConfig(BaseModel):
    url: str = "http://127.0.0.1:8188"
    enabled: bool = False


class UploadConfig(BaseModel):
    default_library_id: int | None = None
    default_collection_id: int | None = None
    default_tags: str = ""


class AppConfig(BaseModel):
    general: GeneralConfig = GeneralConfig()
    comfy: ComfyConfig = ComfyConfig()
    upload: UploadConfig = UploadConfig()


class ConfigManager:
    def __init__(self):
        self.config_path = settings.DATA_DIR / "config.toml"
        self._config: AppConfig = AppConfig()
        # Ensure data dir exists
        settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load()

    def load(self):
        if not self.config_path.exists():
            self.save()
            return

        try:
            with open(self.config_path, "rb") as f:
                data = tomllib.load(f)
            self._config = AppConfig(**data)
        except Exception as e:
            print(f"Error loading config: {e}")
            # Fallback to default
            self._config = AppConfig()

    def save(self):
        # Simple TOML writer
        lines = []

        # General
        lines.append("[general]")
        lines.append(f'model = "{self._config.general.model}"')
        lines.append(f"batch_size = {self._config.general.batch_size}")
        lines.append(f'theme = "{self._config.general.theme}"')
        lines.append("")

        # Comfy
        lines.append("[comfy]")
        lines.append(f'url = "{self._config.comfy.url}"')
        lines.append(f"enabled = {str(self._config.comfy.enabled).lower()}")
        lines.append("")

        # Upload
        lines.append("[upload]")
        if self._config.upload.default_library_id is not None:
            lines.append(
                f"default_library_id = {self._config.upload.default_library_id}"
            )
        if self._config.upload.default_collection_id is not None:
            lines.append(
                f"default_collection_id = {self._config.upload.default_collection_id}"
            )
        lines.append(f'default_tags = "{self._config.upload.default_tags}"')
        lines.append("")

        with open(self.config_path, "w") as f:
            f.write("\n".join(lines))

    @property
    def config(self) -> AppConfig:
        return self._config

    def update(self, new_config: Dict[str, Any]):
        # Get current state as dict
        current_data = self._config.model_dump()

        # Merge new_config into current_data
        # We expect new_config to be partial, e.g. {"general": {"batch_size": 64}}
        for section, values in new_config.items():
            if section in current_data and isinstance(values, dict):
                current_data[section].update(values)
            else:
                current_data[section] = values

        # Validate and update
        self._config = AppConfig(**current_data)
        self.save()


config_manager = ConfigManager()
