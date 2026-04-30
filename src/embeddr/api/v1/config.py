import logging
from typing import List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select
from embeddr.db.session import get_session
from embeddr_core.models.config import AutoAnalysisConfig
from embeddr_core.plugin_interface import PluginIntent
from embeddr.core.plugin_loader import get_all_plugin_instances
from embeddr.api.security import require_permission_for_request
from embeddr.auth.permissions import Permissions

logger = logging.getLogger("embeddr.api.v1.config")

router = APIRouter(
    dependencies=[Depends(require_permission_for_request(
        Permissions.CONFIG_READ, Permissions.CONFIG_WRITE
    ))],
)


class AnalysisConfigSetRequest(BaseModel):
    scope: str  # "global" or "collection"
    scope_id: Optional[UUID] = None
    plugin_name: str
    enabled: bool
    priority: Optional[int] = 0


class AnalysisConfigResponse(BaseModel):
    scope: str
    scope_id: Optional[UUID] = None
    plugin_name: str
    enabled: bool
    priority: int


class PluginCapabilitiesResponse(BaseModel):
    plugin_name: str
    capabilities: List[dict]


@router.get("/schemas")
def list_config_schemas() -> List[dict]:
    """Return one entry per plugin that registered a config schema.

    Sourced from the Lotus registry's kind=config capabilities — petal plugins
    contribute these via the adapter (built from `config_model`); legacy and
    core plugins register them directly.
    """
    from embeddr.core.plugin_loader import get_lotus_registry
    from embeddr_core.models.lotus import LotusKind

    out: List[dict] = []
    reg = get_lotus_registry()
    for cap in reg.list(kind=LotusKind.config):
        data = cap.data or {}
        input_block = data.get("input") or {}
        out.append({
            "plugin_name": cap.plugin or "",
            "config_id": cap.id,
            "title": cap.title,
            "description": cap.description,
            "scope": data.get("scope", "global"),
            "schema": input_block.get("schema") or {},
            "defaults": input_block.get("defaults") or {},
            "ui": input_block.get("ui") or {},
            "renderer": data.get("renderer") or "",
        })
    return out


@router.get("/analysis/capabilities", response_model=List[PluginCapabilitiesResponse], deprecated=True)
def get_analysis_capabilities():
    """
    Deprecated: No longer used.
    """
    return []


@router.get("/analysis", response_model=List[AnalysisConfigResponse])
def get_analysis_configs(
    scope: Optional[str] = None,
    scope_id: Optional[UUID] = None,
    plugin_name: Optional[str] = None,
    session: Session = Depends(get_session)
):
    query = select(AutoAnalysisConfig)
    if scope:
        query = query.where(AutoAnalysisConfig.scope == scope)
    if scope_id:
        query = query.where(AutoAnalysisConfig.scope_id == scope_id)
    if plugin_name:
        query = query.where(AutoAnalysisConfig.plugin_name == plugin_name)

    configs = session.exec(query).all()
    return configs


@router.post("/analysis", response_model=AnalysisConfigResponse)
def set_analysis_config(req: AnalysisConfigSetRequest, session: Session = Depends(get_session)):
    """Set or update a configuration."""
    # Validate scope
    if req.scope == "global" and req.scope_id is not None:
        # Just ignore scope_id if scope is global? Or error?
        # Let's clean it up to avoid duplicates with scope_id set.
        req.scope_id = None

    query = select(AutoAnalysisConfig).where(
        AutoAnalysisConfig.scope == req.scope,
        AutoAnalysisConfig.scope_id == req.scope_id,
        AutoAnalysisConfig.plugin_name == req.plugin_name
    )
    existing = session.exec(query).first()

    if existing:
        existing.enabled = req.enabled
        if req.priority is not None:
            existing.priority = req.priority
        session.add(existing)
        session.commit()
        session.refresh(existing)
        return existing
    else:
        new_config = AutoAnalysisConfig(
            scope=req.scope,
            scope_id=req.scope_id,
            plugin_name=req.plugin_name,
            enabled=req.enabled,
            priority=req.priority or 0
        )
        session.add(new_config)
        session.commit()
        session.refresh(new_config)
        return new_config


