from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Literal, Optional, Set, Tuple
from uuid import UUID

from sqlalchemy import or_
from sqlmodel import Session, select

from embeddr_core.graph_semantics import (
    normalize_namespace_group,
    relation_semantics_tuple,
    relation_taxonomy,
)
from embeddr_core.models.artifact_lineage import ArtifactLineage
from embeddr_core.models.artifact_relation import ArtifactRelation


GraphDirection = Literal["incoming", "outgoing", "both"]


@dataclass
class GraphQueryFilters:
    relation_types_include: Set[str] = field(default_factory=set)
    relation_types_exclude: Set[str] = field(default_factory=set)
    relation_families_include: Set[str] = field(default_factory=set)
    source_namespaces_include: Set[str] = field(default_factory=set)
    source_namespaces_exclude: Set[str] = field(default_factory=set)
    artifact_types_include: Set[str] = field(default_factory=set)
    artifact_base_types_include: Set[str] = field(default_factory=set)
    include_legacy_stash_contains: bool = False

    @staticmethod
    def _clean(values: Optional[Iterable[str]]) -> Set[str]:
        if not values:
            return set()
        return {str(v).strip() for v in values if str(v).strip()}

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "GraphQueryFilters":
        value = raw or {}
        return cls(
            relation_types_include=cls._clean(value.get("relation_types_include")),
            relation_types_exclude=cls._clean(value.get("relation_types_exclude")),
            relation_families_include=cls._clean(value.get("relation_families_include")),
            source_namespaces_include=cls._clean(value.get("source_namespaces_include")),
            source_namespaces_exclude=cls._clean(value.get("source_namespaces_exclude")),
            artifact_types_include=cls._clean(value.get("artifact_types_include")),
            artifact_base_types_include=cls._clean(
                value.get("artifact_base_types_include")
            ),
            include_legacy_stash_contains=bool(
                value.get("include_legacy_stash_contains", False)
            ),
        )


def _passes_relation_filters(
    relation_type_raw: str,
    relation_family: str,
    source_namespace: str,
    filters: GraphQueryFilters,
) -> bool:
    if (
        not filters.include_legacy_stash_contains
        and relation_type_raw == "contains"
        and source_namespace == "plugin:stash"
    ):
        return False

    if filters.relation_types_include and relation_type_raw not in filters.relation_types_include:
        return False
    if relation_type_raw in filters.relation_types_exclude:
        return False

    if filters.relation_families_include and relation_family not in filters.relation_families_include:
        return False

    if filters.source_namespaces_include and source_namespace not in filters.source_namespaces_include:
        return False
    if source_namespace in filters.source_namespaces_exclude:
        return False

    return True


def _relation_query(
    session: Session,
    frontier_ids: List[UUID],
    direction: GraphDirection,
) -> List[ArtifactRelation]:
    where_parts = []
    if direction in ("outgoing", "both"):
        where_parts.append(ArtifactRelation.source_id.in_(frontier_ids))
    if direction in ("incoming", "both"):
        where_parts.append(ArtifactRelation.target_id.in_(frontier_ids))
    if not where_parts:
        return []
    return session.exec(select(ArtifactRelation).where(or_(*where_parts))).all()


def _lineage_query(
    session: Session,
    frontier_ids: List[UUID],
    direction: GraphDirection,
) -> List[ArtifactLineage]:
    where_parts = []
    if direction in ("outgoing", "both"):
        where_parts.append(ArtifactLineage.parent_id.in_(frontier_ids))
    if direction in ("incoming", "both"):
        where_parts.append(ArtifactLineage.child_id.in_(frontier_ids))
    if not where_parts:
        return []
    return session.exec(select(ArtifactLineage).where(or_(*where_parts))).all()


