from pydantic import BaseModel
from sqlmodel import select
from embeddr.mcp.instance import mcp
from embeddr.mcp.utils import get_db_session
from embeddr_core.models.collection import Collection, CollectionItem
from embeddr_core.models.library import LocalImage


class CollectionObject(BaseModel):
    id: int
    name: str
    image_count: int


class CollectionListResponse(BaseModel):
    collections: list[CollectionObject]


# @mcp.resource("data://list", mime_type="application/json")
# def list_collections() -> CollectionListResponse:
#     with get_db_session() as session:
#         collections = session.exec(select(Collection)).all()
#         return CollectionListResponse(
#             collections=[
#                 {"id": col.id, "name": col.name}
#                 for col in collections
#             ]
#         )


@mcp.tool()
def list_collections() -> list[CollectionObject]:
    """List all image collections available in the system."""
    with get_db_session() as session:
        collections = session.exec(select(Collection)).all()

        return [
            CollectionObject(id=col.id, name=col.name, image_count=len(col.items))
            for col in collections
        ]


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
