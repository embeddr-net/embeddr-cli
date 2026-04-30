"""Artifact type API: list, get, and summarize artifact types.

Mirrors the routes the Go server exposed at /api/types/*. Plugins register
types via TypeDef (petal) or provided_artifact_types (EmbeddrPlugin); both
land in the artifact_types table. These endpoints read from there.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, func, select

from embeddr.db.session import get_session
from embeddr_core.models.artifact import Artifact
from embeddr_core.models.artifact_type import ArtifactType

logger = logging.getLogger("embeddr.api.v1.types")

router = APIRouter()


def _is_deprecated(t: ArtifactType) -> bool:
    meta = t.metadata_json or {}
    return bool(meta.get("deprecated"))


def _serialize_type(t: ArtifactType, artifact_count: int | None = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "name": t.name,
        "default_capabilities": list(t.default_capabilities or []),
        "metadata": dict(t.metadata_json or {}),
    }
    if t.parent_name:
        out["parent_name"] = t.parent_name
    if t.description:
        out["description"] = t.description
    if artifact_count is not None:
        out["artifact_count"] = artifact_count
    return out


@router.get("/")
def list_types(
    include_deprecated: bool = Query(False),
    session: Session = Depends(get_session),
) -> List[Dict[str, Any]]:
    rows = session.exec(select(ArtifactType)).all()
    if not include_deprecated:
        rows = [t for t in rows if not _is_deprecated(t)]
    return [_serialize_type(t) for t in rows]


@router.get("/summary")
def list_types_summary(
    include_deprecated: bool = Query(False),
    session: Session = Depends(get_session),
) -> Dict[str, Any]:
    """List types with per-type artifact counts and a global total.

    Deprecated types (those whose registering plugin is no longer loaded)
    are excluded by default; pass `?include_deprecated=true` to see them.
    """
    types = session.exec(select(ArtifactType)).all()
    if not include_deprecated:
        types = [t for t in types if not _is_deprecated(t)]

    count_rows = session.exec(
        select(Artifact.type_name, func.count(Artifact.id)).group_by(Artifact.type_name)
    ).all()
    counts: Dict[str, int] = {name: int(n) for name, n in count_rows}

    total = 0
    items: List[Dict[str, Any]] = []
    for t in types:
        n = counts.get(t.name, 0)
        total += n
        items.append(_serialize_type(t, artifact_count=n))

    return {"types": items, "total_artifacts": total}


@router.get("/{type_name}")
def get_type(type_name: str, session: Session = Depends(get_session)) -> Dict[str, Any]:
    t = session.get(ArtifactType, type_name)
    if not t:
        raise HTTPException(status_code=404, detail=f"Type not found: {type_name}")
    return _serialize_type(t)
