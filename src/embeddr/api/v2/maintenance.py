from typing import List, Optional
from uuid import UUID
import os
from pathlib import Path
import importlib.util

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select, col, func, or_, literal, delete

from embeddr.db.session import get_engine
from embeddr_core.models.artifact import Artifact
from embeddr_core.models.artifact_relation import ArtifactRelation
from embeddr_core.models.artifact_embedding import ArtifactEmbedding
from embeddr_core.models.artifact_feature import ArtifactFeatureRef
from embeddr_core.models.artifact_annotation import ArtifactAnnotation
from embeddr_core.models.artifact_lineage import ArtifactLineage
from embeddr_core.models.artifact import ArtifactPreview
from pydantic import BaseModel

router = APIRouter()


def get_session():
    engine = get_engine()
    with Session(engine) as session:
        yield session


class OrphanItem(BaseModel):
    id: UUID
    uri: Optional[str]
    type: str
    metadata: dict
    reason: str  # "db_orphan" or "missing_file"


class ScriptInfo(BaseModel):
    name: str
    description: str = ""


class ScriptRunRequest(BaseModel):
    dryRun: bool = True


# Define scripts directory (Standardized location)
SCRIPTS_DIR = Path("/home/user/git/embeddr-net/embeddr-scripts/maintenance")


@router.get("/scripts", response_model=List[ScriptInfo])
def list_scripts():
    """List available maintenance scripts."""
    if not SCRIPTS_DIR.exists():
        return []

    scripts = []
    for f in SCRIPTS_DIR.glob("*.py"):
        if f.name.startswith("__"):
            continue

        desc = ""
        try:
            # Load module to get DESCRIPTION
            spec = importlib.util.spec_from_file_location(f.stem, f)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                desc = getattr(module, "DESCRIPTION", "")
        except Exception:
            desc = "Error loading description"
            # In case of error, still list the script

        scripts.append(ScriptInfo(name=f.name, description=desc))
    return scripts


@router.post("/scripts/run")
def run_script(script_name: str, dry_run: bool = True):
    """Run a maintenance script."""
    script_path = SCRIPTS_DIR / script_name
    if not script_path.exists():
        raise HTTPException(404, "Script not found")

    try:
        spec = importlib.util.spec_from_file_location(
            "maint_script", script_path)
        if not spec or not spec.loader:
            raise HTTPException(500, "Could not load script spec")

        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        if not hasattr(module, "run"):
            raise HTTPException(400, "Script does not have 'run' function")

        # Execute
        result = module.run(dry_run=dry_run)
        return result
    except Exception as e:
        import traceback
        traceback.print_exc()
        raise HTTPException(500, f"Script execution failed: {str(e)}")


@router.post("/scripts/{script_name}")
def run_script_by_name(script_name: str, payload: ScriptRunRequest):
    """Run a maintenance script by name (preferred route)."""
    return run_script(script_name=script_name, dry_run=payload.dryRun)


@router.get("/orphans", response_model=List[OrphanItem])
def get_db_orphans(limit: int = 100, session: Session = Depends(get_session)):
    """
    Identify DB orphans: Artifacts that are not connected to any other artifact
    (not contained in anything, not containing anything).
    We exclude 'collection' types as they are often root nodes.
    """

    # Subquery for items that are contained in something
    subq_inside = select(ArtifactRelation.source_id).where(
        ArtifactRelation.relation_type.in_(["contained_in", "member_of"])
    )

    # Subquery for items that contain something (parents)
    subq_contains = select(ArtifactRelation.target_id).where(
        ArtifactRelation.relation_type.in_(["contains", "group"])
    )

    # Find items that are NOT in these subqueries
    stmt = select(Artifact).where(
        Artifact.id.not_in(subq_inside),
        Artifact.id.not_in(subq_contains),
        Artifact.type_name != "collection",
        Artifact.base_type_name != "collection"
    ).limit(limit)

    db_orphans = session.exec(stmt).all()

    results = []
    for art in db_orphans:
        results.append(OrphanItem(
            id=art.id,
            uri=art.uri,
            type=art.base_type_name,
            metadata=art.metadata_json or {},
            reason="db_orphan"
        ))

    return results


