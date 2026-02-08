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
    api_key_header_val: str = Security(api_key_header_scheme),
    api_key_query_val: str = Security(api_key_query_scheme),
    request: Request = None,
):
    """
    Validate the API key from header, query param, or cookie.
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

    # DB-backed keys
    with Session(get_engine()) as session:
        api_key = auth_service.lookup_api_key(session, raw_key)
        if not api_key:
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN, detail="Could not validate credentials"
            )
        try:
            auth_service.touch_api_key(session, api_key)
        except Exception:
            logger.debug(
                "Failed to update api key last_used_at", exc_info=True)
        return raw_key


def check_auth_enabled() -> bool:
    return auth_service.is_auth_enabled()


async def get_auth_context(
    api_key_header_val: str = Security(api_key_header_scheme),
    api_key_query_val: str = Security(api_key_query_scheme),
    request: Request = None,
):
    raw_key = _extract_raw_key(api_key_header_val, api_key_query_val, request)
    mode = auth_service.get_auth_mode()

    if mode == "open":
        return auth_service.AuthContext(
            mode=mode,
            authenticated=False,
            permissions={"*"},
            is_admin=True,
        )

    if not raw_key:
        raise HTTPException(
            status_code=HTTP_403_FORBIDDEN, detail="Missing credentials"
        )

    with Session(get_engine()) as session:
        api_key = auth_service.lookup_api_key(session, raw_key)
        if not api_key:
            raise HTTPException(
                status_code=HTTP_403_FORBIDDEN, detail="Could not validate credentials"
            )
        return auth_service.build_auth_context(session, api_key, raw_key)


def _has_permission(auth: auth_service.AuthContext, permission: str) -> bool:
    if auth.is_open or auth.is_admin:
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
        if auth.is_open or auth.is_admin:
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
