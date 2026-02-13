"""Panel session REST endpoints.

All endpoints are scoped to the authenticated client credential.
Panels are never leaked across tenants.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlmodel import Session

from embeddr.api.security import get_auth_context, require_permission_for_request
from embeddr.db.session import get_session
from embeddr.services.auth_service import AuthContext
from embeddr.services import panel_session_service as svc

router = APIRouter(
    dependencies=[
        Depends(require_permission_for_request(
            "panels:read", "panels:write"))
    ]
)
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Request / response DTOs
# ---------------------------------------------------------------------------

class PanelRegisterRequest(BaseModel):
    panel_id: str
    panel_type: str
    window_id: Optional[str] = None
    title: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None
    items: Optional[List[Dict[str, Any]]] = None


class PanelUpdateRequest(BaseModel):
    meta: Optional[Dict[str, Any]] = None
    items: Optional[List[Dict[str, Any]]] = None
    title: Optional[str] = None


class PanelResponse(BaseModel):
    id: UUID
    panel_id: str
    panel_type: str
    window_id: Optional[str] = None
    title: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None
    items: Optional[List[Dict[str, Any]]] = None
    client_id: Optional[UUID] = None
    credential_id: Optional[UUID] = None
    operator_id: Optional[UUID] = None
    created_at: str
    last_active_at: str
    closed_at: Optional[str] = None


def _to_response(ps) -> PanelResponse:
    return PanelResponse(
        id=ps.id,
        panel_id=ps.panel_id,
        panel_type=ps.panel_type,
        window_id=ps.window_id,
        title=ps.title,
        meta=ps.meta,
        items=ps.items,
        client_id=ps.client_id,
        credential_id=ps.credential_id,
        operator_id=ps.operator_id,
        created_at=ps.created_at.isoformat(),
        last_active_at=ps.last_active_at.isoformat(),
        closed_at=ps.closed_at.isoformat() if ps.closed_at else None,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/")
def list_panels(
    panel_type: Optional[str] = None,
    include_closed: bool = False,
    limit: int = 50,
    session: Session = Depends(get_session),
    auth: AuthContext = Depends(get_auth_context),
) -> List[PanelResponse]:
    panels = svc.list_panels(
        session, auth,
        panel_type=panel_type, include_closed=include_closed, limit=limit,
    )
    return [_to_response(p) for p in panels]


@router.get("/last-active")
def last_active_panel(
    panel_type: Optional[str] = None,
    session: Session = Depends(get_session),
    auth: AuthContext = Depends(get_auth_context),
) -> Optional[PanelResponse]:
    ps = svc.get_last_active_panel(session, auth, panel_type=panel_type)
    if not ps:
        raise HTTPException(status_code=404, detail="No active panel found")
    return _to_response(ps)


@router.post("/", status_code=201)
def register_panel(
    body: PanelRegisterRequest,
    session: Session = Depends(get_session),
    auth: AuthContext = Depends(get_auth_context),
) -> PanelResponse:
    ps = svc.register_panel(
        session, auth,
        panel_id=body.panel_id,
        panel_type=body.panel_type,
        window_id=body.window_id,
        title=body.title,
        meta=body.meta,
        items=body.items,
    )
    return _to_response(ps)


@router.get("/{panel_id}")
def get_panel(
    panel_id: str,
    session: Session = Depends(get_session),
    auth: AuthContext = Depends(get_auth_context),
) -> PanelResponse:
    ps = svc.get_panel(session, auth, panel_id=panel_id)
    if not ps:
        raise HTTPException(
            status_code=404, detail=f"Panel not found: {panel_id}")
    return _to_response(ps)


@router.patch("/{panel_id}")
def update_panel(
    panel_id: str,
    body: PanelUpdateRequest,
    session: Session = Depends(get_session),
    auth: AuthContext = Depends(get_auth_context),
) -> PanelResponse:
    ps = svc.update_panel(
        session, auth,
        panel_id=panel_id,
        meta=body.meta,
        items=body.items,
        title=body.title,
    )
    if not ps:
        raise HTTPException(
            status_code=404, detail=f"Panel not found: {panel_id}")
    return _to_response(ps)


@router.post("/{panel_id}/touch")
def touch_panel(
    panel_id: str,
    session: Session = Depends(get_session),
    auth: AuthContext = Depends(get_auth_context),
) -> PanelResponse:
    ps = svc.touch_panel(session, auth, panel_id=panel_id)
    if not ps:
        raise HTTPException(
            status_code=404, detail=f"Panel not found: {panel_id}")
    return _to_response(ps)


@router.delete("/{panel_id}")
def close_panel(
    panel_id: str,
    session: Session = Depends(get_session),
    auth: AuthContext = Depends(get_auth_context),
) -> PanelResponse:
    ps = svc.close_panel(session, auth, panel_id=panel_id)
    if not ps:
        raise HTTPException(
            status_code=404, detail=f"Panel not found: {panel_id}")
    return _to_response(ps)
