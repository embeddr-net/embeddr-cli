from fastmcp import FastMCP
from sqlmodel import Session, select
from embeddr.db.session import get_engine
from embeddr_core.models.library import LocalImage, LibraryPath
from embeddr_core.models.collection import Collection, CollectionItem
from embeddr_core.services.vector_store import get_vector_store
from embeddr_core.services.embedding import (
    get_text_embedding,
    get_image_embedding,
    get_loaded_model_name,
)
import base64

# Initialize FastMCP server
mcp = FastMCP("Embeddr")


def get_db_session():
    """Helper to get a new database session."""
    return Session(get_engine())


@mcp.tool()
def search_images(query: str, limit: int = 5) -> list[dict]:
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
def search_by_image_id(image_id: int, limit: int = 5) -> list[dict]:
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
def search_by_image_upload(image_base64: str, limit: int = 5) -> list[dict]:
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


@mcp.resource("libraries://list")
def list_libraries() -> str:
    """List all image libraries available in the system."""
    with get_db_session() as session:
        libraries = session.exec(select(LibraryPath)).all()
        if not libraries:
            return "No libraries found."
        return "\n".join(
            [f"ID: {lib.id} | Name: {lib.name} | Path: {lib.path}" for lib in libraries]
        )


@mcp.resource("collections://list")
def list_collections() -> str:
    """List all image collections created by the user."""
    with get_db_session() as session:
        collections = session.exec(select(Collection)).all()
        if not collections:
            return "No collections found."
        return "\n".join([f"ID: {col.id} | Name: {col.name}" for col in collections])


@mcp.tool()
def create_collection(name: str) -> str:
    """Create a new image collection with the given name."""
    with get_db_session() as session:
        # Check if exists
        existing = session.exec(
            select(Collection).where(Collection.name == name)
        ).first()
        if existing:
            return f"Collection '{name}' already exists with ID {existing.id}"

        collection = Collection(name=name)
        session.add(collection)
        session.commit()
        session.refresh(collection)
        return f"Created collection '{name}' with ID {collection.id}"


@mcp.tool()
def add_image_to_collection(image_id: int, collection_id: int) -> str:
    """Add a specific image to a collection."""
    with get_db_session() as session:
        # Verify image exists
        image = session.get(LocalImage, image_id)
        if not image:
            return f"Image with ID {image_id} not found"

        # Verify collection exists
        collection = session.get(Collection, collection_id)
        if not collection:
            return f"Collection with ID {collection_id} not found"

        # Check if already in collection
        exists = session.exec(
            select(CollectionItem)
            .where(CollectionItem.collection_id == collection_id)
            .where(CollectionItem.image_id == image_id)
        ).first()

        if exists:
            return f"Image {image_id} is already in collection {collection_id}"

        item = CollectionItem(collection_id=collection_id, image_id=image_id)
        session.add(item)
        session.commit()
        return f"Successfully added image {image_id} to collection '{collection.name}' ({collection_id})"


@mcp.tool()
def add_images_to_collection(image_ids: list[int], collection_id: int) -> str:
    """Add multiple images to a collection."""
    with get_db_session() as session:
        # Verify collection exists
        collection = session.get(Collection, collection_id)
        if not collection:
            return f"Collection with ID {collection_id} not found"

        added_count = 0
        errors = []

        for image_id in image_ids:
            # Verify image exists
            image = session.get(LocalImage, image_id)
            if not image:
                errors.append(f"Image ID {image_id} not found")
                continue

            # Check if already in collection
            exists = session.exec(
                select(CollectionItem)
                .where(CollectionItem.collection_id == collection_id)
                .where(CollectionItem.image_id == image_id)
            ).first()

            if exists:
                continue

            item = CollectionItem(collection_id=collection_id, image_id=image_id)
            session.add(item)
            added_count += 1

        session.commit()

        result_msg = f"Successfully added {added_count} images to collection '{collection.name}' ({collection_id})."
        if errors:
            result_msg += f" Errors: {'; '.join(errors)}"
        return result_msg


@mcp.tool()
def get_collection_items(collection_id: int) -> list[dict]:
    """Get all images in a specific collection."""
    with get_db_session() as session:
        collection = session.get(Collection, collection_id)
        if not collection:
            return [f"Collection with ID {collection_id} not found"]

        items = session.exec(
            select(LocalImage)
            .join(CollectionItem)
            .where(CollectionItem.collection_id == collection_id)
        ).all()

        return [{"id": img.id, "path": img.path, "prompt": img.prompt} for img in items]
