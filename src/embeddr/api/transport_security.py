from __future__ import annotations

from http.cookies import SimpleCookie
from typing import Any, Callable, Dict, Iterable, Optional
from urllib.parse import parse_qs

from fastapi import Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlmodel import Session

from embeddr.api.security import COOKIE_NAME
from embeddr.db.session import get_engine
from embeddr.services import auth_service


def _normalize_permissions(required_permissions: Optional[Iterable[str]]) -> list[str]:
    return [str(permission).strip() for permission in (required_permissions or []) if str(permission).strip()]


def _has_required_permissions(auth: auth_service.AuthContext, required_permissions: list[str]) -> bool:
    if auth.is_open or auth.is_admin or getattr(auth, "is_root", False):
        return True
    if not required_permissions:
        return True
    return any(auth.can(permission) for permission in required_permissions)


def _extract_cookie_value(cookie_header: str, key: str) -> Optional[str]:
    if not cookie_header:
        return None
    cookie = SimpleCookie()
    try:
        cookie.load(cookie_header)
    except Exception:
        return None
    morsel = cookie.get(key)
    return morsel.value if morsel else None


def _extract_raw_credential_from_scope(scope: Dict[str, Any]) -> Optional[str]:
    headers = {
        k.decode("latin-1").lower(): v.decode("latin-1")
        for k, v in (scope.get("headers") or [])
    }
    header_key = headers.get("x-api-key")
    if header_key:
        return header_key

    query_string = scope.get("query_string", b"")
    query = parse_qs(query_string.decode(
        "latin-1") if isinstance(query_string, (bytes, bytearray)) else str(query_string))
    query_key_values = query.get("api_key")
    if query_key_values and query_key_values[0]:
        return str(query_key_values[0])

    cookie_header = headers.get("cookie")
    return _extract_cookie_value(cookie_header or "", COOKIE_NAME)


def resolve_request_auth_context(request: Request) -> auth_service.AuthContext:
    mode = auth_service.get_auth_mode()
    if mode == "open":
        return auth_service.AuthContext(
            mode=mode,
            authenticated=False,
            permissions={"*"},
            is_admin=True,
            is_root=True,
        )

    raw_credential = (
        request.headers.get("X-API-Key")
        or request.query_params.get("api_key")
        or request.cookies.get(COOKIE_NAME)
    )
    if not raw_credential:
        raise HTTPException(status_code=403, detail="Missing credentials")

    with Session(get_engine()) as session:
        auth = auth_service.resolve_auth_context(session, raw_credential)
    if not auth:
        raise HTTPException(
            status_code=403, detail="Could not validate credentials")
    return auth


def require_transport_auth(
    transport_name: str,
    *,
    required_permissions: Optional[Iterable[str]] = None,
) -> Callable[[Request], auth_service.AuthContext]:
    normalized_permissions = _normalize_permissions(required_permissions)

    def _dep(request: Request) -> auth_service.AuthContext:
        auth = resolve_request_auth_context(request)
        if not _has_required_permissions(auth, normalized_permissions):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Not authorized for transport '{transport_name}'. "
                    f"Requires one of: {', '.join(normalized_permissions)}"
                ),
            )
        return auth

    return _dep


class TransportAuthMiddleware:
    def __init__(
        self,
        app,
        *,
        transport_name: str,
        required_permissions: Optional[Iterable[str]] = None,
    ):
        self.app = app
        self.transport_name = transport_name
        self.required_permissions = _normalize_permissions(
            required_permissions)

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        mode = auth_service.get_auth_mode()
        if mode == "open":
            await self.app(scope, receive, send)
            return

        raw_credential = _extract_raw_credential_from_scope(scope)
        if not raw_credential:
            response = JSONResponse(
                status_code=403,
                content={"detail": "Missing credentials"},
            )
            await response(scope, receive, send)
            return

        with Session(get_engine()) as session:
            auth = auth_service.resolve_auth_context(session, raw_credential)

        if not auth:
            response = JSONResponse(
                status_code=403,
                content={"detail": "Could not validate credentials"},
            )
            await response(scope, receive, send)
            return

        if not _has_required_permissions(auth, self.required_permissions):
            response = JSONResponse(
                status_code=403,
                content={
                    "detail": (
                        f"Not authorized for transport '{self.transport_name}'. "
                        f"Requires one of: {', '.join(self.required_permissions)}"
                    )
                },
            )
            await response(scope, receive, send)
            return

        scope["embeddr.auth"] = auth
        await self.app(scope, receive, send)


def wrap_transport_asgi(
    app,
    *,
    transport_name: str,
    required_permissions: Optional[Iterable[str]] = None,
):
    return TransportAuthMiddleware(
        app,
        transport_name=transport_name,
        required_permissions=required_permissions,
    )


class AuthenticatedTransportApp:
    def __init__(
        self,
        app,
        *,
        transport_name: str,
        required_permissions: Optional[Iterable[str]] = None,
    ):
        self._app = app
        self.transport_name = transport_name
        self.required_permissions = _normalize_permissions(
            required_permissions) or ["lotus:dispatch"]

    def include_router(self, router, *args, **kwargs):
        auth_required = bool(kwargs.pop("embeddr_auth_required", True))
        if not auth_required:
            return self._app.include_router(router, *args, **kwargs)

        existing_dependencies = list(kwargs.get("dependencies") or [])
        existing_dependencies.append(
            Depends(
                require_transport_auth(
                    self.transport_name,
                    required_permissions=self.required_permissions,
                )
            )
        )
        kwargs["dependencies"] = existing_dependencies
        return self._app.include_router(router, *args, **kwargs)

    def mount(self, path: str, app, *args, **kwargs):
        auth_required = bool(kwargs.pop("embeddr_auth_required", True))
        if not auth_required:
            return self._app.mount(path, app, *args, **kwargs)

        secured_app = wrap_transport_asgi(
            app,
            transport_name=self.transport_name,
            required_permissions=self.required_permissions,
        )
        return self._app.mount(path, secured_app, *args, **kwargs)

    def __getattr__(self, item):
        return getattr(self._app, item)
