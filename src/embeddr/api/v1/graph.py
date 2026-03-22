"""
Graph API — /api/graph

First-class router for the artifact relation graph.

Endpoints:
  GET  /graph/taxonomy                — relation types, families, source namespaces
  POST /graph/query                   — BFS traversal from seed artifact(s)
  GET  /graph/artifact/{id}           — quick subgraph rooted at a single artifact

  POST   /graph/relations             — create a relation edge
  GET    /graph/relations             — list relations with filters
  DELETE /graph/relations/{id}        — delete a specific relation by its UUID
  PATCH  /graph/relations/{id}        — update weight or metadata on a relation

  GET  /graph/relation-types          — list all registered relation types
  POST /graph/relation-types          — register a new relation type (admin/plugin use)

Backward-compat aliases for the old /artifacts/graph/* and /artifacts/{id}/subgraph
endpoints are kept in artifacts.py as thin forwarders.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Dict, List, Optional, Set
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field as PydanticField
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select, or_, func, cast, String, delete

from embeddr.api.security import get_auth_context
from embeddr.db.session import get_engine
from embeddr_core.models.artifact import Artifact
from embeddr_core.models.artifact_embedding import ArtifactEmbedding
from embeddr_core.models.artifact_annotation import ArtifactAnnotation
from embeddr_core.models.artifact_feature import ArtifactFeatureRef
from embeddr_core.models.artifact_lineage import ArtifactLineage
from embeddr_core.models.artifact_relation import ArtifactRelation
from embeddr_core.models.relation_type import RelationTypeDef
from embeddr.services.graph_query_service import (
    GraphQueryFilters,
    run_graph_bfs,
    get_graph_taxonomy_payload,
)

logger = logging.getLogger("embeddr.api.graph")
router = APIRouter()


# ---------------------------------------------------------------------------
# Session / auth helpers (mirrors pattern from artifacts.py)
# ---------------------------------------------------------------------------

def get_session():
    with Session(get_engine()) as session:
        yield session


def _filter_allowed_ids(session: Session, ids: List[UUID], auth) -> Set[UUID]:
    if not auth or auth.is_open or auth.is_admin or getattr(auth, "is_root", False):
        return set(ids)
    from sqlalchemy import literal

    def _owner_match():
        user_id = getattr(auth, "user_id", None)
        if user_id:
            return Artifact.owner_user_id == user_id
        return literal(False)

    def _public_vis():
        meta_text = func.lower(cast(Artifact.metadata_json, String))
        return or_(
            meta_text.contains('"visibility":"public"'),
            meta_text.contains('"visibility": "public"'),
        )

    rows = session.exec(
        select(Artifact.id).where(Artifact.id.in_(ids)).where(or_(_owner_match(), _public_vis()))
    ).all()
    return set(rows)


def _ensure_artifact_access(session: Session, artifact_id: UUID, auth, *, for_write: bool = False) -> Artifact:
    artifact = session.get(Artifact, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    is_open = not auth or auth.is_open or auth.is_admin or getattr(auth, "is_root", False)
    if is_open:
        return artifact

    user_id = getattr(auth, "user_id", None)
    is_owner = user_id and artifact.owner_user_id == user_id

    if for_write and not is_owner:
        raise HTTPException(status_code=404, detail="Artifact not found")

    if not for_write:
        vis = (artifact.metadata_json or {}).get("visibility", "private")
        if not is_owner and vis != "public":
            raise HTTPException(status_code=404, detail="Artifact not found")

    return artifact


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class RelationCreate(BaseModel):
    source_id: UUID
    target_id: UUID
    relation_type: str
    source_namespace: str = "user"
    weight: Optional[float] = None
    metadata_json: Dict[str, Any] = PydanticField(default_factory=dict)


class RelationUpdate(BaseModel):
    weight: Optional[float] = None
    metadata_json: Optional[Dict[str, Any]] = None


class RelationResponse(BaseModel):
    id: UUID
    source_id: UUID
    target_id: UUID
    relation_type: str
    source_namespace: str
    created_at: datetime
    weight: Optional[float]
    metadata_json: Dict[str, Any]


class GraphQueryFiltersInput(BaseModel):
    relation_types_include: List[str] = PydanticField(default_factory=list)
    relation_types_exclude: List[str] = PydanticField(default_factory=list)
    relation_families_include: List[str] = PydanticField(default_factory=list)
    source_namespaces_include: List[str] = PydanticField(default_factory=list)
    source_namespaces_exclude: List[str] = PydanticField(default_factory=list)
    artifact_types_include: List[str] = PydanticField(default_factory=list)
    artifact_base_types_include: List[str] = PydanticField(default_factory=list)
    include_legacy_stash_contains: bool = False


class GraphQueryRequest(BaseModel):
    seed_ids: List[UUID]
    max_depth: int = 2
    direction: str = "both"
    include_lineage: bool = True
    include_relations: bool = True
    limit_nodes: int = 1500
    limit_edges: int = 6000
    include_overlay_counts: bool = True
    filters: GraphQueryFiltersInput = PydanticField(default_factory=GraphQueryFiltersInput)


class RelationTypeCreate(BaseModel):
    name: str
    family: str = "other"
    inverse: Optional[str] = None
    transitive: bool = False
    structural: bool = False
    description: str = ""
    registered_by: str = "user"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _relation_to_response(rel: ArtifactRelation) -> RelationResponse:
    return RelationResponse(
        id=rel.id,
        source_id=rel.source_id,
        target_id=rel.target_id,
        relation_type=rel.relation_type,
        source_namespace=rel.source_namespace,
        created_at=rel.created_at,
        weight=rel.weight,
        metadata_json=rel.metadata_json or {},
    )


def _artifact_label(artifact: Artifact) -> str:
    meta = artifact.metadata_json or {}
    return (
        meta.get("name")
        or meta.get("filename")
        or meta.get("title")
        or str(artifact.id)[:8]
    )


# ---------------------------------------------------------------------------
# Taxonomy
# ---------------------------------------------------------------------------

@router.get("/taxonomy")
def graph_taxonomy(
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    """
    Return all known relation types, families, and observed source namespaces.
    Includes core built-ins and plugin-registered types from the RelationTypeDef table.
    """
    _ = auth
    return get_graph_taxonomy_payload(session)


# ---------------------------------------------------------------------------
# Graph traversal query
# ---------------------------------------------------------------------------

@router.post("/query")
def graph_query(
    req: GraphQueryRequest,
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    """
    BFS traversal of the artifact graph from one or more seed artifacts.

    Returns nodes (artifacts) and edges (relations + lineage) within max_depth
    hops. Results are filtered by the caller's access permissions.
    """
    if not req.seed_ids:
        return {"nodes": [], "edges": [], "meta": {"truncated": False, "nodes_returned": 0, "edges_returned": 0}}

    direction = (req.direction or "both").strip().lower()
    if direction not in {"incoming", "outgoing", "both"}:
        raise HTTPException(status_code=422, detail="direction must be one of: incoming, outgoing, both")

    started_at = perf_counter()

    for seed_id in req.seed_ids:
        _ensure_artifact_access(session, seed_id, auth)

    filters = GraphQueryFilters.from_dict(req.filters.model_dump())
    traversal = run_graph_bfs(
        session=session,
        seed_ids=req.seed_ids,
        max_depth=req.max_depth,
        direction=direction,  # type: ignore[arg-type]
        include_lineage=req.include_lineage,
        include_relations=req.include_relations,
        filters=filters,
        limit_nodes=req.limit_nodes,
        limit_edges=req.limit_edges,
    )

    traversed_node_ids = set(traversal["node_ids"])
    traversed_edges = list(traversal["edges"])

    if not traversed_node_ids:
        return {"nodes": [], "edges": [], "meta": {**(traversal.get("meta") or {}), "nodes_returned": 0, "edges_returned": 0}}

    allowed_ids = _filter_allowed_ids(session, list(traversed_node_ids), auth)
    artifacts = session.exec(select(Artifact).where(Artifact.id.in_(allowed_ids))).all()

    include_types = set(req.filters.artifact_types_include or [])
    include_base_types = set(req.filters.artifact_base_types_include or [])

    if include_types or include_base_types:
        artifacts = [
            a for a in artifacts
            if (not include_types or a.type_name in include_types)
            and (not include_base_types or a.base_type_name in include_base_types)
        ]

    artifact_by_id = {a.id: a for a in artifacts}
    filtered_node_ids = set(artifact_by_id.keys())

    filtered_edges = [
        e for e in traversed_edges
        if e["source_id"] in filtered_node_ids and e["target_id"] in filtered_node_ids
    ]

    # Degree map for UI sizing hints
    degree_map: Dict[UUID, Dict[str, int]] = {nid: {"in": 0, "out": 0, "total": 0} for nid in filtered_node_ids}
    for edge in filtered_edges:
        src, tgt = edge["source_id"], edge["target_id"]
        if src in degree_map:
            degree_map[src]["out"] += 1
            degree_map[src]["total"] += 1
        if tgt in degree_map:
            degree_map[tgt]["in"] += 1
            degree_map[tgt]["total"] += 1

    # Optional overlay counts (features / embeddings / annotations per node)
    overlay_counts: Dict[UUID, Dict[str, int]] = {nid: {"features": 0, "embeddings": 0, "annotations": 0} for nid in filtered_node_ids}
    if req.include_overlay_counts and filtered_node_ids:
        for model_cls, key in [
            (ArtifactFeatureRef, "features"),
            (ArtifactEmbedding, "embeddings"),
            (ArtifactAnnotation, "annotations"),
        ]:
            rows = session.exec(
                select(model_cls.artifact_id, func.count(model_cls.id))
                .where(model_cls.artifact_id.in_(filtered_node_ids))
                .group_by(model_cls.artifact_id)
            ).all()
            for artifact_id, count in rows:
                if artifact_id in overlay_counts:
                    overlay_counts[artifact_id][key] = int(count or 0)

    seed_set = set(req.seed_ids)
    sorted_artifacts = sorted(
        artifacts,
        key=lambda a: (0 if a.id in seed_set else 1, _artifact_label(a).lower(), str(a.id)),
    )

    node_payload = [
        {
            "id": str(a.id),
            "type_name": a.type_name,
            "base_type_name": a.base_type_name,
            "label": _artifact_label(a),
            "uri": a.uri,
            "is_seed": a.id in seed_set,
            "degree": degree_map.get(a.id, {}),
            "overlays": overlay_counts.get(a.id, {}),
            "metadata": a.metadata_json or {},
        }
        for a in sorted_artifacts
    ]

    edge_payload = []
    for edge in filtered_edges:
        src, tgt = str(edge["source_id"]), str(edge["target_id"])
        rt = str(edge["relation_type_raw"])
        ns = str(edge.get("source_namespace", ""))
        edge_payload.append({
            "id": f"{src}::{tgt}::{rt}::{ns}",
            "source_id": src,
            "target_id": tgt,
            "relation_type_raw": rt,
            "relation_type_canonical": edge["relation_type_canonical"],
            "relation_family": edge["relation_family"],
            "source_namespace": ns,
            "is_lineage": bool(edge.get("is_lineage", False)),
        })

    elapsed_ms = int((perf_counter() - started_at) * 1000)
    meta = {
        **(traversal.get("meta") or {}),
        "nodes_returned": len(node_payload),
        "edges_returned": len(edge_payload),
        "elapsed_ms": elapsed_ms,
    }
    logger.debug("graph/query seeds=%d depth=%d nodes=%d edges=%d ms=%d",
                 len(req.seed_ids), req.max_depth, len(node_payload), len(edge_payload), elapsed_ms)

    return {"nodes": node_payload, "edges": edge_payload, "meta": meta}


# ---------------------------------------------------------------------------
# Quick subgraph for a single artifact
# ---------------------------------------------------------------------------

@router.get("/artifact/{artifact_id}")
def artifact_subgraph(
    artifact_id: UUID,
    max_depth: int = Query(default=2, ge=1, le=10),
    include_lineage: bool = True,
    include_relations: bool = True,
    direction: str = "both",
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    """
    Convenience wrapper — graph query seeded from a single artifact.
    Equivalent to POST /graph/query with seed_ids=[artifact_id].
    """
    req = GraphQueryRequest(
        seed_ids=[artifact_id],
        max_depth=max_depth,
        direction=direction,
        include_lineage=include_lineage,
        include_relations=include_relations,
    )
    return graph_query(req, session=session, auth=auth)


# ---------------------------------------------------------------------------
# Relation CRUD
# ---------------------------------------------------------------------------

@router.post("/relations", response_model=RelationResponse, status_code=201)
def create_relation(
    rel: RelationCreate,
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    """
    Create a typed relation edge between two artifacts.

    If an identical edge (same source, target, relation_type, source_namespace)
    already exists, returns the existing edge with 200 instead of creating a duplicate.
    """
    _ensure_artifact_access(session, rel.source_id, auth, for_write=True)
    _ensure_artifact_access(session, rel.target_id, auth)

    existing = session.exec(
        select(ArtifactRelation).where(
            ArtifactRelation.source_id == rel.source_id,
            ArtifactRelation.target_id == rel.target_id,
            ArtifactRelation.relation_type == rel.relation_type,
            ArtifactRelation.source_namespace == rel.source_namespace,
        )
    ).first()

    if existing:
        return _relation_to_response(existing)

    new_rel = ArtifactRelation(
        id=uuid4(),
        source_id=rel.source_id,
        target_id=rel.target_id,
        relation_type=rel.relation_type,
        source_namespace=rel.source_namespace,
        created_at=datetime.now(timezone.utc),
        weight=rel.weight,
        metadata_json=rel.metadata_json or {},
    )
    session.add(new_rel)
    try:
        session.commit()
        session.refresh(new_rel)
    except IntegrityError:
        session.rollback()
        # Race condition — another request inserted first, return it
        existing = session.exec(
            select(ArtifactRelation).where(
                ArtifactRelation.source_id == rel.source_id,
                ArtifactRelation.target_id == rel.target_id,
                ArtifactRelation.relation_type == rel.relation_type,
                ArtifactRelation.source_namespace == rel.source_namespace,
            )
        ).first()
        if existing:
            return _relation_to_response(existing)
        raise

    return _relation_to_response(new_rel)


@router.get("/relations", response_model=List[RelationResponse])
def list_relations(
    source_id: Optional[UUID] = Query(default=None),
    target_id: Optional[UUID] = Query(default=None),
    relation_type: Optional[str] = Query(default=None),
    source_namespace: Optional[str] = Query(default=None),
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    """
    List relation edges with optional filters.

    At least one of source_id or target_id is recommended for performance.
    """
    query = select(ArtifactRelation)

    if source_id is not None:
        _ensure_artifact_access(session, source_id, auth)
        query = query.where(ArtifactRelation.source_id == source_id)
    if target_id is not None:
        _ensure_artifact_access(session, target_id, auth)
        query = query.where(ArtifactRelation.target_id == target_id)
    if relation_type is not None:
        query = query.where(ArtifactRelation.relation_type == relation_type)
    if source_namespace is not None:
        query = query.where(ArtifactRelation.source_namespace == source_namespace)

    query = query.order_by(ArtifactRelation.created_at.desc()).offset(offset).limit(limit)
    relations = session.exec(query).all()
    return [_relation_to_response(r) for r in relations]


@router.get("/relations/{relation_id}", response_model=RelationResponse)
def get_relation(
    relation_id: UUID,
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    """Fetch a single relation edge by its UUID."""
    rel = session.get(ArtifactRelation, relation_id)
    if not rel:
        raise HTTPException(status_code=404, detail="Relation not found")
    _ensure_artifact_access(session, rel.source_id, auth)
    return _relation_to_response(rel)


@router.patch("/relations/{relation_id}", response_model=RelationResponse)
def update_relation(
    relation_id: UUID,
    update: RelationUpdate,
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    """Update the weight or metadata_json of a relation edge."""
    rel = session.get(ArtifactRelation, relation_id)
    if not rel:
        raise HTTPException(status_code=404, detail="Relation not found")
    _ensure_artifact_access(session, rel.source_id, auth, for_write=True)

    if update.weight is not None:
        rel.weight = update.weight
    if update.metadata_json is not None:
        rel.metadata_json = update.metadata_json

    session.add(rel)
    session.commit()
    session.refresh(rel)
    return _relation_to_response(rel)


@router.delete("/relations/{relation_id}", status_code=204)
def delete_relation(
    relation_id: UUID,
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    """Delete a relation edge by its UUID."""
    rel = session.get(ArtifactRelation, relation_id)
    if not rel:
        raise HTTPException(status_code=404, detail="Relation not found")
    _ensure_artifact_access(session, rel.source_id, auth, for_write=True)
    session.delete(rel)
    session.commit()


# ---------------------------------------------------------------------------
# Relation type registry
# ---------------------------------------------------------------------------

@router.get("/relation-types")
def list_relation_types(
    registered_by: Optional[str] = Query(default=None),
    family: Optional[str] = Query(default=None),
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    """
    List all registered relation types (core + plugin-registered).
    Optionally filter by registered_by prefix or family.
    """
    _ = auth
    query = select(RelationTypeDef)
    if registered_by is not None:
        query = query.where(RelationTypeDef.registered_by == registered_by)
    if family is not None:
        query = query.where(RelationTypeDef.family == family)
    query = query.order_by(RelationTypeDef.family, RelationTypeDef.name)
    rows = session.exec(query).all()
    return [
        {
            "name": r.name,
            "family": r.family,
            "inverse": r.inverse,
            "transitive": r.transitive,
            "structural": r.structural,
            "description": r.description,
            "registered_by": r.registered_by,
        }
        for r in rows
    ]


@router.post("/relation-types", status_code=201)
def register_relation_type(
    spec: RelationTypeCreate,
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    """
    Register a new relation type. Admin or plugin use.
    Returns the existing record if the name is already registered.
    """
    if not (auth.is_admin or getattr(auth, "is_root", False) or auth.is_open):
        raise HTTPException(status_code=403, detail="Admin required")

    existing = session.get(RelationTypeDef, spec.name)
    if existing:
        return {"status": "exists", "name": existing.name}

    row = RelationTypeDef(
        name=spec.name,
        family=spec.family,
        inverse=spec.inverse,
        transitive=spec.transitive,
        structural=spec.structural,
        description=spec.description,
        registered_by=spec.registered_by,
    )
    session.add(row)
    session.commit()
    return {"status": "created", "name": row.name}
