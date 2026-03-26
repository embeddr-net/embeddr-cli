"""Provenance service — assembles the creation story for an artifact.

Given an artifact ID, returns a structured provenance chain showing:
- What created it (execution info)
- What was used to make it (parent/input artifacts)
- What was made from it (child/output artifacts)
- Semantic relations (generates, reference, etc.)
- Brand info resolved from source_namespace via SourceBrandRegistry
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from uuid import UUID

from pydantic import BaseModel, Field
from sqlmodel import Session, select

from embeddr_core.models.artifact import Artifact
from embeddr_core.models.artifact_lineage import ArtifactLineage
from embeddr_core.models.artifact_relation import ArtifactRelation
from embeddr_core.models.artifact_execution import ArtifactExecution
from embeddr_core.models.artifact_annotation import ArtifactAnnotation

logger = logging.getLogger("embeddr.services.provenance")


# ── Response models ──────────────────────────────────────

class ProvenanceSource(BaseModel):
    namespace: str
    name: str
    icon_url: Optional[str] = None


class ProvenanceArtifactSummary(BaseModel):
    id: str
    type_name: str
    base_type_name: Optional[str] = None
    name: Optional[str] = None
    uri: Optional[str] = None
    created_at: Optional[str] = None


class ProvenanceExecution(BaseModel):
    id: str
    type: str
    plugin_name: str
    status: str
    created_at: Optional[str] = None
    finished_at: Optional[str] = None


class ProvenanceStep(BaseModel):
    artifact: ProvenanceArtifactSummary
    role: str  # "input", "output", "related"
    relation_type: Optional[str] = None
    source: Optional[ProvenanceSource] = None
    execution: Optional[ProvenanceExecution] = None
    depth: int = 1


class ProvenanceAnnotation(BaseModel):
    """An annotation relevant to provenance — e.g. workflow inputs, generation params."""
    id: str
    annotation_type: str
    plugin_name: Optional[str] = None
    text: Optional[str] = None
    data: Optional[Dict[str, Any]] = None  # parsed JSON if text is JSON
    confidence: Optional[float] = None


class ProvenanceResponse(BaseModel):
    artifact: ProvenanceArtifactSummary
    sources: List[ProvenanceSource] = Field(default_factory=list)
    creation_execution: Optional[ProvenanceExecution] = None
    inputs: List[ProvenanceStep] = Field(default_factory=list)
    outputs: List[ProvenanceStep] = Field(default_factory=list)
    relations: List[ProvenanceStep] = Field(default_factory=list)
    annotations: List[ProvenanceAnnotation] = Field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────

def _summarize_artifact(art: Artifact) -> ProvenanceArtifactSummary:
    meta = art.metadata_json or {}
    name = (
        getattr(art, "name", None)
        or meta.get("name")
        or meta.get("filename")
        or meta.get("title")
    )
    return ProvenanceArtifactSummary(
        id=str(art.id),
        type_name=art.type_name,
        base_type_name=art.base_type_name,
        name=name,
        uri=art.uri,
        created_at=art.created_at.isoformat() if art.created_at else None,
    )


def _summarize_execution(exe: ArtifactExecution) -> ProvenanceExecution:
    return ProvenanceExecution(
        id=str(exe.id),
        type=exe.type,
        plugin_name=exe.plugin_name,
        status=exe.status,
        created_at=exe.created_at.isoformat() if exe.created_at else None,
        finished_at=exe.finished_at.isoformat() if exe.finished_at else None,
    )


def _resolve_brand(namespace: str) -> ProvenanceSource:
    try:
        from embeddr_core.services.source_brand_registry import get_source_brand_registry
        brand = get_source_brand_registry().resolve_or_default(namespace)
        return ProvenanceSource(
            namespace=namespace,
            name=brand.name,
            icon_url=brand.icon_url,
        )
    except Exception:
        return ProvenanceSource(namespace=namespace, name=namespace)


# Relation types that represent provenance / creation, not structural containment
_PROVENANCE_RELATION_TYPES = {
    "generates", "generated_by", "reference", "part_of",
    "variant_of", "derived_from", "style_reference",
}

_STRUCTURAL_RELATION_TYPES = {
    "contains", "contained_in", "group", "member_of",
}


# ── Main service function ────────────────────────────────

def get_artifact_provenance(
    session: Session,
    artifact_id: UUID,
    *,
    max_depth: int = 2,
) -> ProvenanceResponse:
    """Build the provenance chain for an artifact."""

    artifact = session.get(Artifact, artifact_id)
    if not artifact:
        raise ValueError(f"Artifact not found: {artifact_id}")

    response = ProvenanceResponse(
        artifact=_summarize_artifact(artifact),
    )

    seen_namespaces: set[str] = set()

    # ── 1. Lineage parents (input artifacts) ──
    parent_lineage = session.exec(
        select(ArtifactLineage).where(
            ArtifactLineage.child_id == artifact_id
        )
    ).all()

    for lineage in parent_lineage:
        parent = session.get(Artifact, lineage.parent_id)
        if not parent:
            continue

        # Get the execution that created this lineage link
        execution = None
        if lineage.execution_id:
            exe = session.get(ArtifactExecution, lineage.execution_id)
            if exe:
                execution = _summarize_execution(exe)

        # Find source_namespace from relations between these artifacts
        edge = session.exec(
            select(ArtifactRelation).where(
                ArtifactRelation.source_id == lineage.parent_id,
                ArtifactRelation.target_id == artifact_id,
            )
        ).first()
        source = None
        if edge and edge.source_namespace:
            source = _resolve_brand(edge.source_namespace)
            seen_namespaces.add(edge.source_namespace)

        response.inputs.append(ProvenanceStep(
            artifact=_summarize_artifact(parent),
            role="input",
            relation_type=edge.relation_type if edge else "lineage",
            source=source,
            execution=execution,
        ))

    # ── 2. Lineage children (output artifacts) ──
    child_lineage = session.exec(
        select(ArtifactLineage).where(
            ArtifactLineage.parent_id == artifact_id
        )
    ).all()

    for lineage in child_lineage:
        child = session.get(Artifact, lineage.child_id)
        if not child:
            continue

        execution = None
        if lineage.execution_id:
            exe = session.get(ArtifactExecution, lineage.execution_id)
            if exe:
                execution = _summarize_execution(exe)

        edge = session.exec(
            select(ArtifactRelation).where(
                ArtifactRelation.source_id == artifact_id,
                ArtifactRelation.target_id == lineage.child_id,
            )
        ).first()
        source = None
        if edge and edge.source_namespace:
            source = _resolve_brand(edge.source_namespace)
            seen_namespaces.add(edge.source_namespace)

        response.outputs.append(ProvenanceStep(
            artifact=_summarize_artifact(child),
            role="output",
            relation_type=edge.relation_type if edge else "lineage",
            source=source,
            execution=execution,
        ))

    # ── 3. Semantic relations (non-structural, non-lineage) ──
    lineage_parent_ids = {l.parent_id for l in parent_lineage}
    lineage_child_ids = {l.child_id for l in child_lineage}

    # Relations where this artifact is the source
    outgoing = session.exec(
        select(ArtifactRelation).where(
            ArtifactRelation.source_id == artifact_id,
        )
    ).all()

    for rel in outgoing:
        if rel.relation_type in _STRUCTURAL_RELATION_TYPES:
            continue
        if rel.target_id in lineage_child_ids:
            continue  # Already captured in outputs

        target = session.get(Artifact, rel.target_id)
        if not target:
            continue

        source = None
        if rel.source_namespace:
            source = _resolve_brand(rel.source_namespace)
            seen_namespaces.add(rel.source_namespace)

        response.relations.append(ProvenanceStep(
            artifact=_summarize_artifact(target),
            role="related",
            relation_type=rel.relation_type,
            source=source,
        ))

    # Relations where this artifact is the target
    incoming = session.exec(
        select(ArtifactRelation).where(
            ArtifactRelation.target_id == artifact_id,
        )
    ).all()

    for rel in incoming:
        if rel.relation_type in _STRUCTURAL_RELATION_TYPES:
            continue
        if rel.source_id in lineage_parent_ids:
            continue  # Already captured in inputs

        src_art = session.get(Artifact, rel.source_id)
        if not src_art:
            continue

        source = None
        if rel.source_namespace:
            source = _resolve_brand(rel.source_namespace)
            seen_namespaces.add(rel.source_namespace)

        response.relations.append(ProvenanceStep(
            artifact=_summarize_artifact(src_art),
            role="related",
            relation_type=rel.relation_type,
            source=source,
        ))

    # ── 4. Find the execution that created this artifact ──
    # Look for executions linked to this artifact
    try:
        creating_exe = session.exec(
            select(ArtifactExecution).where(
                ArtifactExecution.primary_artifact_id == artifact_id,
                ArtifactExecution.status == "completed",
            ).order_by(ArtifactExecution.created_at.desc())
        ).first()
        if creating_exe:
            response.creation_execution = _summarize_execution(creating_exe)
    except Exception:
        pass

    # ── 5. Annotations (workflow inputs, generation params, captions) ──
    _PROVENANCE_ANNOTATION_TYPES = {
        "comfyui:workflow_inputs",
        "generation_params",
        "caption",
        "prompt",
        "summary",
    }
    try:
        annotations = session.exec(
            select(ArtifactAnnotation).where(
                ArtifactAnnotation.artifact_id == artifact_id,
            )
        ).all()

        for ann in annotations:
            # Include provenance-relevant annotations, skip internal ones
            if ann.annotation_type not in _PROVENANCE_ANNOTATION_TYPES:
                # Still include any annotation from a known plugin
                if not ann.plugin_name:
                    continue

            parsed_data = None
            if ann.text:
                try:
                    import json
                    parsed_data = json.loads(ann.text)
                except (json.JSONDecodeError, TypeError):
                    pass

            response.annotations.append(ProvenanceAnnotation(
                id=str(ann.id),
                annotation_type=ann.annotation_type,
                plugin_name=ann.plugin_name,
                text=ann.text if not parsed_data else None,
                data=parsed_data,
                confidence=ann.confidence,
            ))

            # Track the annotation's plugin as a source
            if ann.plugin_name:
                seen_namespaces.add(ann.plugin_name)
    except Exception as e:
        logger.debug("Failed to load annotations for provenance: %s", e)

    # ── 6. Collect all source brands ──
    # Also check the artifact's own metadata for source info
    meta = artifact.metadata_json or {}
    for key in ("source", "origin"):
        if isinstance(meta.get(key), str) and meta[key].strip():
            seen_namespaces.add(meta[key].strip())

    response.sources = [_resolve_brand(ns) for ns in sorted(seen_namespaces)]

    return response