@router.delete("/analysis")
def delete_analysis_config(scope: str, plugin_name: str, scope_id: Optional[UUID] = None, session: Session = Depends(get_session)):
    query = select(AutoAnalysisConfig).where(
        AutoAnalysisConfig.scope == scope,
        AutoAnalysisConfig.scope_id == scope_id,
        AutoAnalysisConfig.plugin_name == plugin_name
    )
    existing = session.exec(query).first()
    if existing:
        session.delete(existing)
        session.commit()
        return {"status": "deleted"}
    else:
        raise HTTPException(status_code=404, detail="Config not found")


# Per-plugin config get/set. Registered last so static paths above
# (`/schemas`, `/analysis`, `/analysis/capabilities`) are matched first
# before the dynamic `{plugin_name}` segment swallows them.

@router.get("/{plugin_name}")
def get_plugin_config(
    plugin_name: str,
    config_id: Optional[str] = None,
    session: Session = Depends(get_session),
) -> dict:
    """Return the persisted config value for a plugin, plus its schema / ui /
    renderer so the SystemPanel control-panel UI can render fields without a
    second round-trip.

    Shape matches what SystemPanel.tsx expects:
      { plugin_name, value, schema, defaults, ui, renderer, scope, title }
    """
    from embeddr_core.services.config_service import resolve_plugin_config
    from embeddr.core.plugin_loader import get_lotus_registry
    from embeddr_core.models.lotus import LotusKind

    value = resolve_plugin_config(
        session=session,
        plugin_name=plugin_name,
        config_id=config_id,
    ) or {}

    # Pull the kind=config capability for this plugin out of the lotus
    # registry so the response carries the schema definition the form needs.
    schema: dict = {}
    defaults: dict = {}
    ui: dict = {}
    renderer: str = ""
    scope: str = "global"
    title: str = ""
    description: str = ""
    cap_id_used: Optional[str] = None
    try:
        reg = get_lotus_registry()
        candidates = reg.list(kind=LotusKind.config, plugin=plugin_name)
        match = None
        if config_id:
            for cap in candidates:
                if cap.id == config_id:
                    match = cap
                    break
        if match is None and candidates:
            match = candidates[0]
        if match is not None:
            data = match.data or {}
            input_block = data.get("input") or {}
            schema = input_block.get("schema") or {}
            defaults = input_block.get("defaults") or {}
            ui = input_block.get("ui") or {}
            renderer = data.get("renderer") or ""
            scope = data.get("scope") or "global"
            title = match.title or ""
            description = match.description or ""
            cap_id_used = match.id
    except Exception as exc:
        logger.debug("config schema lookup failed for %s: %s", plugin_name, exc)

    return {
        "plugin_name": plugin_name,
        "config_id": cap_id_used,
        "title": title,
        "description": description,
        "scope": scope,
        "value": value,
        "schema": schema,
        "defaults": defaults,
        "ui": ui,
        "renderer": renderer,
    }


@router.put("/{plugin_name}")
def put_plugin_config(
    plugin_name: str,
    body: dict,
    config_id: Optional[str] = None,
    session: Session = Depends(get_session),
) -> dict:
    """Persist a new config value for a plugin.

    Accepts either {"value": {...}} (matches embeddr_client._put usage) or a
    raw object as the body itself.
    """
    from embeddr_core.services.config_service import set_plugin_config
    if isinstance(body, dict) and "value" in body and isinstance(body["value"], dict):
        value = body["value"]
    elif isinstance(body, dict):
        value = body
    else:
        raise HTTPException(status_code=400, detail="body must be an object")
    saved = set_plugin_config(
        session=session,
        plugin_name=plugin_name,
        value=value,
        config_id=config_id,
    )
    return {"plugin_name": plugin_name, "value": saved}
