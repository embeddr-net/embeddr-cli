from typing import List, Optional
from uuid import UUID
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select
from embeddr.db.session import get_session
from embeddr_core.models.config import AutoAnalysisConfig
from embeddr_core.models.analysis_capability import AnalysisCapability
from embeddr_core.plugin_interface import PluginIntent
from embeddr.core.plugin_loader import get_all_plugin_instances

router = APIRouter()


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
    capabilities: List[AnalysisCapability]


@router.get("/analysis/capabilities", response_model=List[PluginCapabilitiesResponse])
def get_analysis_capabilities():
    """
    Get all registered analysis capabilities from currently loaded plugins.
    """
    response = []
    loaded = get_all_plugin_instances()

    for plugin in loaded:
        # Check if plugin supports auto analysis
        if PluginIntent.AUTO_ANALYSIS in plugin.intents:
            caps = plugin.analysis_capabilities
            if caps:
                response.append(PluginCapabilitiesResponse(
                    plugin_name=plugin.name,
                    capabilities=caps
                ))

    return response


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
