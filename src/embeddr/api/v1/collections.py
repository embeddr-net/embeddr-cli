from typing import List, Optional, Dict, Any
from uuid import uuid4, UUID
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from pydantic import BaseModel, Field
from sqlalchemy.orm import aliased
from sqlmodel import Session, select, func
from embeddr.db.session import get_session
from embeddr_core.models.artifact import Artifact
from embeddr_core.models.artifact_type import ArtifactType
from embeddr_core.models.artifact_relation import ArtifactRelation
from embeddr_core.relations import STRUCTURAL_PARENT_TO_CHILD
from embeddr_core.services.scanner_registry import scanner_registry
# Ensure default scanners are registered
import embeddr_core.services.scanner  # noqa: F401
from embeddr.core.plugin_loader import get_lotus_registry
from embeddr_core.models.lotus import LotusKind
import logging
from embeddr.api.security import require_permission_for_request, get_auth_context

router = APIRouter(
    dependencies=[
        Depends(require_permission_for_request(
            "collections:read", "collections:write"))
    ]
)
logger = logging.getLogger(__name__)


def _ensure_artifact_type(
    session: Session,
    name: str,
    parent_name: Optional[str] = None,
    description: Optional[str] = None,
    default_capabilities: Optional[List[str]] = None,
) -> ArtifactType:
    existing = session.get(ArtifactType, name)
    if existing:
        return existing

    artifact_type = ArtifactType(
        name=name,
        parent_name=parent_name,
        description=description,
        default_capabilities=default_capabilities or [],
        metadata_json={},
    )
    session.add(artifact_type)
    session.commit()
    session.refresh(artifact_type)
    return artifact_type


class CollectionCreateRequest(BaseModel):
    uri: str
    type_name: str  # e.g. "collection:directory"
    label: str
    scanner_config: Dict[str, Any] = Field(default_factory=dict)
    recursive: bool = True


class CollectionUpdateRequest(BaseModel):
    label: Optional[str] = None
    scanner_config: Optional[Dict[str, Any]] = None


class CollectionResponse(BaseModel):
    id: str
    uri: str
    type_name: str
    label: str
    file_count: int
    metadata: Dict[str, Any] = {}


class ScannerTypeInfo(BaseModel):
    type_name: str
    display_name: str
    description: str
    required_config: Dict[str, Any] = {}


@router.get("/scanners", response_model=List[ScannerTypeInfo])
def list_available_scanners():
    scanners = []
    registry = get_lotus_registry()

    # Query for capabilities that declare themselves as scanners via slot
    # We accept both indexer (new ontology) and provider (legacy) kinds
    caps = registry.list(slot="collection.scanner")

    for cap in caps:
        # Filter unrelated kinds strictly? No, if it claims the slot, it's a scanner.
        if cap.kind not in {LotusKind.indexer, LotusKind.provider}:
            continue

        data = cap.data or {}
        type_name = data.get("scanner_type") or data.get("type_name")
        if not type_name:
            continue
        config_schema = data.get("config_schema") or {}
        scanners.append(ScannerTypeInfo(
            type_name=type_name,
            display_name=cap.title or f"Source for {type_name}",
            description=cap.description or "Indexes artifacts for this source type",
            required_config=config_schema
        ))

    if scanners:
        return scanners

    # Fallback: use registered scanners when no providers are registered
    keys = list(scanner_registry._scanners.keys())
    logger.info(f"Listing available scanners. Found {len(keys)}: {keys}")

    for type_name in keys:
        if type_name == "collection:mix":
            continue
        scanner = scanner_registry.get_scanner(type_name)
        if not scanner:
            continue
        config_schema = {}
        if hasattr(scanner, "get_config_schema"):
            try:
                config_schema = scanner.get_config_schema()
            except Exception as e:
                logger.error(
                    f"Error getting config schema for {type_name}: {e}")

        display = getattr(scanner, "display_name",
                          None) or f"Source for {type_name}"
        description = getattr(scanner, "description",
                              None) or "Indexes artifacts for this source type"

        scanners.append(ScannerTypeInfo(
            type_name=type_name,
            display_name=display,
            description=description,
            required_config=config_schema
        ))
    return scanners


