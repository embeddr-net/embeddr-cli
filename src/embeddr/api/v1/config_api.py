from __future__ import annotations

from typing import Any, Dict, Optional

from embeddr_core.services.config_service import resolve_plugin_config, set_plugin_config
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlmodel import Session

from embeddr.db.session import get_session
from embeddr.api.security import require_permission_for_request
from embeddr.auth.permissions import Permissions

router = APIRouter(
    dependencies=[Depends(require_permission_for_request(
        Permissions.CONFIG_READ, Permissions.CONFIG_WRITE
    ))],
)


class ConfigGetResponse(BaseModel):
    ok: bool = True
    plugin: str
    config_id: Optional[str] = None
    scope: str = "global"
    scope_id: Optional[str] = None
    value: Dict[str, Any] = Field(default_factory=dict)


class ConfigSetRequest(BaseModel):
    value: Dict[str, Any] = Field(default_factory=dict)
    scope: str = "global"
    scope_id: Optional[str] = None
    config_id: Optional[str] = None


class ConfigSetResponse(ConfigGetResponse):
    pass


@router.get("/config/{plugin_name}", response_model=ConfigGetResponse)
def get_config(
    plugin_name: str,
    scope: str = Query("global"),
    scope_id: Optional[str] = Query(None),
    config_id: Optional[str] = Query(None),
    session: Session = Depends(get_session),
):
    try:
        value = resolve_plugin_config(
            session=session,
            plugin_name=plugin_name,
            scope=scope,
            scope_id=scope_id,
            config_id=config_id,
        )
        return ConfigGetResponse(
            plugin=plugin_name,
            config_id=config_id,
            scope=scope,
            scope_id=scope_id,
            value=value,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/config/{plugin_name}", response_model=ConfigSetResponse)
def put_config(
    plugin_name: str,
    payload: ConfigSetRequest,
    session: Session = Depends(get_session),
):
    try:
        value = set_plugin_config(
            session=session,
            plugin_name=plugin_name,
            value=payload.value,
            scope=payload.scope,
            scope_id=payload.scope_id,
            config_id=payload.config_id,
        )
        return ConfigSetResponse(
            plugin=plugin_name,
            config_id=payload.config_id,
            scope=payload.scope,
            scope_id=payload.scope_id,
            value=value,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
