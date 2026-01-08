from typing import List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select, col
from embeddr.db.session import get_engine
from embeddr_core.models.artifact import Artifact
from embeddr_core.models.artifact_embedding import ArtifactEmbedding
from embeddr_core.models.artifact_annotation import ArtifactAnnotation
from embeddr_core.models.artifact_lineage import ArtifactLineage
from embeddr_core.models.artifact_relation import ArtifactRelation

router = APIRouter()


def get_session():
    engine = get_engine()
    with Session(engine) as session:
        yield session


@router.get("/", response_model=List[Artifact])
def list_artifacts(
    limit: int = 50,
    offset: int = 0,
    type_name: Optional[str] = None,
    session: Session = Depends(get_session)
):
    """List all artifacts with optional filtering."""
    query = select(Artifact)
    if type_name:
        query = query.where(Artifact.type_name == type_name)

    query = query.offset(offset).limit(limit)
    return session.exec(query).all()


@router.get("/{artifact_id}", response_model=Artifact)
def get_artifact(artifact_id: UUID, session: Session = Depends(get_session)):
    """Retrieve a single artifact."""
    artifact = session.get(Artifact, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return artifact


@router.get("/{artifact_id}/embeddings", response_model=List[ArtifactEmbedding])
def get_artifact_embeddings(artifact_id: UUID, session: Session = Depends(get_session)):
    """Retrieve embeddings for an artifact."""
    query = select(ArtifactEmbedding).where(
        ArtifactEmbedding.artifact_id == artifact_id)
    return session.exec(query).all()


@router.get("/{artifact_id}/annotations", response_model=List[ArtifactAnnotation])
def get_artifact_annotations(artifact_id: UUID, session: Session = Depends(get_session)):
    """Retrieve annotations (captions, etc) for an artifact."""
    query = select(ArtifactAnnotation).where(
        ArtifactAnnotation.artifact_id == artifact_id)
    return session.exec(query).all()


@router.get("/{artifact_id}/lineage")
def get_artifact_lineage(artifact_id: UUID, session: Session = Depends(get_session)):
    """Retrieve parent and child lineage for an artifact."""
    parents = session.exec(
        select(ArtifactLineage).where(ArtifactLineage.child_id == artifact_id)
    ).all()
    children = session.exec(
        select(ArtifactLineage).where(ArtifactLineage.parent_id == artifact_id)
    ).all()

    return {
        "parents": parents,
        "children": children
    }


@router.get("/{artifact_id}/relations", response_model=List[ArtifactRelation])
def get_artifact_relations(artifact_id: UUID, session: Session = Depends(get_session)):
    """Retrieve semantic relations for an artifact."""
    # Find where it is source OR target
    query = select(ArtifactRelation).where(
        (ArtifactRelation.source_id == artifact_id) |
        (ArtifactRelation.target_id == artifact_id)
    )
    return session.exec(query).all()
