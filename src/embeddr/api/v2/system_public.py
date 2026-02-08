from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends
from sqlmodel import Session

from embeddr.db.session import get_session
from embeddr.core.config import settings
from embeddr_core.services.config_service import resolve_plugin_config

router = APIRouter()


def _load_instance_profile(session: Session) -> Dict[str, Any]:
    fallback = {
        "name": getattr(settings, "PROJECT_NAME", None) or "Embeddr",
        "logo_url": None,
        "description": None,
    }
    try:
        value = resolve_plugin_config(
            session=session,
            plugin_name="embeddr-core",
            scope="global",
            scope_id=None,
            config_id="embeddr-core.instance.profile",
        )
        if isinstance(value, dict):
            return {**fallback, **value}
    except Exception:
        pass
    return fallback


@router.get("/system/public")
def get_public_system_info(session: Session = Depends(get_session)) -> Dict[str, Any]:
    instance_profile = _load_instance_profile(session)
    return {
        "instance": instance_profile,
        "dev_mode": bool(getattr(settings, "DEV_MODE", False)),
    }
