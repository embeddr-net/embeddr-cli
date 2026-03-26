from fastapi import APIRouter, Depends, Query, HTTPException, Response
from sqlmodel import Session, select, col, or_, cast, String, func
from embeddr.db.session import get_session
from embeddr_core.models.artifact_embedding import ArtifactEmbedding
from embeddr_core.models.artifact import Artifact
from embeddr_core.models.artifact_lineage import ArtifactLineage
from embeddr.api.security import get_auth_context, require_permission_for_request
from embeddr.auth.permissions import Permissions
from uuid import UUID
import numpy as np
import warnings
import json

# Suppress UMAP warnings if any
warnings.filterwarnings("ignore")

try:
    import umap
except ImportError:
    umap = None

router = APIRouter(
    dependencies=[Depends(require_permission_for_request(
        Permissions.ARTIFACTS_READ, Permissions.ARTIFACTS_WRITE
    ))]
)


def _public_visibility_expr():
    meta_text = func.lower(cast(Artifact.metadata_json, String))
    return or_(
        meta_text.contains('"visibility":"public"'),
        meta_text.contains('"visibility": "public"'),
    )


def _visibility_filter_expr(auth):
    if not auth or auth.is_open or auth.is_admin or getattr(auth, "is_root", False):
        return None
    user_id = getattr(auth, "user_id", None)
    if user_id:
        return or_(Artifact.owner_user_id == user_id, _public_visibility_expr())
    return _public_visibility_expr()


@router.get("/umap")
def get_umap_projection(
    model_name: str = Query("openai/clip-vit-base-patch32",
                            description="Model name for embeddings"),
    n_neighbors: int = 15,
    min_dist: float = 0.1,
    spread: float = 1.0,
    limit: int = 1000,
    random_state: int = 42,
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    if not umap:
        raise HTTPException(
            status_code=501,
            detail="umap-learn library not installed on backend."
        )

    # 1. Fetch real embeddings
    stmt = (
        select(ArtifactEmbedding, Artifact)
        .join(Artifact, Artifact.id == ArtifactEmbedding.artifact_id)
        .where(ArtifactEmbedding.model_name == model_name)
    )
    visibility_expr = _visibility_filter_expr(auth)
    if visibility_expr is not None:
        stmt = stmt.where(visibility_expr)
    stmt = stmt.limit(limit)
    results = session.exec(stmt).all()

    # Maps artifact_id (str) -> embedding (np.array)
    embedding_map = {}
    meta_map = {}

    for emb, art in results:
        vec = emb.vector_json
        if isinstance(vec, str):
            try:
                vec = json.loads(vec)
            except (ValueError, TypeError, json.JSONDecodeError):
                continue

        aid = str(art.id)
        embedding_map[aid] = np.array(vec)
        meta_map[aid] = {
            "id": aid,
            "label": art.metadata_json.get("label") or art.metadata_json.get("filename") or "Untitled",
            "type": art.type_name,
            "uri": art.uri,
        }

    # 2. Compute Centroids for Parents
    embedded_ids = [UUID(uid) for uid in embedding_map.keys()]
    if embedded_ids:
        # Find lineages where child matches our embedded set
        lineage_stmt = (
            select(ArtifactLineage, Artifact)
            .join(Artifact, Artifact.id == ArtifactLineage.parent_id)
            .where(col(ArtifactLineage.child_id).in_(embedded_ids))
        )
        lineage_rows = session.exec(lineage_stmt).all()

        parent_vectors = {}
        parent_artifacts = {}

        for lineage, parent_art in lineage_rows:
            pid = str(parent_art.id)
            cid = str(lineage.child_id)

            if pid in embedding_map:
                continue

            if cid in embedding_map:
                if pid not in parent_vectors:
                    parent_vectors[pid] = []
                    parent_artifacts[pid] = parent_art
                parent_vectors[pid].append(embedding_map[cid])

        for pid, vectors in parent_vectors.items():
            if not vectors:
                continue
            centroid = np.mean(np.array(vectors), axis=0)
            embedding_map[pid] = centroid

            art = parent_artifacts[pid]
            meta_map[pid] = {
                "id": pid,
                "label": art.metadata_json.get("label") or art.metadata_json.get("filename") or "Container",
                "type": art.type_name or "folder",
                "uri": art.uri,
                "is_virtual": True
            }

    # Prepare data for UMAP
    final_ids = list(embedding_map.keys())
    if len(final_ids) < 5:
        return []

    matrix = np.array([embedding_map[uid] for uid in final_ids])

    try:
        reducer = umap.UMAP(
            n_components=3,
            n_neighbors=min(n_neighbors, len(final_ids) - 1),
            min_dist=min_dist,
            spread=spread,
            metric='cosine',
            random_state=random_state
        )

        projections = reducer.fit_transform(matrix)

        output = []
        for i, point in enumerate(projections):
            uid = final_ids[i]
            meta = meta_map[uid]

            # Better Color Logic
            color = "#cccccc"
            t = (meta["type"] or "").lower()

            if "image" in t:
                color = "#FF5555"
            elif "text" in t:
                color = "#5555FF"
            elif "workflow" in t:
                color = "#55FF55"
            elif "folder" in t or "container" in t:
                color = "#FFFF55"
            elif meta.get("is_virtual"):
                color = "#FFD700"

            output.append({
                "id": uid,
                "x": float(point[0]),
                "y": float(point[1]),
                "z": float(point[2]),
                "color": color,
                "label": meta["label"],
                "metadata": {
                    "uri": meta["uri"],
                    "type": meta["type"],
                    "is_virtual": meta.get("is_virtual", False)
                }
            })

        return output
    except Exception as e:
        print(f"UMAP Error: {e}")
        raise HTTPException(
            status_code=500, detail=f"UMAP computation failed: {str(e)}")
