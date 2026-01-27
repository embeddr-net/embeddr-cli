from sqlalchemy.orm import aliased
import logging
from sqlalchemy import literal
from typing import Any, Dict, List, Optional
from uuid import UUID
import os

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile, File, Request
from fastapi.responses import FileResponse, Response, RedirectResponse, StreamingResponse
from sqlmodel import Session, select, col, func, or_, cast, String, delete
from embeddr.db.session import get_engine
from sqlalchemy import text
from json import JSONDecodeError
from embeddr_core.models.artifact import Artifact, ArtifactPreview
from embeddr_core.models.artifact_blob import ArtifactBlob
from embeddr_core.models.artifact_ingest import ArtifactIngest as ArtifactIngestModel
from embeddr_core.models.artifact_embedding import ArtifactEmbedding
from embeddr_core.models.artifact_feature import ArtifactFeatureRef
from embeddr_core.models.artifact_annotation import ArtifactAnnotation
from embeddr_core.models.artifact_lineage import ArtifactLineage
from embeddr_core.models.artifact_relation import ArtifactRelation
from pydantic import BaseModel
from embeddr_core.plugin_interface import EmbeddrEvent
from embeddr.core.plugin_loader import _EVENT_BUS
from embeddr.services.ingestion_service import ingestion_service
from pathlib import Path
from embeddr.services.storage import storage_service
from embeddr.services.blob_refs import blob_ref_from_blob, blob_ref_storage_path, BlobRef
from embeddr.services.blob_registry import resolve_response, resolve_blob, list_providers
from embeddr_core.services.config_service import resolve_plugin_config
from urllib.parse import urlparse
import requests

router = APIRouter()
logger = logging.getLogger("embeddr.api.v2.artifacts")


def _resolve_uri_response(
    uri: Optional[str],
    *,
    purpose: str,
    artifact_id: Optional[str],
) -> Optional[Response]:
    if not uri:
        return None

    if uri.startswith("s3://"):
        parsed = urlparse(uri)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/") if parsed.path else None
        provider_name = _select_s3_provider(bucket)
        blob_ref = BlobRef(
            provider=provider_name,
            bucket=bucket or None,
            key=key,
            path=uri,
            etag=None,
            content_type=None,
            size=None,
            artifact_id=artifact_id,
        )
        response = resolve_response(blob_ref, purpose)
        if response is None and provider_name != "s3":
            blob_ref.provider = "s3"
            return resolve_response(blob_ref, purpose)
        return response

    if uri.startswith("http"):
        return RedirectResponse(url=uri)

    return None


def _proxy_url_response(url: str) -> Response:
    resp = requests.get(url, stream=True)
    resp.raise_for_status()
    headers = {"Vary": "Origin"}
    content_type = resp.headers.get("content-type")
    if resp.headers.get("content-length"):
        headers["Content-Length"] = resp.headers.get("content-length")
    return StreamingResponse(
        resp.iter_content(chunk_size=1024 * 256),
        status_code=resp.status_code,
        media_type=content_type,
        headers=headers,
    )


def _should_proxy_redirect(request: Request, url: Optional[str]) -> bool:
    if not url:
        return False
    try:
        request_netloc = urlparse(str(request.url)).netloc
        target_netloc = urlparse(url).netloc
        return bool(target_netloc and request_netloc and target_netloc != request_netloc)
    except Exception:
        return False


def _resolve_uri_url(
    uri: Optional[str],
    *,
    purpose: str,
    artifact_id: Optional[str],
) -> Optional[Dict[str, Any]]:
    if not uri:
        return None

    if uri.startswith("s3://"):
        parsed = urlparse(uri)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/") if parsed.path else None
        provider_name = _select_s3_provider(bucket)
        blob_ref = BlobRef(
            provider=provider_name,
            bucket=bucket or None,
            key=key,
            path=uri,
            etag=None,
            content_type=None,
            size=None,
            artifact_id=artifact_id,
        )
        resolved = resolve_blob(blob_ref, purpose)
        if resolved and resolved.get("ok"):
            return resolved
        if provider_name != "s3":
            blob_ref.provider = "s3"
            resolved = resolve_blob(blob_ref, purpose)
            if resolved and resolved.get("ok"):
                return resolved

    if uri.startswith("http"):
        return {"ok": True, "url": uri, "headers": None, "expiresAt": None}

    return None


def _select_s3_provider(bucket: Optional[str]) -> str:
    if not bucket:
        return "s3"

    try:
        with Session(get_engine()) as session:
            cfg = resolve_plugin_config(
                session=session,
                plugin_name="embeddr-storage-s3",
                config_id="embeddr-storage-s3.config",
            ) or {}
    except Exception:
        return "s3"

    instances = cfg.get("instances")
    if isinstance(instances, list):
        for idx, inst in enumerate(instances):
            if not isinstance(inst, dict):
                continue
            inst_bucket = inst.get("s3_bucket") or inst.get("bucket")
            if inst_bucket != bucket:
                continue
            name = str(inst.get("name") or inst.get(
                "id") or f"instance-{idx + 1}")
            provider_name = f"s3:{name}"
            if provider_name in list_providers():
                return provider_name
            return "s3"

    return "s3"


