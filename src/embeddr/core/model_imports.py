from __future__ import annotations

import importlib
import re
from typing import Optional, Type

from pydantic import BaseModel


_IDENT_RE = re.compile(r"[^0-9a-zA-Z_]+")


def _safe_plugin_module(plugin_name: str) -> str:
    return plugin_name.replace("-", "_").replace(".", "_")


def _import_model(model_path: str) -> Type[BaseModel]:
    """
    Import 'pkg.module:ClassName' into a Pydantic model class.
    """
    if ":" not in model_path:
        raise ValueError(
            f"Invalid model path (expected module:Class): {model_path}")
    mod_name, cls_name = model_path.split(":", 1)
    mod = importlib.import_module(mod_name)
    cls = getattr(mod, cls_name, None)
    if cls is None:
        raise ValueError(f"Model class not found: {model_path}")
    if not issubclass(cls, BaseModel):
        raise ValueError(f"Model is not a pydantic BaseModel: {model_path}")
    return cls


def import_model_with_fallbacks(
    model_path: str,
    *,
    plugin_name: Optional[str] = None,
    plugin_module: Optional[str] = None,
) -> Optional[Type[BaseModel]]:
    """
    Try:
      1) model_path as provided
      2) if plugin_module provided: {plugin_module}.models:Class
      3) if plugin_name provided: rewrite embeddr_plugins.<X> -> embeddr_plugins.<safe>
    """
    try:
        return _import_model(model_path)
    except Exception:
        if plugin_module and ":" in model_path:
            _, cls_name = model_path.split(":", 1)
            try:
                return _import_model(f"{plugin_module}.models:{cls_name}")
            except Exception:
                try:
                    return _import_model(f"{plugin_module}.plugin:{cls_name}")
                except Exception:
                    pass

        if plugin_name and ":" in model_path:
            safe = _safe_plugin_module(plugin_name)
            before, cls_name = model_path.split(":", 1)

            try_paths = []
            if before.startswith("embeddr_plugins."):
                parts = before.split(".")
                if len(parts) >= 2:
                    parts[1] = safe
                    try_paths.append(".".join(parts) + ":" + cls_name)

            try_paths.append(f"embeddr_plugins.{safe}:{cls_name}")
            try_paths.append(f"embeddr_plugins.{safe}.models:{cls_name}")
            try_paths.append(f"embeddr_plugins.{safe}.plugin:{cls_name}")

            for path in try_paths:
                try:
                    return _import_model(path)
                except Exception:
                    continue

    return None
