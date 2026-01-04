import json
import os
import shutil
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import List

import imagehash
from embeddr_core.models.collection import CollectionItem
from embeddr_core.models.library import LibraryPath, LocalImage
from embeddr_core.models.lineage import ImageLineage
from embeddr_core.services.embedding import (
    get_image_embedding,
    get_loaded_model_name,
    get_text_embedding,
)
from embeddr_core.services.vector_store import get_vector_store
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image
from sqlalchemy.orm import selectinload
from sqlmodel import Session, SQLModel, func, select

from embeddr.core.config import settings
from embeddr.db.session import get_session


def get_video_metadata(path: Path):
    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        video_stream = next(
            (s for s in data["streams"] if s["codec_type"] == "video"), None
        )
        if not video_stream:
            return None

        width = int(video_stream.get("width", 0))
        height = int(video_stream.get("height", 0))
        duration = float(data["format"].get("duration", 0))

        avg_frame_rate = video_stream.get("avg_frame_rate", "0/0")
        if "/" in avg_frame_rate:
            num, den = map(int, avg_frame_rate.split("/"))
            fps = num / den if den > 0 else 0
        else:
            fps = float(avg_frame_rate)

        frame_count = int(video_stream.get("nb_frames", 0))
        if frame_count == 0 and fps > 0:
            frame_count = int(duration * fps)

        return {
            "width": width,
            "height": height,
            "duration": duration,
            "fps": fps,
            "frame_count": frame_count,
            "mime_type": "video/mp4",
        }
    except Exception as e:
        print(f"Error getting video metadata: {e}")
        return None


def extract_video_frame(video_path: Path, output_path: Path, timestamp: float = 0.0):
    try:
        cmd = [
            "ffmpeg",
            "-ss",
            str(timestamp),
            "-i",
            str(video_path),
            "-vframes",
            "1",
            "-q:v",
            "2",
            "-y",
            str(output_path),
        ]
        subprocess.run(cmd, capture_output=True, check=True)
        return True
    except Exception as e:
        print(f"Error extracting frame: {e}")
        return False


router = APIRouter()


class ImageDetailResponse(SQLModel):
    id: int | None = None
    path: str
    filename: str
    library_path_id: int | None = None
    created_at: datetime
    width: int | None = None
    height: int | None = None
    file_size: int | None = None
    mime_type: str | None = None
    media_type: str = "image"
    duration: float | None = None
    fps: float | None = None
    frame_count: int | None = None
    prompt: str | None = None
    phash: str | None = None
    is_archived: bool = False

    library: LibraryPath | None = None
    parents: List[LocalImage] = []
    children: List[LocalImage] = []


class ImageUpdate(SQLModel):
    is_archived: bool | None = None
    tags: str | None = None
    prompt: str | None = None


@router.post("/check", response_model=dict)
async def check_image(
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
):
    # Save file temporarily to calculate hash
    temp_filename = f"temp_check_{uuid.uuid4()}.png"
    temp_path = Path(settings.DATA_DIR) / "temp" / temp_filename
    temp_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with open(temp_path, "wb") as buffer:
            shutil.copyfileobj(file.file, buffer)

        # Calculate PHash
        with Image.open(temp_path) as img:
            phash = str(imagehash.phash(img))

        # Check for duplicates
        existing = session.exec(
            select(LocalImage).where(LocalImage.phash == phash)
        ).first()

        if existing:
            return {
                "is_duplicate": True,
                "phash": phash,
                "existing_image": {
                    "id": existing.id,
                    "path": existing.path,
                    "filename": existing.filename,
                    "phash": existing.phash,
                },
            }

        return {"is_duplicate": False, "phash": phash}

    except Exception as e:
        print(f"Failed to check image: {e}")
        raise HTTPException(status_code=400, detail="Invalid image file")
    finally:
        if temp_path.exists():
            os.remove(temp_path)


