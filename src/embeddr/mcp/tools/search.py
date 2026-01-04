import base64
from sqlmodel import select
from embeddr.mcp.instance import mcp
from embeddr.mcp.utils import get_db_session
from embeddr_core.models.library import LocalImage
from embeddr_core.services.vector_store import get_vector_store
from embeddr_core.services.embedding import (
    get_text_embedding,
    get_image_embedding,
    get_loaded_model_name,
)


@mcp.tool()
def search_images(query: str, limit: int = 5) -> list[dict | str]:
    """
    Search for images using natural language query.
    Returns a list of images with their ID, path, prompt, and similarity score.
    """
    model_name = get_loaded_model_name() or "openai/clip-vit-base-patch32"
    try:
        query_vector = get_text_embedding(query, model_name=model_name)
        store = get_vector_store(model_name=model_name)
        results = store.search(query_vector, limit=limit)

        ids = [id for id, score in results]
        if not ids:
            return []

        with get_db_session() as session:
            images = session.exec(
                select(LocalImage).where(LocalImage.id.in_(ids))
            ).all()
            # Map scores to images
            score_map = {id: score for id, score in results}

            return [
                {
                    "id": img.id,
                    "path": img.path,
                    "prompt": img.prompt,
                    "score": score_map.get(img.id, 0),
                }
                for img in images
            ]
    except Exception as e:
        return [f"Error searching images: {str(e)}"]


@mcp.tool()
def search_by_image_id(image_id: int, limit: int = 5) -> list[dict | str]:
    """
    Search for similar images using an existing image ID.
    Returns a list of similar images with their ID, path, prompt, and similarity score.
    """
    model_name = get_loaded_model_name() or "openai/clip-vit-base-patch32"
    try:
        store = get_vector_store(model_name=model_name)
        vector = store.get_vector_by_id(image_id)

        if vector is None:
            return [f"Image with ID {image_id} not found in vector store"]

        results = store.search(vector, limit=limit)

        ids = [id for id, score in results]
        if not ids:
            return []

        with get_db_session() as session:
            images = session.exec(
                select(LocalImage).where(LocalImage.id.in_(ids))
            ).all()
            score_map = {id: score for id, score in results}

            return [
                {
                    "id": img.id,
                    "path": img.path,
                    "prompt": img.prompt,
                    "score": score_map.get(img.id, 0),
                }
                for img in images
            ]
    except Exception as e:
        return [f"Error searching by image ID: {str(e)}"]


@mcp.tool()
def search_by_image_upload(image_base64: str, limit: int = 5) -> list[dict | str]:
    """
    Search for similar images by uploading a base64 encoded image.
    Returns a list of similar images with their ID, path, prompt, and similarity score.
    """
    model_name = get_loaded_model_name() or "openai/clip-vit-base-patch32"
    try:
        # Decode base64 image
        image_bytes = base64.b64decode(image_base64)

        # Generate embedding
        embedding = get_image_embedding(image_bytes, model_name=model_name)

        store = get_vector_store(model_name=model_name)
        results = store.search(embedding, limit=limit)

        ids = [id for id, score in results]
        if not ids:
            return []

        with get_db_session() as session:
            images = session.exec(
                select(LocalImage).where(LocalImage.id.in_(ids))
            ).all()
            score_map = {id: score for id, score in results}

            return [
                {
                    "id": img.id,
                    "path": img.path,
                    "prompt": img.prompt,
                    "score": score_map.get(img.id, 0),
                }
                for img in images
            ]
    except Exception as e:
        return [f"Error searching by uploaded image: {str(e)}"]