@router.get("", response_model=List[CollectionResponse])
def list_collections(
    category: Optional[str] = None,
    session: Session = Depends(get_session)
):
    # Find all top-level collections (user created roots)
    # We want to return ALL collections now, distinguishing types in the response if needed
    stmt = select(Artifact).where(Artifact.base_type_name == "collection")
    roots = session.exec(stmt).all()

    resp = []
    for r in roots:
        if r is None:
            continue
        meta = r.metadata_json or {}

        # Filter based on category if provided
        src = meta.get("source", "unknown")
        is_library = (r.type_name == "collection:directory") or (
            src == "user_library")

        if category == "library" and not is_library:
            continue
        if category == "source" and is_library:
            continue

        # Use Recursive CTE to find all descendant artifacts (deep traversal)
        # 1. Base case: Immediate children
        hierarchy = select(ArtifactRelation.target_id).where(
            ArtifactRelation.source_id == r.id,
            ArtifactRelation.relation_type.in_(STRUCTURAL_PARENT_TO_CHILD)
        ).cte(recursive=True)

        # 2. Recursive step: Children of children
        parent_hierarchy = aliased(hierarchy, name="parent_hierarchy")
        child_relation = aliased(ArtifactRelation, name="child_relation")

        hierarchy = hierarchy.union_all(
            select(child_relation.target_id)
            .join(parent_hierarchy, child_relation.source_id == parent_hierarchy.c.target_id)
            .where(child_relation.relation_type.in_(STRUCTURAL_PARENT_TO_CHILD))
        )

        # 3. Count matching artifacts found in the hierarchy
        # Note: we filter by base_type directly on the Artifact table joined to the hierarchy
        count = session.exec(
            select(func.count(Artifact.id))
            .join(hierarchy, Artifact.id == hierarchy.c.target_id)
            .where(Artifact.base_type_name.in_(["image", "video"]))
        ).one()

        if r.metadata_json:
            src = r.metadata_json.get("source", "unknown")
            label_suffix = ""
            if r.type_name == "collection:directory":
                label_suffix = " (Library)"
            elif src == "user_library":  # Fallback for older items
                label_suffix = " (Library)"

        # Smart label resolution
        label = meta.get("label") or meta.get("post_title") or meta.get(
            "thread_title") or meta.get("name")
        if not label and r.uri:
            # Fallback to last part of URI if no label
            label = r.uri.split("/")[-1]
            # Handle website thread URIs slightly better if needed
            if label.startswith("res/"):
                label = label.replace(".html", "")

        label = label or "Untitled"

        resp.append(CollectionResponse(
            id=str(r.id),
            uri=r.uri or "",
            type_name=r.type_name,
            # + label_suffix # We let UI handle icons
            label=label,
            file_count=count,
            metadata=r.metadata_json or {}
        ))
    return resp


def background_scan(root_id: str, recursive: bool):
    from embeddr.db.session import get_engine
    with Session(get_engine()) as session:
        try:
            # Strip whitespace to prevent copy-paste errors
            clean_id = root_id.strip()
            root_uuid = UUID(clean_id)
        except ValueError:
            logger.error(f"Invalid UUID string provided for scan: {root_id}")
            return

        try:
            artifact = session.get(Artifact, root_uuid)

            if not artifact:
                logger.error(
                    f"Root artifact {root_id} not found, aborting scan.")
                return

            logger.info(
                f"Starting background scan for {root_id} ({artifact.uri})")

            # Lookup scanner
            scanner = scanner_registry.get_scanner(artifact.type_name)
            if not scanner:
                logger.warning(
                    f"No scanner registered for type: {artifact.type_name}")
                return

            logger.info(
                f"Using scanner: {scanner} (Class: {scanner.__class__.__name__}) for {artifact.type_name}")

            result = scanner.scan(session, artifact, recursive=recursive)

            # Handle both return types (int) or (int, list) for backward compat with other scanners
            if isinstance(result, tuple):
                added = result[0]
            else:
                added = result

            logger.info(
                f"Completed background scan for {root_id}. Added/Updated: {added}")

        except Exception as e:
            logger.exception(f"Error during background scan for {root_id}")