class PaginatedArtifacts(BaseModel):
    items: List[Artifact]
    total: int
    limit: int
    offset: int


class ArtifactCreate(BaseModel):
    type_name: str = "collection"
    base_type_name: str = "artifact"
    metadata_json: dict = {}
    uri: Optional[str] = None
    override_capabilities: List[str] = []


class ArtifactUpdate(BaseModel):
    metadata_json: Optional[Dict[str, Any]] = None
    override_capabilities: Optional[List[str]] = None
    uri: Optional[str] = None  # Added support for updating URI
    type_name: Optional[str] = None
    base_type_name: Optional[str] = None


class RelationCreate(BaseModel):
    target_id: UUID
    relation_type: str = "contains"
    metadata_json: Dict[str, Any] = {}


class RelationIngest(BaseModel):
    target_uri: str
    relation_type: str = "contains"
    metadata_json: Dict[str, Any] = {}


class ArtifactIngestRequestItem(BaseModel):
    uri: str
    id: Optional[str] = None
    type_name: str = "artifact"
    base_type_name: str = "artifact"
    metadata_json: Dict[str, Any] = {}
    override_capabilities: List[str] = []
    relations: List[RelationIngest] = []


class IngestRequest(BaseModel):
    items: List[ArtifactIngestRequestItem]


def get_session():
    engine = get_engine()
    with Session(engine) as session:
        yield session


@router.post("/ingest", status_code=202)
async def ingest_artifacts(req: IngestRequest):
    """
    Async ingestion endpoint.
    Accepts items, puts them in a queue, and processes them in background.
    """
    count = 0
    for item in req.items:
        await ingestion_service.ingest(item.model_dump())
        count += 1

    return {"status": "accepted", "queued": count}