@router.post("/upload", response_model=ImageDetailResponse)
async def upload_image(
    file: UploadFile = File(...),
    prompt: str = Form(None),
    parent_ids: str = Form(None),
    library_id: int = Form(None),
    tags: str = Form(None),
    force: bool = Form(False),
    session: Session = Depends(get_session),
):
    # Find library
    if library_id:
        library = session.get(LibraryPath, library_id)
        if not library:
            raise HTTPException(status_code=404, detail="Library not found")
    else:
        # Find or create default library "Uploads"
        upload_dir = Path(settings.DATA_DIR) / "uploads"
        upload_dir.mkdir(parents=True, exist_ok=True)

        library = session.exec(
            select(LibraryPath).where(LibraryPath.path == str(upload_dir))
        ).first()
        if not library:
            library = LibraryPath(path=str(upload_dir), name="Uploads")
            session.add(library)
            session.commit()
            session.refresh(library)

    # Determine if video
    is_video = file.content_type and file.content_type.startswith("video/")
    ext = Path(file.filename).suffix
    if not ext:
        ext = ".mp4" if is_video else ".png"

    # Save file temporarily
    temp_filename = f"temp_{uuid.uuid4()}{ext}"
    temp_path = Path(settings.DATA_DIR) / "temp" / temp_filename
    temp_path.parent.mkdir(parents=True, exist_ok=True)

    with open(temp_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    width = height = 0
    mime_type = file.content_type
    phash = None
    duration = fps = frame_count = None
    media_type = "video" if is_video else "image"
    temp_thumb_path = None

    try:
        if is_video:
            meta = get_video_metadata(temp_path)
            if meta:
                width = meta["width"]
                height = meta["height"]
                duration = meta["duration"]
                fps = meta["fps"]
                frame_count = meta["frame_count"]
                # mime_type = meta["mime_type"]

            # Generate temp thumbnail for phash and embedding
            temp_thumb_path = temp_path.with_suffix(".jpg")
            if extract_video_frame(
                temp_path, temp_thumb_path, timestamp=duration / 2 if duration else 0
            ):
                try:
                    with Image.open(temp_thumb_path) as img:
                        phash = str(imagehash.phash(img))
                except Exception:
                    pass
        else:
            with Image.open(temp_path) as img:
                phash = str(imagehash.phash(img))
                width, height = img.size
                mime_type = Image.MIME.get(img.format)
    except Exception as e:
        print(f"Failed to process file: {e}")
        if temp_path.exists():
            os.remove(temp_path)
        if temp_thumb_path and temp_thumb_path.exists():
            os.remove(temp_thumb_path)
        raise HTTPException(status_code=400, detail="Invalid file")

    # Check for duplicates
    existing = None
    if phash:
        existing = session.exec(
            select(LocalImage).where(LocalImage.phash == phash)
        ).first()

    if existing:
        if not force:
            if temp_path.exists():
                os.remove(temp_path)
            if temp_thumb_path and temp_thumb_path.exists():
                os.remove(temp_thumb_path)

            # Return 409 Conflict with existing image details
            return JSONResponse(
                status_code=409,
                content={
                    "detail": "Duplicate detected",
                    "existing_image": {
                        "id": existing.id,
                        "path": existing.path,
                        "filename": existing.filename,
                        "phash": existing.phash,
                    },
                },
            )
        else:
            # Force upload: tag as duplicate
            if tags:
                tags += ",duplicate"
            else:
                tags = "duplicate"

    # Move to final location
    filename = f"{uuid.uuid4()}{ext}"
    file_path = Path(library.path) / filename
    shutil.move(temp_path, file_path)

    local_image = LocalImage(
        path=str(file_path),
        filename=filename,
        library_path_id=library.id,
        width=width,
        height=height,
        mime_type=mime_type,
        media_type=media_type,
        duration=duration,
        fps=fps,
        frame_count=frame_count,
        prompt=prompt,
        tags=tags,
        phash=phash,
    )
    session.add(local_image)
    session.commit()
    session.refresh(local_image)

    # Generate embedding (using loaded model or default)
    try:
        image_bytes = None
        if is_video and temp_thumb_path and temp_thumb_path.exists():
            with open(temp_thumb_path, "rb") as f:
                image_bytes = f.read()
        elif not is_video:
            with open(file_path, "rb") as f:
                image_bytes = f.read()

        if image_bytes:
            model_name = get_loaded_model_name() or "openai/clip-vit-base-patch32"
            embedding = get_image_embedding(image_bytes, model_name=model_name)
            store = get_vector_store(model_name=model_name)
            store.add(
                local_image.id,
                embedding,
                {"path": local_image.path, "library_id": library.id},
            )
    except Exception as e:
        print(f"Failed to generate embedding: {e}")

    # Generate thumbnail
    try:
        thumb_dir = Path(settings.THUMBNAILS_DIR) / str(library.id)
        thumb_dir.mkdir(parents=True, exist_ok=True)
        thumb_path = thumb_dir / filename

        if is_video:
            thumb_path = thumb_path.with_suffix(".jpg")
            if temp_thumb_path and temp_thumb_path.exists():
                shutil.move(temp_thumb_path, thumb_path)
            else:
                extract_video_frame(
                    file_path, thumb_path, timestamp=duration / 2 if duration else 0
                )
        else:
            with Image.open(file_path) as img:
                img.thumbnail((300, 300))
                img.save(thumb_path)
    except Exception as e:
        print(f"Failed to generate thumbnail: {e}")
    finally:
        if temp_thumb_path and temp_thumb_path.exists():
            os.remove(temp_thumb_path)

    # Handle parent IDs
    if parent_ids:
        try:
            # parent_ids can be a JSON list or comma-separated string
            try:
                p_ids = json.loads(parent_ids)
            except json.JSONDecodeError:
                p_ids = [
                    int(pid.strip()) for pid in parent_ids.split(",") if pid.strip()
                ]

            if isinstance(p_ids, int):
                p_ids = [p_ids]
            # Make Unique
            p_ids = list(set(p_ids))

            for pid in p_ids:
                try:
                    pid_int = int(pid)
                    lineage = ImageLineage(parent_id=pid_int, child_id=local_image.id)
                    session.add(lineage)
                except (ValueError, TypeError):
                    continue
            session.commit()
            session.refresh(local_image)
        except Exception as e:
            print(f"Failed to process parent IDs: {e}")

    return local_image


@router.post("/search/image", response_model=dict)
async def search_by_image(
    file: UploadFile = File(...),
    limit: int = Form(50),
    skip: int = Form(0),
    library_id: int | None = Form(None),
    collection_id: int | None = Form(None),
    model: str | None = Form(None),
    session: Session = Depends(get_session),
):
    # Use loaded model if available and no specific model requested
    if model is None:
        model = get_loaded_model_name() or "openai/clip-vit-base-patch32"

    # Read image bytes
    image_bytes = await file.read()

    # Generate embedding
    try:
        query_vector = get_image_embedding(image_bytes, model_name=model)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to generate embedding: {e}"
        )

    store = get_vector_store(model_name=model)

    # Prepare filter
    search_filter = None
    if library_id:
        search_filter = {"library_id": library_id}

    allowed_ids = None
    if collection_id:
        allowed_ids = set(
            session.exec(
                select(CollectionItem.image_id).where(
                    CollectionItem.collection_id == collection_id
                )
            ).all()
        )
        if not allowed_ids:
            return {"total": 0, "items": [], "skip": skip, "limit": limit}

    # Search
    results = store.search(
        query_vector,
        limit=limit,
        offset=skip,
        filter=search_filter,
        allowed_ids=allowed_ids,
    )

    ids = [id for id, score in results]

    if not ids:
        return {"total": 0, "items": [], "skip": skip, "limit": limit}

    images = session.exec(select(LocalImage).where(LocalImage.id.in_(ids))).all()
    image_map = {img.id: img for img in images}

    ordered_images = []
    for id in ids:
        if id in image_map:
            ordered_images.append(image_map[id])

    items = [
        ImageDetailResponse(
            **img.model_dump(), parents=img.parents, library=img.library
        )
        for img in ordered_images
    ]

    return {
        "total": len(items),
        "items": items,
        "skip": skip,
        "limit": limit,
    }