@router.post("", response_model=CollectionResponse)
def add_collection(
    req: CollectionCreateRequest,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    if not req.uri:
        raise HTTPException(status_code=422, detail="uri is required")
    if not req.type_name:
        raise HTTPException(status_code=422, detail="type_name is required")
    # Verify scanner exists for type
    if not scanner_registry.get_scanner(req.type_name):
        raise HTTPException(
            status_code=400, detail=f"No scanner available for type {req.type_name}")

    # Ensure artifact types exist to satisfy FK constraints
    if req.type_name == "collection:directory":
        _ensure_artifact_type(
            session,
            name="artifact",
            parent_name=None,
            description="Root artifact type",
            default_capabilities=[],
        )
        _ensure_artifact_type(
            session,
            name="collection",
            parent_name="artifact",
            description="Collection container",
            default_capabilities=["container"],
        )
        _ensure_artifact_type(
            session,
            name="collection:directory",
            parent_name="collection",
            description="Filesystem directory collection",
            default_capabilities=["container"],
        )

    # Check existing
    existing = session.exec(
        select(Artifact).where(Artifact.uri == req.uri)).first()

    if existing:
        # Update it to be a user collection root
        existing.type_name = req.type_name
        existing.metadata_json["label"] = req.label
        existing.metadata_json["source"] = "user_library"
        if existing.owner_user_id is None:
            existing.owner_user_id = auth.user_id
        if existing.owner_operator_id is None:
            existing.owner_operator_id = auth.operator_id

        # Merge config
        if req.scanner_config:
            existing.metadata_json["scanner_config"] = req.scanner_config

        session.add(existing)
        session.commit()
        session.refresh(existing)
        root_id = existing.id
    else:
        root_id = uuid4()
        art = Artifact(
            id=root_id,
            type_name=req.type_name,
            base_type_name="collection",
            uri=req.uri,
            metadata_json={
                "label": req.label,
                "source": "user_library",
                "scanner_config": req.scanner_config
            },
            owner_user_id=auth.user_id,
            owner_operator_id=auth.operator_id,
        )
        session.add(art)
        session.commit()
        session.refresh(art)  # refresh to get data for response
        existing = art

    # Trigger Scan
    from embeddr.core.execution_spine import ExecutionSpine
    ExecutionSpine.submit_job(
        job_type="collection.scan",
        inputs={"artifact_id": str(root_id), "recursive": req.recursive},
        plugin_name="embeddr-fs-scanner",
        resource_class="io",
        priority=5,
        trigger="user_api",
        primary_artifact_id=root_id
    )

    count = session.query(ArtifactRelation).filter(
        ArtifactRelation.source_id == root_id,
        ArtifactRelation.relation_type.in_(STRUCTURAL_PARENT_TO_CHILD)
    ).count()

    return CollectionResponse(
        id=str(root_id),
        uri=existing.uri or "",
        type_name=existing.type_name,
        label=existing.metadata_json.get("label", "Untitled"),
        file_count=count,
        metadata=existing.metadata_json or {}
    )


@router.post("/{root_id}/scan")
def rescan_collection(
    root_id: str,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session)
):
    try:
        root_uuid = UUID(root_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID")

    art = session.get(Artifact, root_uuid)
    if not art:
        raise HTTPException(status_code=404, detail="Root not found")

    from embeddr.core.execution_spine import ExecutionSpine
    job = ExecutionSpine.submit_job(
        job_type="collection.scan",
        inputs={"artifact_id": root_id, "recursive": True},
        plugin_name="embeddr-fs-scanner",
        resource_class="io",
        priority=5,
        trigger="user_api_rescan",
        primary_artifact_id=root_uuid
    )

    return {"status": "rescan_queued", "job_id": str(job.id)}


@router.patch("/{root_id}", response_model=CollectionResponse)
def update_collection(
    root_id: str,
    payload: CollectionUpdateRequest,
    session: Session = Depends(get_session)
):
    try:
        root_uuid = UUID(root_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID")

    art = session.get(Artifact, root_uuid)
    if not art:
        raise HTTPException(status_code=404, detail="Root not found")

    if payload.label is not None or payload.scanner_config is not None:
        meta = dict(art.metadata_json or {})
        if payload.label is not None:
            meta["label"] = payload.label
        if payload.scanner_config is not None:
            meta["scanner_config"] = payload.scanner_config
        art.metadata_json = meta

    session.add(art)
    session.commit()
    session.refresh(art)

    count = session.query(ArtifactRelation).filter(
        ArtifactRelation.source_id == root_uuid,
        ArtifactRelation.relation_type.in_(STRUCTURAL_PARENT_TO_CHILD)
    ).count()

    return CollectionResponse(
        id=str(root_uuid),
        uri=art.uri or "",
        type_name=art.type_name,
        label=art.metadata_json.get("label", "Untitled"),
        file_count=count,
        metadata=art.metadata_json or {},
    )


@router.delete("/{root_id}")
def remove_collection(root_id: str, session: Session = Depends(get_session)):
    # This just removes the root designation? Or deletes everything?
    # For now, let's just delete the root artifact.
    # Cascading deletes might be needed for relations.
    # But usually we don't want to delete the actual files' records unless requested.
    # Let's just remove the root from the UI list (change type back to folder? or delete)

    # Safe delete: delete artifact, relations will cascade if DB configured, or handle manually
    try:
        root_uuid = UUID(root_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid UUID")

    art = session.get(Artifact, root_uuid)
    if not art:
        raise HTTPException(status_code=404, detail="Root not found")

    session.delete(art)
    session.commit()
    return {"status": "deleted"}