@router.post("/uploads/{upload_id}")
async def upload_artifact_bytes(
    upload_id: UUID,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    ingest = session.get(ArtifactIngestModel, upload_id)
    if not ingest:
        raise HTTPException(404, "Upload not found")
    if ingest.status != "pending":
        raise HTTPException(409, f"Upload is {ingest.status}")

    if not ingest.meta_json.get("confirmed"):
        raise HTTPException(400, "confirmation_required")

    artifact = session.get(Artifact, ingest.artifact_id)
    if not artifact:
        raise HTTPException(404, "Artifact not found")

    filename = Path(file.filename or "upload.bin").name
    blob_ref = storage_service.store_upload(
        session=session,
        artifact=artifact,
        upload_file=file,
        filename=filename,
        content_type=file.content_type,
        backend_hint=ingest.storage_backend,
        confirmed=bool(ingest.meta_json.get("confirmed")),
    )

    storage_path = blob_ref_storage_path(blob_ref)
    if not storage_path:
        raise HTTPException(
            status_code=500, detail="blob_storage_path_missing")

    blob = ArtifactBlob(
        artifact_id=artifact.id,
        storage_backend=blob_ref.provider,
        path=storage_path,
        sha256=blob_ref.etag,
        size=blob_ref.size,
        content_type=blob_ref.content_type,
    )
    session.add(blob)

    ingest.status = "uploaded"
    ingest.storage_backend = blob_ref.provider
    ingest.storage_path = storage_path
    ingest.content_type = blob_ref.content_type
    ingest.size = blob_ref.size
    ingest.original_filename = filename
    session.add(ingest)

    artifact.uri = storage_path
    session.add(artifact)

    session.commit()
    session.refresh(blob)

    return {
        "ok": True,
        "artifact_id": str(artifact.id),
        "upload_id": str(upload_id),
        "blob": {
            "id": str(blob.id),
            "path": blob.path,
            "size": blob.size,
            "content_type": blob.content_type,
            "sha256": blob.sha256,
            "storage_backend": blob.storage_backend,
            "public_url": None,
        },
        "blob_ref": blob_ref.to_dict(),
    }


@router.post("/{artifact_id}/relations", response_model=Dict[str, str])
def add_relation(
    artifact_id: UUID,
    rel: RelationCreate,
    session: Session = Depends(get_session)
):
    """Create a relationship between artifacts."""
    # Check if exists to avoid dupes?
    existing = session.exec(select(ArtifactRelation).where(
        ArtifactRelation.source_id == artifact_id,
        ArtifactRelation.target_id == rel.target_id,
        ArtifactRelation.relation_type == rel.relation_type
    )).first()

    if existing:
        return {"status": "exists", "id": f"{existing.source_id}:{existing.target_id}"}

    new_rel = ArtifactRelation(
        source_id=artifact_id,
        target_id=rel.target_id,
        relation_type=rel.relation_type,
        metadata_json=rel.metadata_json
    )
    session.add(new_rel)
    session.commit()
    return {"status": "created", "id": f"{new_rel.source_id}:{new_rel.target_id}"}


@router.post("", response_model=Artifact)
async def create_artifact(
    art_in: ArtifactCreate,
    session: Session = Depends(get_session)
):
    """Create a new artifact manually (e.g. valid 'collection' or virtual container)."""
    # If no URI, mint a virtual one?
    uri = art_in.uri
    if not uri and "name" in art_in.metadata_json:
        # e.g. virtual://user/My Album
        import uuid
        uri = f"virtual://user/{uuid.uuid4()}"

    new_art = Artifact(
        type_name=art_in.type_name,
        base_type_name=art_in.base_type_name,  # Allow base_type_name to be set via API
        uri=uri,
        metadata_json=art_in.metadata_json,
        override_capabilities=art_in.override_capabilities
    )

    session.add(new_art)
    session.commit()
    session.refresh(new_art)

    if _EVENT_BUS:
        # Since we are async now, we can await if needed, OR the event bus handles tasks.
        # But we MUST ensure the event bus publish finds the loop.
        # Since create_artifact is now `async def`, it runs in the main loop.
        try:
            _EVENT_BUS.publish(EmbeddrEvent(
                event_type="artifact.created",
                source="api/v2/artifacts",
                payload={
                    "id": str(new_art.id),
                    "uri": str(new_art.uri),
                    "type": new_art.type_name
                }
            ))
        except Exception as e:
            # Don't fail the request if eventing fails
            print(f"Failed to publish artifact.created: {e}")

    return new_art


@router.patch("/{artifact_id}", response_model=Artifact)
def update_artifact(
    artifact_id: UUID,
    art_update: ArtifactUpdate,
    session: Session = Depends(get_session)
):
    """Update an existing artifact."""
    artifact = session.get(Artifact, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    if art_update.metadata_json is not None:
        # Deep merge or shallow merge? Shallow merge for now.
        # Ensure we copy the existing dict so SA tracking picks it up
        current = dict(artifact.metadata_json or {})
        current.update(art_update.metadata_json)
        artifact.metadata_json = current

    if art_update.override_capabilities is not None:
        artifact.override_capabilities = art_update.override_capabilities

    if art_update.uri is not None:
        artifact.uri = art_update.uri

    if art_update.type_name is not None:
        artifact.type_name = art_update.type_name

    if art_update.base_type_name is not None:
        artifact.base_type_name = art_update.base_type_name

    session.add(artifact)
    session.commit()
    session.refresh(artifact)

    if _EVENT_BUS:
        _EVENT_BUS.publish(EmbeddrEvent(
            event_type="artifact.updated",
            source="api/v2/artifacts",
            payload={
                "id": str(artifact.id),
                "changes": art_update.dict(exclude_unset=True)
            }
        ))

    return artifact


@router.get("/search", response_model=PaginatedArtifacts)
def search_artifacts(
    q: Optional[str] = Query(None),
    limit: int = 20,
    offset: int = 0,
    type_name: Optional[str] = None,
    type_prefix: Optional[str] = None,
    session: Session = Depends(get_session)
):
    """Search artifacts by URI or metadata."""

    query = select(Artifact)
    count_query = select(func.count(Artifact.id))

    # Text Search Filter
    if q and len(q) > 0:
        filter_condition = or_(
            col(Artifact.uri).contains(q),
            cast(Artifact.metadata_json, String).contains(q)
        )
        query = query.where(filter_condition)
        count_query = count_query.where(filter_condition)

    if type_name:
        query = query.where(Artifact.type_name == type_name)
        count_query = count_query.where(Artifact.type_name == type_name)

    if type_prefix:
        query = query.where(Artifact.type_name.startswith(type_prefix))
        count_query = count_query.where(
            Artifact.type_name.startswith(type_prefix))

    total = session.exec(count_query).one()

    # Apply limit/offset
    query = query.limit(limit).offset(offset)
    items = session.exec(query).all()

    return PaginatedArtifacts(
        items=items,
        total=total,
        limit=limit,
        offset=offset
    )


class BulkOperationRequest(BaseModel):
    operation: str
    artifact_ids: List[UUID]
    payload: Dict[str, Any] = {}


@router.post("/bulk_operations")
def bulk_operations(
    request: BulkOperationRequest,
    session: Session = Depends(get_session)
):
    """Perform bulk operations on artifacts (move, delete, tag)."""
    if not request.artifact_ids:
        return {"count": 0, "message": "No artifacts selected"}

    if request.operation == "delete":
        # Bulk delete
        # First, find all artifacts to be deleted
        stm = select(Artifact).where(Artifact.id.in_(request.artifact_ids))
        items = session.exec(stm).all()

        # Explicitly delete related entities to ensure cleanup
        # (ArtifactEmbedding, ArtifactAnnotation, ArtifactRelation, etc.)
        # Note: If database has ON DELETE CASCADE, this is redundant but safe.
        # If not, this prevents integrity errors or zombie data.

        # Delete embeddings
        session.exec(delete(ArtifactEmbedding).where(
            ArtifactEmbedding.artifact_id.in_(request.artifact_ids)))

        # Delete feature refs
        session.exec(delete(ArtifactFeatureRef).where(
            ArtifactFeatureRef.artifact_id.in_(request.artifact_ids)))

        # Delete annotations
        session.exec(delete(ArtifactAnnotation).where(
            ArtifactAnnotation.artifact_id.in_(request.artifact_ids)))

        # Delete previews
        session.exec(delete(ArtifactPreview).where(
            ArtifactPreview.artifact_id.in_(request.artifact_ids)))

        # Delete relations (both directions)
        session.exec(delete(ArtifactRelation).where(
            or_(
                ArtifactRelation.source_id.in_(request.artifact_ids),
                ArtifactRelation.target_id.in_(request.artifact_ids)
            )
        ))

        # Lineage (as descendant)
        session.exec(delete(ArtifactLineage).where(
            ArtifactLineage.artifact_id.in_(request.artifact_ids)))

        # Lineage (as ancestor) - this breaks history for descendants, but if ancestor is gone...
        session.exec(delete(ArtifactLineage).where(
            ArtifactLineage.ancestor_id.in_(request.artifact_ids)))

        count = 0
        for item in items:
            session.delete(item)
            count += 1
        session.commit()
        return {"count": count, "message": f"Deleted {count} artifacts"}

    elif request.operation == "move":
        # Bulk move to collection
        target_id = request.payload.get("target_id")
        # Logic: "Root" might be indicated by None or a specific ID?
        # If target_id is valid UUID, we link to it.
        # If target_id is "root" or None, we remove parent links.

        if not target_id:
            raise HTTPException(
                status_code=400, detail="Target collection ID required for move")

        # 1. Remove existing parent relationships for these artifacts
        # Parents are:
        # - Source of 'contains' targeting artifact
        # - Target of 'contained_in' sourcing artifact
        ids = request.artifact_ids

        # Let's iterate and delete to be safe with SQLModel/SQLAlchemy session tracking

        rels_to_remove = session.exec(
            select(ArtifactRelation).where(
                or_(
                    (ArtifactRelation.target_id.in_(ids) &
                     ArtifactRelation.relation_type.in_(["contains", "group"])),
                    (ArtifactRelation.source_id.in_(ids) &
                     ArtifactRelation.relation_type.in_(["contained_in", "member_of"]))
                )
            )
        ).all()

        for r in rels_to_remove:
            session.delete(r)

        # 2. Add new relationship if target is not "root"
        # We'll use "contained_in" from Artifact -> Target Collection
        # This matches the upload behavior and is safer if the target is a "smart" collection
        if target_id != "root":
            try:
                target_uuid = UUID(str(target_id))
            except ValueError:
                # If "root" string was passed but logic missed it, or bad ID
                if target_id == "root":
                    target_uuid = None
                else:
                    raise HTTPException(
                        status_code=400, detail="Invalid target ID")

            if target_uuid:
                for art_id in ids:
                    # Check if connection exists (unlikely after delete, but safe)
                    # We consistently create "contained_in" relations for manual moves
                    rel = ArtifactRelation(
                        source_id=art_id,
                        target_id=target_uuid,
                        relation_type="contained_in",
                        source_namespace="user_manual",
                        target_namespace="user_manual"
                    )
                    session.add(rel)

        session.commit()
        return {"count": len(ids), "message": "Moved artifacts"}

    else:
        raise HTTPException(
            status_code=400, detail=f"Unknown operation {request.operation}")


@router.get("/", response_model=PaginatedArtifacts)
def list_artifacts(
    limit: int = 50,
    offset: int = 0,
    type_name: Optional[str] = None,
    media_type: Optional[str] = None,
    collection_id: Optional[UUID] = None,
    library_id: Optional[UUID] = None,
    parent_id: Optional[UUID] = None,
    recursive: bool = True,
    sort: str = "new",
    is_archived: Optional[bool] = None,
    tags: Optional[List[str]] = Query(None),
    ids: Optional[List[UUID]] = Query(None),
    session: Session = Depends(get_session)
):
    """List all artifacts with optional filtering."""
    # Count query
    count_query = select(func.count(Artifact.id))
    query = select(Artifact)

    if ids:
        query = query.where(Artifact.id.in_(ids))
        count_query = count_query.where(Artifact.id.in_(ids))

    # Join for collection filtering if needed
    # Treat parent_id same as collection_id for filtering
    target_id = collection_id or library_id or parent_id

    if target_id:
        if recursive:
            # Recursive CTE to find all descendant IDs
            # Base case: direct children
            # Note: We currently only recurse down "contains".
            # If we want to support "contained_in" recursively, we need a more complex graph walk.
            # For now, we assume deep hierarchies use "contains".
            base_stmt = select(ArtifactRelation.target_id).where(
                ArtifactRelation.source_id == target_id,
                ArtifactRelation.relation_type.in_(["contains", "group"])
            )

            cte = base_stmt.cte("descendants", recursive=True)

            # Recursive step: children of children
            ar_alias = aliased(ArtifactRelation)
            recursive_part = select(ar_alias.target_id).join(
                cte, ar_alias.source_id == cte.c.target_id
            ).where(ar_alias.relation_type.in_(["contains", "group"]))

            cte = cte.union_all(recursive_part)

            # Filter Artifacts that are in the CTE
            # We also include direct children via "contained_in" (shallow inclusion)
            # to capture uploaded files at this level even if they don't continue the chain.
            subq_up = select(ArtifactRelation.source_id).where(
                ArtifactRelation.target_id == target_id,
                ArtifactRelation.relation_type.in_(
                    ["contained_in", "member_of"])
            )

            query = query.where(
                or_(
                    Artifact.id.in_(select(cte.c.target_id)),
                    Artifact.id.in_(subq_up)
                )
            )
            count_query = count_query.where(
                or_(
                    Artifact.id.in_(select(cte.c.target_id)),
                    Artifact.id.in_(subq_up)
                )
            )
        else:
            # Direct children only
            # Case 1: Parent --[contains/group]--> Child
            subq_down = select(ArtifactRelation.target_id).where(
                ArtifactRelation.source_id == target_id,
                ArtifactRelation.relation_type.in_(["contains", "group"])
            )
            # Case 2: Child --[contained_in/member_of]--> Parent
            subq_up = select(ArtifactRelation.source_id).where(
                ArtifactRelation.target_id == target_id,
                ArtifactRelation.relation_type.in_(
                    ["contained_in", "member_of"])
            )

            query = query.where(
                or_(
                    Artifact.id.in_(subq_down),
                    Artifact.id.in_(subq_up)
                )
            )
            count_query = count_query.where(
                or_(
                    Artifact.id.in_(subq_down),
                    Artifact.id.in_(subq_up)
                )
            )
    elif not recursive:
        # No collection specified + recursive=False -> Root items (orphans)
        # Items that are not inside ANY container.
        # This means:
        # 1. Not a target of "contains"
        # 2. Not a source of "contained_in"

        subq_is_contained = select(ArtifactRelation.target_id).where(
            ArtifactRelation.relation_type.in_(["contains", "group"])
        )
        subq_is_inside = select(ArtifactRelation.source_id).where(
            ArtifactRelation.relation_type.in_(["contained_in", "member_of"])
        )

        query = query.where(Artifact.id.not_in(subq_is_contained))\
                     .where(Artifact.id.not_in(subq_is_inside))
        count_query = count_query.where(Artifact.id.not_in(subq_is_contained))\
                                 .where(Artifact.id.not_in(subq_is_inside))

    if type_name:
        if "," in type_name:
            # Handle comma separated list as OR
            types = [t.strip() for t in type_name.split(",")]
            count_query = count_query.where(Artifact.type_name.in_(types))
            query = query.where(Artifact.type_name.in_(types))
        else:
            count_query = count_query.where(Artifact.type_name == type_name)
            query = query.where(Artifact.type_name == type_name)

    if media_type:
        # Map common media types to artifact types
        if media_type == "image":
            # Check both base_type_name and type_name to cover legacy/plugin created artifacts
            cond = or_(
                Artifact.base_type_name == "image",
                Artifact.type_name == "image"
            )
            query = query.where(cond)
            count_query = count_query.where(cond)
        elif media_type == "video":
            cond = or_(
                Artifact.base_type_name == "video",
                Artifact.type_name == "video"
            )
            query = query.where(cond)
            count_query = count_query.where(cond)

    # Naive tag filtering on metadata_json
    if tags:
        for tag in tags:
            # We assume tags are stored in metadata_json['tags'] as list or string
            # SQLite specific JSON extract might be needed for robustness, but simple string search is fallback
            # This is NOT efficient for huge DBs.
            tag_quoted = f'"{tag}"'  # quoted for json
            query = query.where(
                cast(Artifact.metadata_json, String).contains(tag))
            count_query = count_query.where(
                cast(Artifact.metadata_json, String).contains(tag))

    if is_archived is not None:
        # Assuming is_archived is a bool in metadata
        if is_archived:
            query = query.where(
                cast(Artifact.metadata_json, String).contains('"is_archived": true'))
            count_query = count_query.where(
                cast(Artifact.metadata_json, String).contains('"is_archived": true'))
        else:
            # This is tricky because false might be missing or explicit false
            # For now, ignore it or check for absence?
            # Let's just implement explicit true check for now.
            pass

    total_count = session.exec(count_query).one()

    # Sort
    if sort == "random":
        query = query.order_by(func.random())
    else:
        query = query.order_by(Artifact.created_at.desc())

    # Fetch items
    query = query.offset(offset).limit(limit)
    items = session.exec(query).all()

    return PaginatedArtifacts(
        items=items,
        total=total_count,
        limit=limit,
        offset=offset
    )


def _coerce_uuid(value: str, label: str) -> UUID:
    try:
        return UUID(str(value))
    except ValueError:
        raise HTTPException(status_code=400, detail=f"Invalid {label}")


def _get_artifact_or_repair(session: Session, artifact_uuid: UUID) -> Optional[Artifact]:
    try:
        return session.get(Artifact, artifact_uuid)
    except JSONDecodeError:
        session.exec(
            text(
                "UPDATE artifact SET metadata_json = '{}' "
                "WHERE id = :id AND (metadata_json IS NULL OR trim(metadata_json) = '')"
            ),
            {"id": str(artifact_uuid)},
        )
        session.commit()
        return session.get(Artifact, artifact_uuid)


@router.get("/{artifact_id}", response_model=Artifact)
def get_artifact(artifact_id: str, session: Session = Depends(get_session)):
    """Retrieve a single artifact."""
    artifact_uuid = _coerce_uuid(artifact_id, "artifact_id")
    artifact = _get_artifact_or_repair(session, artifact_uuid)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")
    return artifact


@router.get("/{artifact_id}/content")
def get_artifact_content(
    artifact_id: str,
    request: Request,
    proxy: bool = False,
    session: Session = Depends(get_session)
):
    """
    Stream the raw content of the artifact if it is a local file.
    Does simplistic mime-type guessing based on extension.
    """
    artifact_uuid = _coerce_uuid(artifact_id, "artifact_id")
    artifact = _get_artifact_or_repair(session, artifact_uuid)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    prefer_proxy = proxy or os.environ.get("EMBEDDR_CONTENT_PROXY", "").lower() in {
        "1",
        "true",
        "yes",
    }

    logger.info(
        "artifact_content_request",
        extra={
            "artifact_id": str(artifact_uuid),
            "uri": artifact.uri,
            "type_name": artifact.type_name,
        },
    )

    # Handle internal text notes
    if artifact.type_name == "text" or (artifact.uri and artifact.uri.startswith("internal://")):
        text_content = None
        if artifact.metadata_json:
            text_content = artifact.metadata_json.get("text_content")

        if text_content is not None:
            return Response(
                content=str(text_content),
                media_type="text/plain",
                headers={"Vary": "Origin"}
            )

    # Check URI
    if not artifact.uri:
        raise HTTPException(
            status_code=400, detail="Artifact does not have a URI")

    blob = session.exec(
        select(ArtifactBlob)
        .where(ArtifactBlob.artifact_id == artifact_uuid)
        .order_by(col(ArtifactBlob.created_at).desc())
    ).first()

    if blob:
        logger.info(
            "artifact_content_blob",
            extra={
                "artifact_id": str(artifact_uuid),
                "storage_backend": blob.storage_backend,
                "path": blob.path,
            },
        )
        response = storage_service.get_content_response(
            session=session,
            blob=blob,
            artifact=artifact,
            purpose="original",
        )
        if response:
            if isinstance(response, RedirectResponse):
                url = response.headers.get("location")
                if url and (prefer_proxy or _should_proxy_redirect(request, url)):
                    return _proxy_url_response(url)
            return response

    # Fallback: resolve from latest ingest record (if blob missing)
    ingest = session.exec(
        select(ArtifactIngestModel)
        .where(ArtifactIngestModel.artifact_id == artifact_uuid)
        .order_by(col(ArtifactIngestModel.created_at).desc())
    ).first()
    if ingest and ingest.storage_path:
        logger.info(
            "artifact_content_ingest",
            extra={
                "artifact_id": str(artifact_uuid),
                "storage_path": ingest.storage_path,
            },
        )
        response = _resolve_uri_response(
            ingest.storage_path,
            purpose="original",
            artifact_id=str(artifact_uuid),
        )
        if response:
            if isinstance(response, RedirectResponse):
                url = response.headers.get("location")
                if url and (prefer_proxy or _should_proxy_redirect(request, url)):
                    return _proxy_url_response(url)
            return response

    uri_response = _resolve_uri_response(
        artifact.uri,
        purpose="original",
        artifact_id=str(artifact_uuid),
    )
    if uri_response:
        if isinstance(uri_response, RedirectResponse):
            url = uri_response.headers.get("location")
            if url and (prefer_proxy or _should_proxy_redirect(request, url)):
                return _proxy_url_response(url)
        return uri_response

    file_path = None

    # Check URI if it's a file path
    uri_path = artifact.uri
    # Handle file:// scheme
    if uri_path.startswith("file://"):
        uri_path = uri_path[7:]

    # If explicit absolute path or existing file
    if os.path.isabs(uri_path) or os.path.exists(uri_path):
        file_path = uri_path

    # If HTTP, we can't stream it directly unless we proxy it (not implemented)
    elif uri_path.startswith("http"):
        if prefer_proxy or _should_proxy_redirect(request, uri_path):
            return _proxy_url_response(uri_path)
        return RedirectResponse(url=uri_path)

    if not file_path or not os.path.exists(file_path):
        raise HTTPException(
            status_code=404, detail=f"Primary file not found on disk: {file_path or artifact.uri}")

    # Simple mime detection
    import mimetypes
    media_type, _ = mimetypes.guess_type(file_path)

    return FileResponse(
        file_path,
        media_type=media_type,
        headers={"Vary": "Origin"}
    )


@router.get("/{artifact_id}/resolve")
def resolve_artifact(
    artifact_id: str,
    variant: str = "original",
    proxy: bool = False,
    session: Session = Depends(get_session),
):
    """
    Resolve an artifact to a URL for preview/original access.
    Proxy mode for v0.1: returns internal URLs.
    """
    artifact_uuid = _coerce_uuid(artifact_id, "artifact_id")
    artifact = _get_artifact_or_repair(session, artifact_uuid)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    logger.info(
        "artifact_resolve_request",
        extra={
            "artifact_id": str(artifact_uuid),
            "variant": variant,
            "proxy": proxy,
            "uri": artifact.uri,
        },
    )

    prefer_proxy = proxy or os.environ.get("EMBEDDR_RESOLVE_PROXY", "").lower() in {
        "1",
        "true",
        "yes",
    }

    if prefer_proxy:
        blob = session.exec(
            select(ArtifactBlob)
            .where(ArtifactBlob.artifact_id == artifact_uuid)
            .order_by(col(ArtifactBlob.created_at).desc())
        ).first()
        blob_ref = None
        if blob:
            blob_ref = blob_ref_from_blob(
                blob,
                artifact_id=str(artifact_uuid),
            ).to_dict()

        url = (
            f"/api/v2/artifacts/{artifact_uuid}/preview"
            if variant == "preview"
            else f"/api/v2/artifacts/{artifact_uuid}/content"
        )

        return {
            "ok": True,
            "artifact_id": str(artifact_uuid),
            "variant": variant,
            "url": url,
            "headers": None,
            "expiresAt": None,
            "blob_ref": blob_ref,
            "proxy": True,
        }

    if variant == "preview":
        url = f"/api/v2/artifacts/{artifact_uuid}/preview"
    else:
        url = f"/api/v2/artifacts/{artifact_uuid}/content"

    if variant == "preview":
        preview = session.exec(
            select(ArtifactPreview)
            .where(ArtifactPreview.artifact_id == artifact_uuid)
            .where(ArtifactPreview.preview_type == "thumbnail")
            .order_by(ArtifactPreview.created_at.desc())
        ).first()

        if preview:
            resolved = _resolve_uri_url(
                preview.uri,
                purpose="preview",
                artifact_id=str(artifact_uuid),
            )
            if resolved and resolved.get("ok"):
                return {
                    **resolved,
                    "artifact_id": str(artifact_uuid),
                    "variant": variant,
                    "preview_uri": preview.uri,
                }

            return {
                "ok": True,
                "artifact_id": str(artifact_uuid),
                "variant": variant,
                "url": url,
                "headers": None,
                "expiresAt": None,
                "preview_uri": preview.uri,
            }

    blob = session.exec(
        select(ArtifactBlob)
        .where(ArtifactBlob.artifact_id == artifact_uuid)
        .order_by(col(ArtifactBlob.created_at).desc())
    ).first()

    blob_ref = None
    if blob:
        blob_ref = blob_ref_from_blob(
            blob,
            artifact_id=str(artifact_uuid),
        ).to_dict()

        resolved = storage_service.resolve_blob(blob, purpose=variant)
        if resolved and resolved.get("ok"):
            logger.info(
                "artifact_resolve_blob",
                extra={
                    "artifact_id": str(artifact_uuid),
                    "storage_backend": blob.storage_backend,
                    "resolved_url": resolved.get("url"),
                },
            )
            return {
                **resolved,
                "artifact_id": str(artifact_uuid),
                "variant": variant,
                "blob_ref": blob_ref,
            }

    return {
        "ok": True,
        "artifact_id": str(artifact_uuid),
        "variant": variant,
        "url": url,
        "headers": None,
        "expiresAt": None,
        "blob_ref": blob_ref,
    }


@router.delete("/{artifact_id}")
def delete_artifact(
    artifact_id: UUID,
    session: Session = Depends(get_session)
):
    """Delete an artifact and its metadata."""
    artifact = session.get(Artifact, artifact_id)
    if not artifact:
        raise HTTPException(status_code=404, detail="Artifact not found")

    session.delete(artifact)
    session.exec(
        delete(ArtifactFeatureRef).where(
            ArtifactFeatureRef.artifact_id == artifact_id
        )
    )
    session.commit()

    if _EVENT_BUS:
        _EVENT_BUS.publish(EmbeddrEvent(
            event_type="artifact.deleted",
            source="api/v2/artifacts",
            payload={"id": str(artifact_id)}
        ))

    return {"ok": True}


@router.get("/{artifact_id}/embeddings", response_model=List[ArtifactEmbedding])
def get_artifact_embeddings(artifact_id: UUID, session: Session = Depends(get_session)):
    """Retrieve embeddings for an artifact."""
    query = select(ArtifactEmbedding).where(
        ArtifactEmbedding.artifact_id == artifact_id)
    return session.exec(query).all()


@router.get("/{artifact_id}/features", response_model=List[ArtifactFeatureRef])
def get_artifact_features(artifact_id: UUID, session: Session = Depends(get_session)):
    """Retrieve feature references for an artifact."""
    query = select(ArtifactFeatureRef).where(
        ArtifactFeatureRef.artifact_id == artifact_id)
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


@router.get("/{artifact_id}/subgraph")
def get_artifact_subgraph(
    artifact_id: UUID,
    max_depth: int = 3,
    include_lineage: bool = True,
    include_relations: bool = True,
    session: Session = Depends(get_session)
):
    """
    Explore the artifact graph recursively from a root node.
    Returns a unified set of nodes and edges found within max_depth.
    """
    visited_nodes = {artifact_id}
    edges = []

    # We use a BFS approach here for simplicity with multiple edge types,
    # but could be optimized further via Recursive CTE if performance is critical for deep trees.
    frontier = {artifact_id}

    for _ in range(max_depth):
        if not frontier:
            break

        next_frontier = set()
        frontier_list = list(frontier)

        # 1. Lineage
        if include_lineage:
            # Parents
            parent_lineage = session.exec(
                select(ArtifactLineage).where(
                    ArtifactLineage.child_id.in_(frontier_list))
            ).all()
            for l in parent_lineage:
                edges.append(
                    {"type": "lineage", "source": l.parent_id, "target": l.child_id})
                if l.parent_id not in visited_nodes:
                    visited_nodes.add(l.parent_id)
                    next_frontier.add(l.parent_id)

            # Children
            child_lineage = session.exec(
                select(ArtifactLineage).where(
                    ArtifactLineage.parent_id.in_(frontier_list))
            ).all()
            for l in child_lineage:
                edges.append(
                    {"type": "lineage", "source": l.parent_id, "target": l.child_id})
                if l.child_id not in visited_nodes:
                    visited_nodes.add(l.child_id)
                    next_frontier.add(l.child_id)

        # 2. Relations
        if include_relations:
            relations = session.exec(
                select(ArtifactRelation).where(
                    (ArtifactRelation.source_id.in_(frontier_list)) |
                    (ArtifactRelation.target_id.in_(frontier_list))
                )
            ).all()
            for r in relations:
                edges.append({
                    "type": "relation",
                    "label": r.relation_type,
                    "source": r.source_id,
                    "target": r.target_id
                })
                # Add neighbors to frontier
                if r.source_id not in visited_nodes:
                    visited_nodes.add(r.source_id)
                    next_frontier.add(r.source_id)
                if r.target_id not in visited_nodes:
                    visited_nodes.add(r.target_id)
                    next_frontier.add(r.target_id)

        frontier = next_frontier

    return {
        "root_id": artifact_id,
        "nodes": list(visited_nodes),
        "edges": edges
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


@router.get("/{artifact_id}/preview")
def get_artifact_preview(
    artifact_id: str,
    preview_type: str = "thumbnail",
    session: Session = Depends(get_session)
):
    """Retrieve an artifact preview/thumbnail."""
    # Find specific preview
    artifact_uuid = _coerce_uuid(artifact_id, "artifact_id")
    preview = session.exec(
        select(ArtifactPreview)
        .where(ArtifactPreview.artifact_id == artifact_uuid)
        .where(ArtifactPreview.preview_type == preview_type)
        .order_by(ArtifactPreview.created_at.desc())
    ).first()

    if not preview:
        # Fallback: If the artifact itself is an image, serve the original file
        artifact = _get_artifact_or_repair(session, artifact_uuid)

        # Check if it looks like an image either by type or extension
        is_image_type = artifact and (
            artifact.base_type_name == "image" or
            artifact.type_name == "image" or
            (artifact.metadata_json and artifact.metadata_json.get(
                "extension", "").lower() in [".jpg", ".jpeg", ".png", ".webp", ".gif"])
        )

        if is_image_type:
            blob = session.exec(
                select(ArtifactBlob)
                .where(ArtifactBlob.artifact_id == artifact_uuid)
                .order_by(col(ArtifactBlob.created_at).desc())
            ).first()

            if blob:
                response = storage_service.get_content_response(
                    session=session,
                    blob=blob,
                    artifact=artifact,
                    purpose="preview",
                )
                if response:
                    return response

        # Fallback: resolve from latest ingest record (if blob missing)
        ingest = session.exec(
            select(ArtifactIngestModel)
            .where(ArtifactIngestModel.artifact_id == artifact_uuid)
            .order_by(col(ArtifactIngestModel.created_at).desc())
        ).first()
        if ingest and ingest.storage_path:
            response = _resolve_uri_response(
                ingest.storage_path,
                purpose="preview",
                artifact_id=str(artifact_uuid),
            )
            if response:
                return response

        if is_image_type and artifact.uri:
            # Handle file:// scheme
            fpath = artifact.uri
            if fpath.startswith("file://"):
                fpath = fpath[7:]

            if os.path.isfile(fpath):
                # Simple mime detection for fallback
                import mimetypes
                media_type, _ = mimetypes.guess_type(fpath)
                return FileResponse(
                    fpath,
                    media_type=media_type,
                    headers={"Vary": "Origin"}
                )

        # External preview fallback (e.g. stash)
        external = (artifact.metadata_json or {}).get(
            "external") if artifact else None
        if isinstance(external, dict):
            preview_url = external.get("preview_url")
            if preview_url:
                response = _resolve_uri_response(
                    preview_url,
                    purpose="preview",
                    artifact_id=str(artifact_uuid),
                )
                if response:
                    return response

        raise HTTPException(status_code=404, detail="Preview not found")

    # If URI is local path
    preview_uri = preview.uri
    if preview_uri.startswith("file://"):
        preview_uri = preview_uri[7:]

    # Resolve remote/s3 preview URIs
    preview_response = _resolve_uri_response(
        preview.uri,
        purpose="preview",
        artifact_id=str(artifact_uuid),
    )
    if preview_response:
        return preview_response

    if preview_uri.startswith("/"):
        if not os.path.isfile(preview_uri):
            raise HTTPException(
                status_code=404, detail="Preview file missing or invalid")
        return FileResponse(
            preview_uri,
            media_type=preview.mime_type,
            headers={"Vary": "Origin"}
        )

    # If URI is remote, we should probably redirect or proxy.
    # For now assume local filesystem as per user workspace description
    # If it is not absolute path, handle it relative to root?
    # User said "Always reference absolute paths".

    return FileResponse(
        preview.uri,
        media_type=preview.mime_type,
        headers={"Vary": "Origin"}
    )
