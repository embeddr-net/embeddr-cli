from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select, col

from embeddr.api.v1.lotus_service import lotus_prepare_inputs
from embeddr.core.event_bus import _EVENT_BUS
from embeddr.core.plugin_loader import (
    get_all_plugin_instances,
    get_lotus_registry,
    _PLUGIN_CAPABILITY_REGISTRY,
)
from embeddr.db.session import get_session
from embeddr_core.models.artifact import Artifact
from embeddr_core.models.artifact_blob import ArtifactBlob
from embeddr.core.model_imports import import_model_with_fallbacks
from embeddr_core.models.lotus import LotusKind
from embeddr_core.plugin_interface import PluginContext
from embeddr_core.services.resource_manager import resource_manager
from embeddr_core.services.config_service import resolve_plugin_config
from embeddr.core.plugin_context_helpers import LotusContext
from embeddr.services.storage import storage_service
from embeddr.services.blob_registry import get_resolver_for_provider
from embeddr.services.resource_adapter_registry import (
    list_resource_adapters,
    select_resource_adapter,
)
from embeddr.api.security import get_auth_context, require_permission_for_request
from embeddr.auth.permissions import Permissions

router = APIRouter(
    dependencies=[Depends(require_permission_for_request(
        Permissions.RESOURCES_READ, Permissions.RESOURCES_WRITE
    ))],
)


logger = logging.getLogger("embeddr.api.resources")


def _trace_enabled() -> bool:
    return os.environ.get("EMBEDDR_RESOURCE_TRACE") == "1" or os.environ.get("EMBEDDR_LOTUS_TRACE") == "1"


class ResourceResolveInput(BaseModel):
    artifact_id: Optional[str] = None
    url: Optional[str] = None
    hint_type: Optional[str] = None
    adapter_id: Optional[str] = None


def _visibility_from_metadata(metadata_json: Optional[Dict[str, Any]]) -> str:
    if isinstance(metadata_json, dict):
        raw = metadata_json.get("visibility")
        if isinstance(raw, str):
            value = raw.strip().lower()
            if value in {"public", "private"}:
                return value
    return "private"


def _artifact_is_readable(artifact: Artifact, auth) -> bool:
    if not auth:
        return True
    if getattr(auth, "is_open", False) or getattr(auth, "is_admin", False) or getattr(auth, "is_root", False):
        return True

    user_id = getattr(auth, "user_id", None)
    if user_id and artifact.owner_user_id == user_id:
        return True

    return _visibility_from_metadata(artifact.metadata_json) == "public"


def _resolve_plugin(plugin_name: str):
    for p in get_all_plugin_instances():
        if p.name == plugin_name:
            return p
    return None


def _cap_exposes_api(cap) -> bool:
    action = getattr(cap, "action", None)
    expose = (getattr(action, "expose", None) or {}) if action else {}
    if not expose:
        data = cap.data or {}
        expose = data.get("expose") or {}
    return bool(expose.get("api", False))


def _cap_input_model(cap):
    data = cap.data or {}
    input_block = data.get("input") or {}
    model_path = input_block.get("model")

    plugin_name = str(data.get("plugin_name")
                      or data.get("plugin") or cap.plugin or "")
    plugin_module = data.get("plugin_module")

    if not isinstance(model_path, str) or not model_path.strip():
        return None

    return import_model_with_fallbacks(
        model_path,
        plugin_name=plugin_name,
        plugin_module=plugin_module,
    )


def _invoke_capability_sync(cap_id: str, inputs: Dict[str, Any], session: Session) -> Dict[str, Any]:
    reg = get_lotus_registry()
    cap = reg.get(cap_id)
    if not cap:
        raise HTTPException(404, f"Capability not found: {cap_id}")
    if cap.kind not in (LotusKind.action, LotusKind.resolver):
        raise HTTPException(
            400, "Only action or resolver capabilities can be invoked")
    if not _cap_exposes_api(cap):
        raise HTTPException(403, f"Capability not exposed to API: {cap_id}")

    data = cap.data or {}
    plugin_name = str(data.get("plugin_name") or data.get("plugin") or "")
    if not plugin_name:
        plugin_name = str(getattr(cap, "plugin", "") or "")

    action_model = getattr(cap, "action", None)
    action_name = str(
        data.get("action_name")
        or data.get("action")
        or (getattr(action_model, "action", None) if action_model else "")
        or ""
    )

    if _trace_enabled():
        logger.warning(
            "[ResourceTrace] invoke capability cap_id=%s plugin=%s action=%s",
            cap_id,
            plugin_name,
            action_name,
        )

    if not plugin_name or not action_name:
        raise HTTPException(500, f"Capability missing plugin/action: {cap_id}")

    Model = _cap_input_model(cap)
    if Model is not None:
        obj = Model.model_validate(inputs)
        inputs = obj.model_dump()

    inputs = lotus_prepare_inputs(
        plugin_name=plugin_name,
        inputs=inputs,
        session=session,
    )

    plugin = _resolve_plugin(plugin_name)
    if not plugin:
        raise HTTPException(400, f"Plugin not found: {plugin_name}")

    ctx = PluginContext(
        bus=_EVENT_BUS,
        capability_registry=_PLUGIN_CAPABILITY_REGISTRY,
        resources=resource_manager,
        config=resolve_plugin_config(
            session=session,
            plugin_name=plugin_name,
        ),
        lotus=LotusContext(session=session),
    )

    return plugin.execute(action_name, None, inputs, context=ctx)