@router.post("/scan_missing", response_model=List[OrphanItem])
def scan_missing_files(limit: int = 100, session: Session = Depends(get_session)):
    """
    Scans for artifacts where the file on disk is missing.
    Iterates through artifacts with a URI and checks existence.
    """
    stmt = select(Artifact).where(Artifact.uri != None)

    # Use yield_per if supported by driver, otherwise fetchall might be big
    # SQLite support for yield_per is varying, but let's try just standard iteration
    query = session.exec(stmt)
    results = []

    scanned = 0
    # Safety limit for scan duration
    MAX_SCAN = 10000

    for art in query:
        scanned += 1
        if scanned > MAX_SCAN:
            break

        if art.uri and not art.uri.startswith("http") and not art.uri.startswith("https"):
            # Check local file
            if not os.path.exists(art.uri):
                results.append(OrphanItem(
                    id=art.id,
                    uri=art.uri,
                    type=art.base_type_name,
                    metadata=art.metadata_json or {},
                    reason="missing_file"
                ))
                if len(results) >= limit:
                    break

    return results


@router.post("/fix_types")
def fix_artifact_types(limit: int = 1000, session: Session = Depends(get_session)):
    """
    Scans artifacts and fixes type_name/base_type_name based on file extension.
    Useful for fixing initial ingestion errors where videos were marked as images/files.
    """
    # Fetch artifacts that are generic 'file' or 'image' which might be videos
    # Or just scan everything with a URI
    stmt = select(Artifact).where(Artifact.uri != None).limit(limit)
    artifacts = session.exec(stmt).all()

    updated_count = 0
    VIDEO_EXTS = {'.webm', '.mp4', '.mkv', '.mov', '.avi'}
    IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.bmp', '.tiff'}

    for art in artifacts:
        if not art.uri:
            continue

        path_lower = art.uri.lower()
        new_type = None
        new_base = None

        if any(path_lower.endswith(ext) for ext in VIDEO_EXTS):
            new_type = "video"
            new_base = "video"
        elif any(path_lower.endswith(ext) for ext in IMAGE_EXTS):
            new_type = "image"
            new_base = "image"

        # Apply update if different
        # We generally trust the extension over the DB for these basic types
        updated = False
        if new_type and art.type_name != new_type:
            art.type_name = new_type
            updated = True

        if new_base and art.base_type_name != new_base:
            art.base_type_name = new_base
            updated = True

        if updated:
            session.add(art)
            updated_count += 1

    session.commit()
    return {"updated": updated_count, "scanned": len(artifacts)}


@router.post("/prune")
def prune_artifacts(ids: List[UUID], session: Session = Depends(get_session)):
    """
    Permanently delete the specified artifacts and their related data.
    """
    if not ids:
        return {"deleted": 0}

    # Delete related entities
    session.exec(delete(ArtifactEmbedding).where(
        ArtifactEmbedding.artifact_id.in_(ids)))
    session.exec(delete(ArtifactFeatureRef).where(
        ArtifactFeatureRef.artifact_id.in_(ids)))
    session.exec(delete(ArtifactAnnotation).where(
        ArtifactAnnotation.artifact_id.in_(ids)))
    session.exec(delete(ArtifactPreview).where(
        ArtifactPreview.artifact_id.in_(ids)))
    session.exec(delete(ArtifactRelation).where(
        or_(
            ArtifactRelation.source_id.in_(ids),
            ArtifactRelation.target_id.in_(ids)
        )
    ))
    session.exec(delete(ArtifactLineage).where(
        ArtifactLineage.artifact_id.in_(ids)))
    session.exec(delete(ArtifactLineage).where(
        ArtifactLineage.ancestor_id.in_(ids)))

    # Delete artifacts
    stmt = select(Artifact).where(Artifact.id.in_(ids))
    items = session.exec(stmt).all()
    count = 0
    for item in items:
        session.delete(item)
        count += 1

    session.commit()
    return {"deleted": count}