def run_graph_bfs(
    *,
    session: Session,
    seed_ids: List[UUID],
    max_depth: int,
    direction: GraphDirection,
    include_lineage: bool,
    include_relations: bool,
    filters: GraphQueryFilters,
    limit_nodes: int,
    limit_edges: int,
) -> Dict[str, Any]:
    max_depth = max(0, int(max_depth))
    limit_nodes = max(1, min(int(limit_nodes), 1500))
    limit_edges = max(1, min(int(limit_edges), 6000))

    visited_nodes: Set[UUID] = set(seed_ids)
    frontier: Set[UUID] = set(seed_ids)
    edge_keys: Set[Tuple[str, str, str, str, bool]] = set()
    edges: List[Dict[str, Any]] = []
    truncated = False
    truncation_reason: Optional[str] = None

    def _track_node(node_id: UUID, next_frontier: Set[UUID]) -> bool:
        nonlocal truncated, truncation_reason
        if node_id in visited_nodes:
            return True
        if len(visited_nodes) >= limit_nodes:
            truncated = True
            truncation_reason = truncation_reason or "limit_nodes_reached"
            return False
        visited_nodes.add(node_id)
        next_frontier.add(node_id)
        return True

    def _track_edge(edge: Dict[str, Any]) -> bool:
        nonlocal truncated, truncation_reason
        key = (
            str(edge["source_id"]),
            str(edge["target_id"]),
            str(edge["relation_type_raw"]),
            str(edge["source_namespace"]),
            bool(edge["is_lineage"]),
        )
        if key in edge_keys:
            return True
        if len(edges) >= limit_edges:
            truncated = True
            truncation_reason = truncation_reason or "limit_edges_reached"
            return False
        edge_keys.add(key)
        edges.append(edge)
        return True

    for _depth in range(max_depth):
        if not frontier or truncated:
            break

        frontier_list = list(frontier)
        next_frontier: Set[UUID] = set()

        if include_relations:
            relations = _relation_query(session, frontier_list, direction)
            for rel in relations:
                relation_type_raw = str(rel.relation_type or "unknown")
                source_namespace = str(rel.source_namespace or "")
                relation_type_canonical, relation_family = relation_semantics_tuple(
                    relation_type_raw
                )
                if not _passes_relation_filters(
                    relation_type_raw,
                    relation_family,
                    source_namespace,
                    filters,
                ):
                    continue

                edge = {
                    "source_id": rel.source_id,
                    "target_id": rel.target_id,
                    "relation_type_raw": relation_type_raw,
                    "relation_type_canonical": relation_type_canonical,
                    "relation_family": relation_family,
                    "source_namespace": source_namespace,
                    "is_lineage": False,
                }
                if not _track_edge(edge):
                    break

                _track_node(rel.source_id, next_frontier)
                _track_node(rel.target_id, next_frontier)

        if include_lineage and not truncated:
            lineage_rows = _lineage_query(session, frontier_list, direction)
            for lineage in lineage_rows:
                edge = {
                    "source_id": lineage.parent_id,
                    "target_id": lineage.child_id,
                    "relation_type_raw": "lineage",
                    "relation_type_canonical": "lineage",
                    "relation_family": "lineage",
                    "source_namespace": "lineage",
                    "is_lineage": True,
                }
                if not _track_edge(edge):
                    break

                _track_node(lineage.parent_id, next_frontier)
                _track_node(lineage.child_id, next_frontier)

        frontier = next_frontier

    return {
        "node_ids": visited_nodes,
        "edges": edges,
        "meta": {
            "truncated": truncated,
            "truncation_reason": truncation_reason,
        },
    }


def get_graph_taxonomy_payload(session: Session) -> Dict[str, Any]:
    relation_rows = session.exec(select(ArtifactRelation.relation_type).distinct()).all()
    namespace_rows = session.exec(
        select(ArtifactRelation.source_namespace).distinct()
    ).all()

    observed_relation_types = sorted({str(v) for v in relation_rows if v})
    namespaces = sorted({str(v) for v in namespace_rows if v})

    # Pass session so taxonomy includes DB-registered plugin types
    taxonomy = relation_taxonomy(observed_relation_types, session=session)

    group_map: Dict[str, List[str]] = {}
    for namespace in namespaces:
        group = normalize_namespace_group(namespace)
        group_map.setdefault(group, []).append(namespace)

    namespace_groups = [
        {"group": key, "namespaces": sorted(values)}
        for key, values in sorted(group_map.items(), key=lambda item: item[0])
    ]

    # Include registered source brands from plugin-driven registry
    try:
        from embeddr_core.services.source_brand_registry import get_source_brand_registry
        source_brands = get_source_brand_registry().to_dict()
    except Exception:
        source_brands = {}

    return {
        "relation_families": taxonomy["families"],
        "relation_types": taxonomy["types"],
        "source_namespaces": namespaces,
        "namespace_groups": namespace_groups,
        "source_brands": source_brands,
    }
