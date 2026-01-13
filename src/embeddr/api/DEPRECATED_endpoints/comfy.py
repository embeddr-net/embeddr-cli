from fastapi import APIRouter, HTTPException, Query
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
        raise HTTPException(
            status_code=404, detail=f"File not found at {req.path}")

    try:
        with open(req.path, "rb") as f:
            image_bytes = f.read()

        final_filename = req.filename or os.path.basename(req.path)

        client = ComfyClient()
        if not client.is_available():
            raise HTTPException(
                status_code=503, detail="ComfyUI is not available")

        result = client.upload_image(
            image_bytes, final_filename, req.overwrite)
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


@router.get("/loras")
async def get_loras(page: int = Query(1, ge=1), limit: int = Query(60, ge=1)):
    """
    Get list of available LoRAs from ComfyUI.
    """
    from embeddr.services.comfy import AsyncComfyClient

    client = AsyncComfyClient()
    if not await client.is_available():
        raise HTTPException(status_code=503, detail="ComfyUI is not available")

    info = await client.get_object_info()

    # Try to find LoraLoader node
    lora_node = info.get("LoraLoader")
    if not lora_node:
        # Fallback to other common nodes if LoraLoader is missing (unlikely)
        lora_node = info.get("LoraLoaderModelOnly")

    loras = []
    if lora_node:
        try:
            # Input format: {"required": {"lora_name": [["file1", "file2"], ...]}}
            loras = lora_node["input"]["required"]["lora_name"][0]
        except (KeyError, IndexError):
            pass

    total = len(loras)
    start = (page - 1) * limit
    end = start + limit
    items = loras[start:end]

    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit
    }


@router.get("/checkpoints")
async def get_checkpoints(page: int = Query(1, ge=1), limit: int = Query(60, ge=1)):
    """
    Get list of available Checkpoints from ComfyUI.
    """
    from embeddr.services.comfy import AsyncComfyClient

    client = AsyncComfyClient()
    if not await client.is_available():
        raise HTTPException(status_code=503, detail="ComfyUI is not available")

    info = await client.get_object_info()

    ckpt_node = info.get("CheckpointLoaderSimple")
    if not ckpt_node:
        ckpt_node = info.get("CheckpointLoader")

    ckpts = []
    if ckpt_node:
        try:
            ckpts = ckpt_node["input"]["required"]["ckpt_name"][0]
        except (KeyError, IndexError):
            pass

    total = len(ckpts)
    start = (page - 1) * limit
    end = start + limit
    items = ckpts[start:end]

    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit
    }


@router.get("/embeddings")
async def get_embeddings(page: int = Query(1, ge=1), limit: int = Query(60, ge=1)):
    """
    Get list of available Embeddings from ComfyUI.
    """
    from embeddr.services.comfy import AsyncComfyClient

    client = AsyncComfyClient()
    if not await client.is_available():
        raise HTTPException(status_code=503, detail="ComfyUI is not available")

    # ComfyUI has a specific endpoint for embeddings usually, but let's check object_info first
    # Actually, embeddings are often just files in the embeddings folder,
    # but they are not always listed in a node input like LoRAs.
    # However, ComfyUI has a /embeddings endpoint.

    embeddings = []
    try:
        resp = await client.client.get("/embeddings")
        if resp.status_code == 200:
            embeddings = resp.json()
    except Exception:
        pass

    total = len(embeddings)
    start = (page - 1) * limit
    end = start + limit
    items = embeddings[start:end]

    return {
        "items": items,
        "total": total,
        "page": page,
        "limit": limit,
        "pages": (total + limit - 1) // limit
    }


@router.get("/samplers")
async def get_samplers():
    """
    Get list of available Samplers from ComfyUI.
    """
    from embeddr.services.comfy import AsyncComfyClient

    client = AsyncComfyClient()
    if not await client.is_available():
        raise HTTPException(status_code=503, detail="ComfyUI is not available")

    info = await client.get_object_info()

    sampler_node = info.get("KSampler")

    if sampler_node:
        try:
            samplers = sampler_node["input"]["required"]["sampler_name"][0]
            schedulers = sampler_node["input"]["required"]["scheduler"][0]
            return {"samplers": samplers, "schedulers": schedulers}
        except (KeyError, IndexError):
            pass

    return {"samplers": [], "schedulers": []}


# @router.get("/queue")
# async def get_queue():
#     """
#     Get current queue status from ComfyUI.
#     """
#     from embeddr.services.comfy import AsyncComfyClient

#     client = AsyncComfyClient()
#     if not await client.is_available():
#         raise HTTPException(status_code=503, detail="ComfyUI is not available")

#     return await client.get_queue()
