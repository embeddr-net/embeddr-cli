from typing import List, Dict, Any
import os
import importlib.metadata

from fastapi import APIRouter, HTTPException, Query, Depends
from sqlmodel import Session, select, func

from embeddr_core.services.embedding import (
    get_loaded_model_name,
    load_model,
    unload_model,
)
from embeddr_core.models.library import LocalImage, LibraryPath

from embeddr.core.logging_utils import get_logs
from embeddr.core.config_manager import config_manager, AppConfig
from embeddr.core.config import settings
from embeddr.db.session import get_engine

router = APIRouter()


def get_session():
    with Session(get_engine()) as session:
        yield session


@router.get("/status")
def get_system_status():
    """
    Get the status of system services.
    """
    return {
        "mcp": os.environ.get("EMBEDDR_ENABLE_MCP", "false").lower() == "true",
        "comfy": os.environ.get("EMBEDDR_ENABLE_COMFY", "false").lower() == "true",
        "docs": os.environ.get("EMBEDDR_ENABLE_DOCS", "false").lower() == "true",
    }


@router.get("/info")
def get_system_info(session: Session = Depends(get_session)):
    """
    Get system information including version, stats, etc.
    """
    try:
        version = importlib.metadata.version("embeddr-cli")
    except importlib.metadata.PackageNotFoundError:
        version = "unknown"

    # Stats
    image_count = session.exec(select(func.count(LocalImage.id))).one()
    library_count = session.exec(select(func.count(LibraryPath.id))).one()

    return {
        "version": version,
        "dev_mode": settings.DEV_MODE,
        "stats": {
            "images": image_count,
            "libraries": library_count,
        },
        "db_version": "v1",  # Placeholder or fetch from alembic_version table
    }


@router.get("/config", response_model=AppConfig)
def get_config():
    """
    Get current configuration.
    """
    return config_manager.config


@router.patch("/config", response_model=AppConfig)
def update_config(config_update: Dict[str, Any]):
    """
    Update configuration.
    """
    config_manager.update(config_update)
    return config_manager.config


@router.get("/logs", response_model=List[str])
async def get_system_logs(
    limit: int = Query(100, ge=1, le=1000),
    filter: str | None = Query(None, description="Filter logs by string"),
):
    """
    Get the latest system logs.
    """
    return get_logs(limit, include_filter=filter)


@router.get("/models", response_model=List[dict])
def get_available_models():
    """
    Get list of available CLIP models with their status.
    """
    loaded_model = get_loaded_model_name()

    models = [
        {"id": "laion/CLIP-ViT-bigG-14-laion2B-39B-b160k", "name": "ViT-BigG-14"},
        {"id": "laion/CLIP-ViT-g-14-laion2B-s34B-b88K", "name": "ViT-G-14"},
        {"id": "openai/clip-vit-base-patch32", "name": "ViT-Base-32"},
        {"id": "openai/clip-vit-base-patch16", "name": "ViT-Base-16"},
        {"id": "openai/clip-vit-large-patch14", "name": "ViT-Large-14"},
    ]

    for m in models:
        m["loaded"] = m["id"] == loaded_model

    return models


@router.post("/models/unload")
def unload_current_model():
    """
    Unload the currently loaded model to free up memory.
    """
    unload_model()
    return {"status": "success", "message": "Model unloaded"}


@router.post("/models/{model_id:path}/load")
def load_specific_model(model_id: str):
    """
    Load a specific model.
    """
    try:
        load_model(model_id)
        return {"status": "success", "message": f"Model {model_id} loaded"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
