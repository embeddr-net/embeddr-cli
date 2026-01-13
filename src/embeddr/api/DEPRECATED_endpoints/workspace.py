from pathlib import Path
from typing import List

from embeddr_core.models.library import LibraryPath, LocalImage
from embeddr_core.services.embedding_manager import generate_embeddings_for_library
from embeddr_core.services.scanner import scan_all_libraries
from embeddr_core.services.thumbnails import generate_thumbnails_for_library
from fastapi import APIRouter, Depends, HTTPException, Query, Path as FastAPIPath
from pydantic import BaseModel, Field
from sqlmodel import Session, func, select

from embeddr.core.config import settings
from embeddr.db.session import get_session

router = APIRouter()


class PathCreate(BaseModel):
    path: str = Field(...,
                      description="The absolute file system path to the directory to be added.")
    name: str | None = Field(
        None, description="A display name for the library. If not provided, the folder name will be used.")
    create_if_missing: bool = Field(
        False, description="If True, the system will attempt to create the directory if it does not exist.")


class PathUpdate(BaseModel):
    name: str | None = Field(
        None, description="The new display name for the library.")


class LibraryPathWithCount(BaseModel):
    id: int = Field(...,
                    description="The unique identifier of the library path.")
    path: str = Field(..., description="The absolute file system path.")
    name: str | None = Field(
        None, description="The display name of the library.")
    image_count: int = Field(...,
                             description="The total number of images indexed in this library.")


@router.post(
    "/paths",
    response_model=LibraryPath,
    summary="Add Library Path",
    description="Register a new local directory as a library path. If 'create_if_missing' is true, the directory will be created.",
)
def add_library_path(
    path_data: PathCreate,
    session: Session = Depends(get_session),
):
    """
    Registers a new directory path to be scanned for images.
    """

    import os

    # If path is just a name (no slashes), assume it's a new library in the data dir
    if "/" not in path_data.path and "\\" not in path_data.path:
        # It's a name, create it in DATA_DIR/libraries
        lib_dir = Path(settings.DATA_DIR) / "libraries" / path_data.path
        path_data.path = str(lib_dir)
        path_data.create_if_missing = True
        if not path_data.name:
            path_data.name = lib_dir.name

    if not os.path.exists(path_data.path):
        if path_data.create_if_missing:
            try:
                os.makedirs(path_data.path, exist_ok=True)
            except Exception as e:
                raise HTTPException(
                    status_code=500, detail=f"Failed to create directory: {e}"
                )
        else:
            raise HTTPException(
                status_code=400, detail="Path does not exist on server")

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


@router.get(
    "/paths",
    response_model=List[LibraryPathWithCount],
    summary="List Library Paths",
    description="Retrieve all registered library paths along with the count of images indexed in each path.",
)
def list_library_paths(session: Session = Depends(get_session)):
    statement = (
        select(LibraryPath, func.count(LocalImage.id))
        .outerjoin(LocalImage)
        .group_by(LibraryPath.id)
    )
    results = session.exec(statement).all()

    return [
        LibraryPathWithCount(id=lib.id, path=lib.path,
                             name=lib.name, image_count=count)
        for lib, count in results
    ]


@router.delete(
    "/paths/{path_id}",
    summary="Remove Library Path",
    description="Unregister a library path from the database. This does not delete the files on disk.",
)
def remove_library_path(
    path_id: int = FastAPIPath(...,
                               description="The ID of the library path to remove"),
    session: Session = Depends(get_session),
):
    path = session.get(LibraryPath, path_id)
    if not path:
        raise HTTPException(status_code=404, detail="Path not found")
    session.delete(path)
    session.commit()
    return {"ok": True}


@router.put(
    "/paths/{path_id}",
    response_model=LibraryPath,
    summary="Update Library Path",
    description="Update the details (e.g., name) of an existing library path.",
)
def update_library_path(
    path_id: int = FastAPIPath(...,
                               description="The ID of the library path to update"),
    path_data: PathUpdate = ...,
    session: Session = Depends(get_session),
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


@router.post(
    "/paths/{path_id}/thumbnails",
    summary="Generate Thumbnails",
    description="Trigger a background job to generate thumbnails for all images in the specified library. Useful if automatic generation failed or for cache warming.",
)
def generate_thumbnails(
    path_id: int = FastAPIPath(...,
                               description="The ID of the library to generate thumbnails for"),
    session: Session = Depends(get_session),
):
    path = session.get(LibraryPath, path_id)
    if not path:
        raise HTTPException(status_code=404, detail="Path not found")

    # Output dir is THUMBNAILS_DIR / library_id
    output_dir = Path(settings.THUMBNAILS_DIR) / str(path.id)

    count = generate_thumbnails_for_library(
        library_path=Path(path.path), output_dir=output_dir
    )

    return {"message": "Thumbnail generation complete", "generated": count}


@router.post(
    "/paths/{path_id}/embeddings",
    summary="Generate Embeddings for Library",
    description="Trigger a background job to calculate CLIP embeddings for all images in the specified library path. These embeddings are stored in the vector database to enable semantic search capabilities.",
)
def generate_embeddings(
    path_id: int = FastAPIPath(...,
                               description="The unique ID of the library path to process"),
    model: str | None = Query(
        None,
        description="The specific CLIP model to use for embedding generation. If left empty, the system default or currently loaded model will be used.",
    ),
    batch_size: int = Query(
        10,
        description="The number of images to embed in a single batch. Higher values use more VRAM but are generally faster.",
    ),
    session: Session = Depends(get_session),
):
    path = session.get(LibraryPath, path_id)
    if not path:
        raise HTTPException(status_code=404, detail="Path not found")

    from embeddr_core.services.embedding import get_loaded_model_name
    from embeddr_core.services.job_manager import job_manager

    # Use loaded model if available and no specific model requested
    if model is None:
        model = get_loaded_model_name() or "openai/clip-vit-base-patch32"

    # We need to pass a new session to the thread because the dependency session is closed after request
    # So we'll pass the session factory or handle session creation inside the job
    # For simplicity, let's assume generate_embeddings_for_library can handle its own session if we pass the engine/factory
    # But wait, generate_embeddings_for_library takes a session.
    # We should wrap it to create a new session.

    from sqlmodel import Session as SQLSession

    from embeddr.db.session import get_engine

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
