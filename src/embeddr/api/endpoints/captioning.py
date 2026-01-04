from fastapi import APIRouter, HTTPException
from embeddr.services.captioning import (
    get_captioning_models,
    get_loaded_model_name,
    load_model,
    unload_model,
)
from pydantic import BaseModel

router = APIRouter()


class LoadModelRequest(BaseModel):
    model_name: str


@router.get("/models")
def list_captioning_models():
    """
    Get a list of available captioning models and their capabilities.
    """
    return get_captioning_models()


@router.get("/status")
def get_model_status():
    """
    Get the currently loaded captioning model.
    """
    return {"loaded_model": get_loaded_model_name()}


@router.post("/load")
def load_captioning_model(request: LoadModelRequest):
    """
    Load a specific captioning model.
    """
    try:
        load_model(request.model_name)
        return {
            "message": f"Model {request.model_name} loaded successfully",
            "loaded_model": request.model_name,
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/unload")
def unload_captioning_model():
    """
    Unload the currently loaded captioning model.
    """
    unload_model()
    return {"message": "Model unloaded successfully"}
