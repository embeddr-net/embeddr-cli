from fastapi import APIRouter, Query, HTTPException
from typing import List
from embeddr.core.logging_utils import get_logs
from embeddr_core.services.embedding import (
    get_loaded_model_name,
    load_model,
    unload_model,
)

router = APIRouter()


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
