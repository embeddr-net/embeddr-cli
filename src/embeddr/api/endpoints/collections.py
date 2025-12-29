from typing import List
from fastapi import APIRouter, Depends, HTTPException, Body
from sqlmodel import Session, select, func
from embeddr.db.session import get_session
from embeddr_core.models.collection import Collection, CollectionItem
from embeddr_core.models.library import LocalImage
from pydantic import BaseModel

router = APIRouter()


class CollectionCreate(BaseModel):
    name: str
    description: str | None = None


class CollectionUpdate(BaseModel):
    name: str | None = None
    description: str | None = None


class CollectionRead(BaseModel):
    id: int
    name: str
    description: str | None
    created_at: str
    item_count: int


@router.post("", response_model=Collection)
def create_collection(
    collection_data: CollectionCreate, session: Session = Depends(get_session)
):
    collection = Collection(
        name=collection_data.name, description=collection_data.description
    )
    session.add(collection)
    session.commit()
    session.refresh(collection)
    return collection


@router.get("", response_model=List[CollectionRead])
def list_collections(session: Session = Depends(get_session)):
    # Join with items to get count
    statement = (
        select(Collection, func.count(CollectionItem.id))
        .outerjoin(CollectionItem)
        .group_by(Collection.id)
    )
    results = session.exec(statement).all()

    return [
        CollectionRead(
            id=c.id,
            name=c.name,
            description=c.description,
            created_at=c.created_at.isoformat(),
            item_count=count,
        )
        for c, count in results
    ]


@router.get("/{collection_id}", response_model=Collection)
def get_collection(collection_id: int, session: Session = Depends(get_session)):
    collection = session.get(Collection, collection_id)
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")
    return collection


@router.delete("/{collection_id}")
def delete_collection(collection_id: int, session: Session = Depends(get_session)):
    collection = session.get(Collection, collection_id)
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")
    session.delete(collection)
    session.commit()
    return {"ok": True}


@router.post("/{collection_id}/items")
def add_item_to_collection(
    collection_id: int,
    image_id: int = Body(..., embed=True),
    session: Session = Depends(get_session),
):
    collection = session.get(Collection, collection_id)
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    image = session.get(LocalImage, image_id)
    if not image:
        raise HTTPException(status_code=404, detail="Image not found")

    # Check if already exists
    existing = session.exec(
        select(CollectionItem).where(
            CollectionItem.collection_id == collection_id,
            CollectionItem.image_id == image_id,
        )
    ).first()

    if existing:
        return {"message": "Image already in collection"}

    item = CollectionItem(collection_id=collection_id, image_id=image_id)
    session.add(item)
    session.commit()
    return {"ok": True}


@router.delete("/{collection_id}/items/{image_id}")
def remove_item_from_collection(
    collection_id: int, image_id: int, session: Session = Depends(get_session)
):
    item = session.exec(
        select(CollectionItem).where(
            CollectionItem.collection_id == collection_id,
            CollectionItem.image_id == image_id,
        )
    ).first()

    if not item:
        raise HTTPException(status_code=404, detail="Item not found in collection")

    session.delete(item)
    session.commit()
    return {"ok": True}


@router.get("/{collection_id}/items", response_model=dict)
def get_collection_items(
    collection_id: int,
    skip: int = 0,
    limit: int = 50,
    session: Session = Depends(get_session),
):
    collection = session.get(Collection, collection_id)
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    # Get images via join
    query = (
        select(LocalImage)
        .join(CollectionItem)
        .where(CollectionItem.collection_id == collection_id)
    )

    total = session.exec(
        select(func.count())
        .select_from(CollectionItem)
        .where(CollectionItem.collection_id == collection_id)
    ).one()

    images = session.exec(query.offset(skip).limit(limit)).all()

    return {"total": total, "items": images, "skip": skip, "limit": limit}
