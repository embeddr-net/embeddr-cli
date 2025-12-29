from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlmodel import Session, select, func
from embeddr.db.session import get_session
from embeddr_core.models.library import LibraryPath, LocalImage
from embeddr_core.services.scanner import scan_all_libraries
from embeddr_core.services.thumbnails import generate_thumbnails_for_library
from embeddr_core.services.embedding_manager import generate_embeddings_for_library
from embeddr.core.config import settings
from pathlib import Path
from pydantic import BaseModel

router = APIRouter()


class PathCreate(BaseModel):
    path: str
    name: str | None = None
    create_if_missing: bool = False


class PathUpdate(BaseModel):
    name: str | None = None


class LibraryPathWithCount(BaseModel):
    id: int
    path: str
    name: str | None
    image_count: int


@router.post("/paths", response_model=LibraryPath)
def add_library_path(path_data: PathCreate, session: Session = Depends(get_session)):
    # Check if path exists on disk
    import os

    if not os.path.exists(path_data.path):
        if path_data.create_if_missing:
            try:
                os.makedirs(path_data.path, exist_ok=True)
            except Exception as e:
                raise HTTPException(
                    status_code=500, detail=f"Failed to create directory: {e}"
                )
        else:
            raise HTTPException(status_code=400, detail="Path does not exist on server")

    # Check if already exists in DB
    existing = session.exec(
        select(LibraryPath).where(LibraryPath.path == path_data.path)
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="Path already added")

    library_path = LibraryPath(path=path_data.path, name=path_data.name)
    session.add(library_path)
    session.commit()
    session.refresh(library_path)
    return library_path


@router.get("/paths", response_model=List[LibraryPathWithCount])
def list_library_paths(session: Session = Depends(get_session)):
    statement = (
        select(LibraryPath, func.count(LocalImage.id))
        .outerjoin(LocalImage)
        .group_by(LibraryPath.id)
    )
    results = session.exec(statement).all()

    return [
        LibraryPathWithCount(id=lib.id, path=lib.path, name=lib.name, image_count=count)
        for lib, count in results
    ]


@router.delete("/paths/{path_id}")
def remove_library_path(path_id: int, session: Session = Depends(get_session)):
    path = session.get(LibraryPath, path_id)
    if not path:
        raise HTTPException(status_code=404, detail="Path not found")
    session.delete(path)
    session.commit()
    return {"ok": True}


@router.put("/paths/{path_id}", response_model=LibraryPath)
def update_library_path(
    path_id: int, path_data: PathUpdate, session: Session = Depends(get_session)
):
    path = session.get(LibraryPath, path_id)
    if not path:
        raise HTTPException(status_code=404, detail="Path not found")

    if path_data.name is not None:
        path.name = path_data.name

    session.add(path)
    session.commit()
    session.refresh(path)
    return path


@router.post("/paths/{path_id}/thumbnails")
def generate_thumbnails(path_id: int, session: Session = Depends(get_session)):
    path = session.get(LibraryPath, path_id)
    if not path:
        raise HTTPException(status_code=404, detail="Path not found")

    # Output dir is THUMBNAILS_DIR / library_id
    output_dir = Path(settings.THUMBNAILS_DIR) / str(path.id)

    count = generate_thumbnails_for_library(
        library_path=Path(path.path), output_dir=output_dir
    )

    return {"message": "Thumbnail generation complete", "generated": count}


@router.post("/paths/{path_id}/embeddings")
def generate_embeddings(
    path_id: int,
    model: str | None = Query(None),
    batch_size: int = 10,
    session: Session = Depends(get_session),
):
    path = session.get(LibraryPath, path_id)
    if not path:
        raise HTTPException(status_code=404, detail="Path not found")

    from embeddr_core.services.job_manager import job_manager
    from embeddr_core.services.embedding import get_loaded_model_name

    # Use loaded model if available and no specific model requested
    if model is None:
        model = get_loaded_model_name() or "openai/clip-vit-base-patch32"

    # We need to pass a new session to the thread because the dependency session is closed after request
    # So we'll pass the session factory or handle session creation inside the job
    # For simplicity, let's assume generate_embeddings_for_library can handle its own session if we pass the engine/factory
    # But wait, generate_embeddings_for_library takes a session.
    # We should wrap it to create a new session.

    from embeddr.db.session import get_engine
    from sqlmodel import Session as SQLSession

    def job_wrapper(library_id, model_name, batch_size, stop_event, progress_callback):
        with SQLSession(get_engine()) as job_session:
            return generate_embeddings_for_library(
                job_session,
                library_id,
                model_name=model_name,
                batch_size=batch_size,
                stop_event=stop_event,
                progress_callback=progress_callback,
            )

    try:
        job_id = job_manager.start_job(
            "embedding_generation",
            job_wrapper,
            library_id=path.id,
            model_name=model,
            batch_size=batch_size,
        )
        return {"message": "Embedding generation started", "job_id": job_id}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.post("/scan")
def scan_workspace(session: Session = Depends(get_session)):
    results = scan_all_libraries(session)
    total_added = sum(results.values())
    return {"message": "Scan complete", "added": total_added, "details": results}
