from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterable

from embeddr.core.config import get_data_dir


PLUGIN_CATEGORY_FOLDERS: set[str] = {
    "core",
    "editors",
    "examples",
    "experimental",
    "features",
    "integrations",
    "development",
    "pages",
    "private",
    "plugins",
    "private_plugins",
    "services",
    "storage",
    "tools",
    "transports",
    "types",
    "widgets",
    "effects",
}


def load_disabled_plugins() -> set[str]:
    disabled: set[str] = set()
    raw = os.environ.get("EMBEDDR_DISABLED_PLUGINS", "").strip()
    if raw:
        disabled.update({item.strip()
                        for item in raw.split(",") if item.strip()})

    data_root = Path(os.environ.get("EMBEDDR_DATA_DIR") or get_data_dir())
    disabled_path = data_root / "disabled_plugins.json"
    if not disabled_path.exists():
        return disabled

    try:
        payload = json.loads(disabled_path.read_text(encoding="utf-8"))
    except Exception:
        return disabled

    items = payload.get("disabled_plugins") if isinstance(
        payload, dict) else None
    if isinstance(items, list):
        disabled.update({str(item).strip()
                        for item in items if str(item).strip()})
    return disabled


def iter_plugin_dirs(plugins_dir: Path) -> Iterable[Path]:
    if not plugins_dir.exists() or not plugins_dir.is_dir():
        return

    for item in plugins_dir.iterdir():
        if not item.is_dir():
            continue

        if item.name in PLUGIN_CATEGORY_FOLDERS:
            for sub in item.iterdir():
                if sub.is_dir():
                    yield sub
            continue

        yield item
