from typing import Optional
import os
import logging
from fastapi import Security, HTTPException, Request, Depends
from fastapi.security import APIKeyHeader, APIKeyQuery
from starlette.status import HTTP_403_FORBIDDEN
from sqlmodel import Session

from embeddr.db.session import get_engine
from embeddr.services import auth_service

API_KEY_NAME = "X-API-Key"
COOKIE_NAME = "embeddr_auth"

# Rename schemes to avoid shadowing in dependency injection
api_key_header_scheme = APIKeyHeader(name=API_KEY_NAME, auto_error=False)
api_key_query_scheme = APIKeyQuery(name="api_key", auto_error=False)

logger = logging.getLogger(__name__)


def _extract_raw_key(
    api_key_header_val: str,
    api_key_query_val: str,
    request: Optional[Request],
) -> Optional[str]:
    if api_key_header_val:
        return api_key_header_val
    if api_key_query_val:
        return api_key_query_val
    cookie_key = request.cookies.get(COOKIE_NAME) if request else None
    return cookie_key


async def get_api_key(
    request: Request,
    api_key_header_val: str = Security(api_key_header_scheme),
    api_key_query_val: str = Security(api_key_query_scheme),
):
    """
    Validate an auth credential from header, query param, or cookie.
    CHECKPOINTS:
    1. Checks X-API-Key header
    2. Checks api_key query param
    3. Checks embeddr_auth cookie
    """
    raw_key = _extract_raw_key(api_key_header_val, api_key_query_val, request)
    mode = auth_service.get_auth_mode()
    # Open mode
    if mode == "open":
        return None

    if not raw_key:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Missing credentials"
        )

    # DB-backed keys/sessions
    with Session(get_engine()) as session:
        ctx = auth_service.resolve_auth_context(session, raw_key)
        if not ctx:
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN, detail="Could not validate credentials"
            )
        return raw_key


def check_auth_enabled() -> bool:
    return auth_service.is_auth_enabled()


async def get_auth_context(
    request: Request,
    api_key_header_val: str = Security(api_key_header_scheme),
    api_key_query_val: str = Security(api_key_query_scheme),
):
    raw_key = _extract_raw_key(api_key_header_val, api_key_query_val, request)
    mode = auth_service.get_auth_mode()

    # Log auth source for diagnostics
    _source = "header" if api_key_header_val else ("query" if (request and request.query_params.get(
        "api_key")) else ("cookie" if (request and request.cookies.get(COOKIE_NAME)) else "none"))
    logger.debug(
        "auth_context_resolve mode=%s key_source=%s key_present=%s path=%s",
        mode, _source, bool(raw_key),
        request.url.path if request else "unknown",
    )

    if mode == "open":
        return auth_service.AuthContext(
            mode=mode,
            authenticated=False,
            permissions={"*"},
            is_admin=True,
            is_root=True,
        )

    if not raw_key:
        logger.warning(
            "auth_rejected reason=missing_credentials path=%s",
            request.url.path if request else "unknown",
        )
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Missing credentials"
        )

    with Session(get_engine()) as session:
        ctx = auth_service.resolve_auth_context(session, raw_key)
        if not ctx:
            logger.warning(
                "auth_rejected reason=invalid_credential key_prefix=%s path=%s",
                raw_key[:8] if raw_key else "?",
                request.url.path if request else "unknown",
            )
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN, detail="Could not validate credentials"
            )
        logger.debug(
            "auth_success user=%s operator=%s is_admin=%s is_root=%s session_id=%s path=%s",
            ctx.username, ctx.operator_name, ctx.is_admin, getattr(
                ctx, "is_root", False), ctx.session_id,
            request.url.path if request else "unknown",
        )
        return ctx


def _has_permission(auth: auth_service.AuthContext, permission: str) -> bool:
    if auth.is_open or auth.is_admin or getattr(auth, "is_root", False):
        return True
    return auth.can(permission)


def require_permission(permission: str):
    def _dep(auth=Depends(get_auth_context)):
        if not _has_permission(auth, permission):
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {permission}",
            )
        return auth

    return _dep


def require_permission_for_request(read_permission: str, write_permission: str):
    def _dep(request: Request, auth=Depends(get_auth_context)):
        if auth.is_open or auth.is_admin or getattr(auth, "is_root", False):
            return auth
        method = request.method.upper() if request else "GET"
        permission = read_permission if method in {
            "GET", "HEAD", "OPTIONS"} else write_permission
        if not auth.can(permission):
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {permission}",
            )
        return auth

    return _dep


def require_admin(auth=Depends(get_auth_context)):
    """Require admin access.  Open mode is always allowed."""
    if auth.is_open:
        return auth
    if not auth.is_admin and not getattr(auth, "is_root", False):
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )
    return auth
