from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, Depends, Request
from sqlmodel import Session

from embeddr.db.session import get_session
from embeddr.core.config import is_dev_mode
from embeddr_core.services.config_service import resolve_plugin_config

router = APIRouter()

# Separate router for well-known endpoints mounted at app root
wellknown_router = APIRouter()


def _load_instance_profile(session: Session) -> Dict[str, Any]:
    fallback = {
        "name": "Embeddr",
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
        "dev_mode": is_dev_mode(),
    }


@wellknown_router.get("/.well-known/embeddr-auth")
def auth_discovery(request: Request) -> Dict[str, Any]:
    """
    OAuth2-style discovery endpoint for Embeddr auth.

    External apps can fetch this to discover auth endpoints, supported scopes,
    and grant types without hardcoding URLs.
    """
    from embeddr.auth.permissions import Permissions

    base = str(request.base_url).rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/api/v1/security/auth/authorize",
        "token_endpoint": f"{base}/api/v1/security/auth/token",
        "consent_endpoint": f"{base}/api/v1/security/auth/authorize/consent",
        "service_client_registration_endpoint": f"{base}/api/v1/security/service-clients",
        "scopes_supported": Permissions.all_permissions(),
        "grant_types_supported": ["client_credentials", "authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["client_secret_post"],
        "response_types_supported": ["code"],
    }
