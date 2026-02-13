"""Panel session service.

CRUD operations for PanelSession — scoped to the requesting client
credential so panels are never leaked across tenants.  All queries
enforce ownership checks unless the caller is admin.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlmodel import Session, select

from embeddr_core.models.panel_session import PanelSession
from embeddr.services.auth_service import AuthContext

logger = logging.getLogger("embeddr.services.panel_session")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _ownership_filter(
    stmt,
    auth: AuthContext,
):
    """Apply ownership filter so only panels matching the credential are
    returned.  Admins in 'open' mode bypass the check."""
    if auth.is_open or auth.is_admin:
        return stmt

    # Prefer credential scoping; fall back to client (user) scoping.
    if auth.api_key_id:
        return stmt.where(PanelSession.credential_id == auth.api_key_id)
    if auth.user_id:
        return stmt.where(PanelSession.client_id == auth.user_id)

    # No identity — return nothing
    return stmt.where(PanelSession.id == None)  # noqa: E711


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def register_panel(
    session: Session,
    auth: AuthContext,
    *,
    panel_id: str,
    panel_type: str,
    window_id: Optional[str] = None,
    title: Optional[str] = None,
    meta: Optional[Dict[str, Any]] = None,
    items: Optional[List[Dict[str, Any]]] = None,
) -> PanelSession:
    """Create or update a panel session.

    If a panel with the same ``panel_id`` already exists for this
    credential, update it instead of creating a duplicate.
    """
    existing = get_panel(session, auth, panel_id=panel_id)
    if existing:
        return update_panel(
            session,
            auth,
            panel_id=panel_id,
            meta=meta,
            items=items,
            title=title,
        )

    ps = PanelSession(
        client_id=auth.user_id,
        credential_id=auth.api_key_id,
        operator_id=auth.operator_id,
        panel_id=panel_id,
        panel_type=panel_type,
        window_id=window_id,
        title=title,
        meta=meta,
        items=items,
    )
    session.add(ps)
    session.commit()
    session.refresh(ps)
    logger.info("Panel session registered: %s (%s)",
                ps.panel_id, ps.panel_type)
    return ps


def update_panel(
    session: Session,
    auth: AuthContext,
    *,
    panel_id: str,
    meta: Optional[Dict[str, Any]] = None,
    items: Optional[List[Dict[str, Any]]] = None,
    title: Optional[str] = None,
) -> Optional[PanelSession]:
    """Update an existing panel session's metadata, items, or title."""
    ps = get_panel(session, auth, panel_id=panel_id)
    if not ps:
        logger.warning("Panel not found for update: %s", panel_id)
        return None

    if meta is not None:
        ps.meta = meta
    if items is not None:
        ps.items = items
    if title is not None:
        ps.title = title

    ps.last_active_at = datetime.now(timezone.utc)
    session.add(ps)
    session.commit()
    session.refresh(ps)
    return ps


def touch_panel(
    session: Session,
    auth: AuthContext,
    *,
    panel_id: str,
) -> Optional[PanelSession]:
    """Bump ``last_active_at`` without changing content."""
    ps = get_panel(session, auth, panel_id=panel_id)
    if not ps:
        return None
    ps.last_active_at = datetime.now(timezone.utc)
    session.add(ps)
    session.commit()
    session.refresh(ps)
    return ps


def close_panel(
    session: Session,
    auth: AuthContext,
    *,
    panel_id: str,
) -> Optional[PanelSession]:
    """Mark a panel session as closed (soft delete)."""
    ps = get_panel(session, auth, panel_id=panel_id)
    if not ps:
        return None
    ps.closed_at = datetime.now(timezone.utc)
    session.add(ps)
    session.commit()
    session.refresh(ps)
    logger.info("Panel session closed: %s", panel_id)
    return ps


def get_panel(
    session: Session,
    auth: AuthContext,
    *,
    panel_id: str,
) -> Optional[PanelSession]:
    """Fetch a single panel by its panel_id, scoped to the credential."""
    stmt = select(PanelSession).where(
        PanelSession.panel_id == panel_id,
        PanelSession.closed_at == None,  # noqa: E711
    )
    stmt = _ownership_filter(stmt, auth)
    return session.exec(stmt).first()


def list_panels(
    session: Session,
    auth: AuthContext,
    *,
    panel_type: Optional[str] = None,
    include_closed: bool = False,
    limit: int = 50,
) -> List[PanelSession]:
    """List panels for the current credential."""
    stmt = select(PanelSession)
    if not include_closed:
        stmt = stmt.where(PanelSession.closed_at == None)  # noqa: E711
    if panel_type:
        stmt = stmt.where(PanelSession.panel_type == panel_type)
    stmt = _ownership_filter(stmt, auth)
    stmt = stmt.order_by(PanelSession.last_active_at.desc()).limit(
        limit)  # type: ignore[union-attr]
    return list(session.exec(stmt).all())


def get_last_active_panel(
    session: Session,
    auth: AuthContext,
    *,
    panel_type: Optional[str] = None,
) -> Optional[PanelSession]:
    """Return the most recently active panel for this credential."""
    panels = list_panels(session, auth, panel_type=panel_type, limit=1)
    return panels[0] if panels else None
