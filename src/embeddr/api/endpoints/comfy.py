from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from embeddr.services.comfy import ComfyClient
from typing import Optional
import os

router = APIRouter()


class UploadFromPathRequest(BaseModel):
    path: str
    filename: Optional[str] = None
    overwrite: bool = False


@router.post("/upload-from-path")
def upload_image_from_path(req: UploadFromPathRequest):
    """
    Upload an image from a local file path to ComfyUI's input directory.
    """
    if not os.path.exists(req.path):
        raise HTTPException(status_code=404, detail=f"File not found at {req.path}")

    try:
        with open(req.path, "rb") as f:
            image_bytes = f.read()

        final_filename = req.filename or os.path.basename(req.path)

        client = ComfyClient()
        if not client.is_available():
            raise HTTPException(status_code=503, detail="ComfyUI is not available")

        result = client.upload_image(image_bytes, final_filename, req.overwrite)
        return result
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/view")
def view_image(filename: str, subfolder: str = "", type: str = "output"):
    """
    Proxy to view an image from ComfyUI.
    """
    client = ComfyClient()
    # Construct the URL to ComfyUI view endpoint
    # ComfyUI URL: http://host:port/view?filename=...&subfolder=...&type=...

    url = f"{client.url}/view?filename={filename}&subfolder={subfolder}&type={type}"

    import requests
    from fastapi.responses import StreamingResponse

    try:
        resp = requests.get(url, stream=True)
        resp.raise_for_status()
        return StreamingResponse(
            resp.iter_content(chunk_size=8192),
            media_type=resp.headers.get("content-type"),
        )
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to fetch image from ComfyUI: {e}"
        )


@router.get("/object_info")
async def get_object_info():
    """
    Get ComfyUI object info (node definitions).
    """
    from embeddr.services.comfy import AsyncComfyClient

    client = AsyncComfyClient()
    if not await client.is_available():
        raise HTTPException(status_code=503, detail="ComfyUI is not available")

    return await client.get_object_info()