@router.get("/tags", response_model=List[dict])
def get_tags(session: Session = Depends(get_session)):
    images = session.exec(select(LocalImage.tags).where(LocalImage.tags != None)).all()
    tag_counts = {}
    for tag_str in images:
        if not tag_str:
            continue
        for tag in tag_str.split(","):
            tag = tag.strip()
            if tag:
                tag_counts[tag] = tag_counts.get(tag, 0) + 1

    return [
        {"name": k, "count": v}
        for k, v in sorted(tag_counts.items(), key=lambda x: x[1], reverse=True)
    ]


@router.get("", response_model=dict)
def list_images(
    session: Session = Depends(get_session),
    skip: int = 0,
    limit: int = 50,
    library_id: int | None = None,
    collection_id: int | None = None,
    sort: str = "new",
    q: str | None = None,
    tags: str | None = Query(None),
    model: str | None = Query(None),
    is_archived: str | None = Query(None),
    media_type: str | None = Query(None),
):
    # Use loaded model if available and no specific model requested
    if model is None:
        model = get_loaded_model_name() or "openai/clip-vit-base-patch32"

    # Parse is_archived
    filter_archived = False
    if is_archived is None:
        filter_archived = False
    else:
        if is_archived.lower() == "true":
            filter_archived = True
        elif is_archived.lower() in ["null", "all", "none"]:
            filter_archived = None
        else:
            filter_archived = False

    if q:
        try:
            query_vector = get_text_embedding(q, model_name=model)
            store = get_vector_store(model_name=model)

            # Prepare filter
            search_filter = None
            if library_id:
                search_filter = {"library_id": library_id}

            allowed_ids = None
            if collection_id:
                allowed_ids = set(
                    session.exec(
                        select(CollectionItem.image_id).where(
                            CollectionItem.collection_id == collection_id
                        )
                    ).all()
                )
                if not allowed_ids:
                    return {"total": 0, "items": [], "skip": skip, "limit": limit}

            results = store.search(
                query_vector,
                limit=limit,
                offset=skip,
                filter=search_filter,
                allowed_ids=allowed_ids,
            )

            ids = [id for id, score in results]

            if not ids:
                return {"total": 0, "items": [], "skip": skip, "limit": limit}

            # Filter by is_archived manually since vector store might not support it yet or we want to filter after retrieval
            # Ideally vector store should support it, but for now let's filter in SQL

            query = select(LocalImage).where(LocalImage.id.in_(ids))

            if filter_archived is not None:
                query = query.where(LocalImage.is_archived == filter_archived)

            if media_type:
                query = query.where(LocalImage.media_type == media_type)

            images = session.exec(
                query.options(
                    selectinload(LocalImage.parents), selectinload(LocalImage.library)
                )
            ).all()
            image_map = {img.id: img for img in images}

            ordered_images = []
            for id in ids:
                if id in image_map:
                    ordered_images.append(image_map[id])

            items = [
                ImageDetailResponse(
                    **img.model_dump(), parents=img.parents, library=img.library
                )
                for img in ordered_images
            ]

            return {
                "total": len(items),  # Approximate
                "items": items,
                "skip": skip,
                "limit": limit,
            }
        except Exception as e:
            print(f"Search error: {e}")
            return {"total": 0, "items": [], "skip": skip, "limit": limit}

    query = select(LocalImage).options(
        selectinload(LocalImage.parents), selectinload(LocalImage.library)
    )
    count_query = select(func.count(LocalImage.id))

    # Filter by is_archived
    if filter_archived is not None:
        query = query.where(LocalImage.is_archived == filter_archived)
        count_query = count_query.where(LocalImage.is_archived == filter_archived)

    if media_type:
        query = query.where(LocalImage.media_type == media_type)
        count_query = count_query.where(LocalImage.media_type == media_type)

    if library_id:
        query = query.where(LocalImage.library_path_id == library_id)
        count_query = count_query.where(LocalImage.library_path_id == library_id)

    if collection_id:
        query = query.join(CollectionItem).where(
            CollectionItem.collection_id == collection_id
        )
        count_query = (
            select(func.count())
            .select_from(LocalImage)
            .join(CollectionItem)
            .where(CollectionItem.collection_id == collection_id)
        )

    if tags:
        tag_list = [t.strip() for t in tags.split(",")]
        for tag in tag_list:
            query = query.where(LocalImage.tags.contains(tag))
            count_query = count_query.where(LocalImage.tags.contains(tag))

    if sort == "new":
        query = query.order_by(LocalImage.created_at.desc())
    elif sort == "random":
        query = query.order_by(func.random())

    # Get total count
    total = session.exec(count_query).one()

    # Get items
    images = session.exec(query.offset(skip).limit(limit)).all()

    items = [
        ImageDetailResponse(
            **img.model_dump(), parents=img.parents, library=img.library
        )
        for img in images
    ]

    return {"total": total, "items": items, "skip": skip, "limit": limit}