def _resolve_artifact_urls(session: Session, artifact_id: UUID, auth) -> Dict[str, Any]:
    artifact = session.get(Artifact, artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")
    if not _artifact_is_readable(artifact, auth):
        raise HTTPException(404, "Artifact not found")

    blob = session.exec(
        select(ArtifactBlob)
        .where(ArtifactBlob.artifact_id == artifact_id)
        .order_by(col(ArtifactBlob.created_at).desc())
    ).first()

    base_type = artifact.base_type_name or artifact.get_base_type_name()

    if blob:
        original = storage_service.resolve_blob(blob, purpose="original")
        preview = storage_service.resolve_blob(blob, purpose="preview")
        resolver = get_resolver_for_provider(blob.storage_backend)
        return {
            "ok": True,
            "id": str(artifact_id),
            "type": artifact.type_name or base_type or "artifact",
            "base_type": base_type or "artifact",
            "original": original,
            "preview": preview,
            "storage_provider": blob.storage_backend,
            "storage_resolver": resolver.name if resolver else None,
        }

    base = f"/api/artifacts/{artifact_id}"
    return {
        "ok": True,
        "id": str(artifact_id),
        "type": artifact.type_name or base_type or "artifact",
        "base_type": base_type or "artifact",
        "content_url": f"{base}/content",
        "preview_url": f"{base}/preview",
        "url": f"{base}/content",
    }


@router.get("/adapters")
def get_resource_adapters():
    return {"adapters": list_resource_adapters()}


@router.post("/resolve")
def resolve_resource(
    payload: ResourceResolveInput,
    background_tasks: BackgroundTasks = None,
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    if _trace_enabled():
        logger.warning(
            "[ResourceTrace] /resources/resolve payload artifact_id=%s url=%s hint_type=%s adapter_id=%s",
            payload.artifact_id,
            payload.url,
            payload.hint_type,
            payload.adapter_id,
        )

    resolved_artifact_id: Optional[UUID] = None
    if payload.artifact_id:
        try:
            resolved_artifact_id = UUID(str(payload.artifact_id))
        except ValueError:
            resolved_artifact_id = None

    if resolved_artifact_id:
        resolved = _resolve_artifact_urls(session, resolved_artifact_id, auth)
        cap = select_resource_adapter(
            artifact_id=str(resolved_artifact_id),
            url=payload.url,
            adapter_id=payload.adapter_id,
        )
        if cap:
            resolved["adapter_id"] = cap.id
            resolved["adapter_plugin"] = cap.plugin
            resolved["adapter_title"] = cap.title
        return resolved

    cap = select_resource_adapter(
        artifact_id=payload.artifact_id,
        url=payload.url,
        adapter_id=payload.adapter_id,
    )

    if cap:
        if _trace_enabled():
            logger.warning(
                "[ResourceTrace] adapter selected id=%s plugin=%s title=%s",
                cap.id,
                cap.plugin,
                cap.title,
            )
        inputs = {
            "artifact_id": payload.artifact_id,
            "url": payload.url,
            "hint_type": payload.hint_type,
        }
        out = _invoke_capability_sync(cap.id, inputs, session)
        if isinstance(out, dict):
            out["adapter_id"] = cap.id
            out["adapter_plugin"] = cap.plugin
            out["adapter_title"] = cap.title
        return out

    if _trace_enabled():
        logger.warning(
            "[ResourceTrace] no adapter selected, falling back to raw url")

    if payload.url:
        return {
            "ok": True,
            "id": payload.artifact_id,
            "type": payload.hint_type or "image",
            "content_url": payload.url,
            "preview_url": payload.url,
            "url": payload.url,
        }

    return {
        "ok": True,
        "id": payload.artifact_id,
        "type": payload.hint_type or "image",
    }
