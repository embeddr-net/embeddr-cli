from fastapi import APIRouter, Depends, Query, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from sqlmodel import Session, select, func
from embeddr.db.session import get_session
from embeddr_core.models.library import LocalImage, LibraryPath
from embeddr_core.models.collection import CollectionItem
from embeddr.core.config import settings
from pathlib import Path
import os
import shutil
import uuid
from PIL import Image
from embeddr_core.services.vector_store import get_vector_store
from embeddr_core.services.embedding import (
    get_text_embedding,
    get_image_embedding,
    get_loaded_model_name,
)

router = APIRouter()


@router.post("/upload", response_model=LocalImage)
async def upload_image(
    file: UploadFile = File(...),
    prompt: str = Form(None),
    session: Session = Depends(get_session),
):
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

    # Save file
    filename = f"{uuid.uuid4()}.png"
    file_path = Path(library.path) / filename

    with open(file_path, "wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    # Get metadata
    try:
        with Image.open(file_path) as img:
            width, height = img.size
            mime_type = Image.MIME.get(img.format)
    except:
        width = height = None
        mime_type = "image/png"

    local_image = LocalImage(
        path=str(file_path),
        filename=filename,
        library_path_id=library.id,
        width=width,
        height=height,
        mime_type=mime_type,
        prompt=prompt,
    )
    session.add(local_image)
    session.commit()
    session.refresh(local_image)

    # Generate embedding (using loaded model or default)
    try:
        with open(file_path, "rb") as f:
            image_bytes = f.read()

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

        with Image.open(file_path) as img:
            img.thumbnail((300, 300))
            img.save(thumb_path)
    except Exception as e:
        print(f"Failed to generate thumbnail: {e}")

    return local_image


@router.get("", response_model=dict)
def list_images(
    session: Session = Depends(get_session),
    skip: int = 0,
    limit: int = 50,
    library_id: int | None = None,
    collection_id: int | None = None,
    sort: str = "new",
    q: str | None = None,
    model: str | None = Query(None),
):
    # Use loaded model if available and no specific model requested
    if model is None:
        model = get_loaded_model_name() or "openai/clip-vit-base-patch32"

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

            images = session.exec(
                select(LocalImage).where(LocalImage.id.in_(ids))
            ).all()
            image_map = {img.id: img for img in images}

            ordered_images = []
            for id in ids:
                if id in image_map:
                    ordered_images.append(image_map[id])

            return {
                "total": len(ordered_images),  # Approximate
                "items": ordered_images,
                "skip": skip,
                "limit": limit,
            }
        except Exception as e:
            print(f"Search error: {e}")
            return {"total": 0, "items": [], "skip": skip, "limit": limit}

    query = select(LocalImage)
    count_query = select(func.count(LocalImage.id))

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

    if sort == "new":
        query = query.order_by(LocalImage.created_at.desc())
    elif sort == "random":
        query = query.order_by(func.random())

    # Get total count
    total = session.exec(count_query).one()

    # Get items
    images = session.exec(query.offset(skip).limit(limit)).all()

    return {"total": total, "items": images, "skip": skip, "limit": limit}


@router.get("/{image_id}", response_model=LocalImage)
def get_image(image_id: int, session: Session = Depends(get_session)):
    image = session.get(LocalImage, image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")
    return image


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
    session: Session = Depends(get_session),
):
    # Use loaded model if available and no specific model requested
    if model is None:
        model = get_loaded_model_name() or "openai/clip-vit-base-patch32"

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
    results = store.search(
        vector, limit=limit, offset=skip, filter=search_filter, allowed_ids=allowed_ids
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

    return {
        "total": len(ordered_images),
        "items": ordered_images,
        "skip": skip,
        "limit": limit,
    }