@router.patch("/{image_id}", response_model=ImageDetailResponse)
def update_image(
    image_id: int, update_data: ImageUpdate, session: Session = Depends(get_session)
):
    image = session.get(LocalImage, image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")

    if update_data.is_archived is not None:
        image.is_archived = update_data.is_archived
    if update_data.tags is not None:
        image.tags = update_data.tags
    if update_data.prompt is not None:
        image.prompt = update_data.prompt

    session.add(image)
    session.commit()
    session.refresh(image)

    return ImageDetailResponse(
        **image.model_dump(), parents=image.parents, library=image.library
    )


@router.get("/file")
def get_file_by_path(path: str):
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path)


@router.get("/{image_id}", response_model=ImageDetailResponse)
def get_image(image_id: int, session: Session = Depends(get_session)):
    image = session.get(LocalImage, image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")

    # Fetch lineage
    parent_links = session.exec(
        select(ImageLineage).where(ImageLineage.child_id == image_id)
    ).all()
    parent_ids = [p.parent_id for p in parent_links]
    parents = []
    if parent_ids:
        parents = session.exec(
            select(LocalImage).where(LocalImage.id.in_(parent_ids))
        ).all()

    child_links = session.exec(
        select(ImageLineage).where(ImageLineage.parent_id == image_id)
    ).all()
    child_ids = [c.child_id for c in child_links]
    children = []
    if child_ids:
        children = session.exec(
            select(LocalImage).where(LocalImage.id.in_(child_ids))
        ).all()

    image_dict = image.model_dump()
    return ImageDetailResponse(
        **image_dict, parents=parents, children=children, library=image.library
    )


@router.get("/{image_id}/file")
def get_image_file(image_id: int, session: Session = Depends(get_session)):
    image = session.get(LocalImage, image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")

    if not os.path.exists(image.path):
        raise HTTPException(status_code=404, detail="Image file not found on disk")

    return FileResponse(image.path)


@router.get("/{image_id}/thumbnail")
def get_image_thumbnail(image_id: int, session: Session = Depends(get_session)):
    image = session.get(LocalImage, image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")

    library = session.get(LibraryPath, image.library_path_id)
    if not library:
        # Fallback to original if library not found (shouldn't happen)
        if os.path.exists(image.path):
            return FileResponse(image.path)
        raise HTTPException(status_code=404, detail="Image file not found")

    try:
        rel_path = Path(image.path).relative_to(Path(library.path))
        thumb_path = Path(settings.THUMBNAILS_DIR) / str(library.id) / rel_path

        if thumb_path.exists():
            return FileResponse(thumb_path)

        # Check for .jpg version (for videos or if extension changed)
        thumb_path_jpg = thumb_path.with_suffix(".jpg")
        if thumb_path_jpg.exists():
            return FileResponse(thumb_path_jpg)

    except ValueError:
        pass

    # Fallback to original
    if os.path.exists(image.path):
        return FileResponse(image.path)

    raise HTTPException(status_code=404, detail="Image file not found")


@router.get("/{image_id}/similar", response_model=dict)
def get_similar_images(
    image_id: int,
    limit: int = 50,
    skip: int = 0,
    library_id: int | None = None,
    collection_id: int | None = None,
    model: str | None = Query(None),
    is_archived: str | None = Query(None),
    media_type: str | None = Query(None),
    session: Session = Depends(get_session),
):
    # Use loaded model if available and no specific model requested
    if model is None:
        model = get_loaded_model_name() or "openai/clip-vit-base-patch32"

    # Parse is_archived
    filter_archived = False
    if is_archived is None:
        filter_archived = False
    else:
        if is_archived.lower() == "true":
            filter_archived = True
        elif is_archived.lower() in ["null", "all", "none"]:
            filter_archived = None
        else:
            filter_archived = False

    store = get_vector_store(model_name=model)
    vector = store.get_vector_by_id(image_id)

    if vector is None:
        return {"total": 0, "items": [], "skip": skip, "limit": limit}

    # Prepare filter
    search_filter = None
    if library_id:
        search_filter = {"library_id": library_id}

    allowed_ids = None
    if collection_id:
        allowed_ids = set(
            session.exec(
                select(CollectionItem.image_id).where(
                    CollectionItem.collection_id == collection_id
                )
            ).all()
        )
        if not allowed_ids:
            return {"total": 0, "items": [], "skip": skip, "limit": limit}

    # Search
    # We might need to fetch more results from vector store to account for filtering
    # But for now let's just fetch limit * 2 to have some buffer
    results = store.search(
        vector,
        limit=limit * 2,
        offset=skip,
        filter=search_filter,
        allowed_ids=allowed_ids,
    )

    ids = [id for id, score in results]

    if not ids:
        return {"total": 0, "items": [], "skip": skip, "limit": limit}

    query = select(LocalImage).where(LocalImage.id.in_(ids))

    if filter_archived is not None:
        query = query.where(LocalImage.is_archived == filter_archived)

    if media_type:
        query = query.where(LocalImage.media_type == media_type)

    images = session.exec(query).all()
    image_map = {img.id: img for img in images}

    ordered_images = []
    for id in ids:
        if id in image_map:
            ordered_images.append(image_map[id])
            if len(ordered_images) >= limit:
                break

    return {
        "total": len(ordered_images),
        "items": ordered_images,
        "skip": skip,
        "limit": limit,
    }
