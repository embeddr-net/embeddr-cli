from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks
from sqlmodel import Session, select
from embeddr.db.session import get_session
from embeddr.models.dataset import Dataset, DatasetItem, DatasetType
from embeddr_core.models.collection import Collection, CollectionItem
from embeddr_core.models.library import LocalImage
from pydantic import BaseModel
from datetime import datetime
import os
import shutil
import json
from embeddr.core.config import get_data_dir
from embeddr.services.captioning import generate_caption
from embeddr.db.session import get_engine

router = APIRouter()


class DatasetCreate(BaseModel):
    name: str
    description: Optional[str] = None
    type: DatasetType = DatasetType.REGULAR
    collection_id: int


class DatasetUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    captioning_config: Optional[str] = None  # JSON string


class DatasetRead(BaseModel):
    id: int
    name: str
    description: Optional[str]
    type: DatasetType
    collection_id: int
    created_at: datetime
    updated_at: datetime
    item_count: int
    captioning_config: Optional[str]


class DatasetItemRead(BaseModel):
    id: int
    original_image_id: int
    processed_image_path: Optional[str]
    pair_image_path: Optional[str]
    caption: Optional[str]
    original_path: str


class DatasetItemUpdate(BaseModel):
    processed_image_path: Optional[str] = None
    pair_image_path: Optional[str] = None
    caption: Optional[str] = None


@router.post("", response_model=Dataset)
def create_dataset(dataset_in: DatasetCreate, session: Session = Depends(get_session)):
    # Verify collection exists
    collection = session.get(Collection, dataset_in.collection_id)
    if not collection:
        raise HTTPException(status_code=404, detail="Collection not found")

    dataset = Dataset(
        name=dataset_in.name,
        description=dataset_in.description,
        type=dataset_in.type,
        collection_id=dataset_in.collection_id,
    )
    session.add(dataset)
    session.commit()
    session.refresh(dataset)

    # Populate items from collection
    collection_items = session.exec(
        select(CollectionItem).where(
            CollectionItem.collection_id == dataset_in.collection_id
        )
    ).all()

    for item in collection_items:
        ds_item = DatasetItem(
            dataset_id=dataset.id,
            original_image_id=item.image_id,
            caption="",  # Initialize empty caption
        )
        session.add(ds_item)

    session.commit()
    return dataset


@router.get("", response_model=List[DatasetRead])
def list_datasets(session: Session = Depends(get_session)):
    datasets = session.exec(select(Dataset)).all()
    result = []
    for ds in datasets:
        count = session.exec(
            select(DatasetItem).where(DatasetItem.dataset_id == ds.id)
        ).all()
        result.append(
            DatasetRead(
                id=ds.id,
                name=ds.name,
                description=ds.description,
                type=ds.type,
                collection_id=ds.collection_id,
                created_at=ds.created_at,
                updated_at=ds.updated_at,
                item_count=len(count),
                captioning_config=ds.captioning_config,
            )
        )
    return result


@router.get("/{dataset_id}", response_model=Dataset)
def get_dataset(dataset_id: int, session: Session = Depends(get_session)):
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")
    return dataset


@router.patch("/{dataset_id}", response_model=Dataset)
def update_dataset(
    dataset_id: int, dataset_in: DatasetUpdate, session: Session = Depends(get_session)
):
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")

    if dataset_in.name is not None:
        dataset.name = dataset_in.name
    if dataset_in.description is not None:
        dataset.description = dataset_in.description
    if dataset_in.captioning_config is not None:
        dataset.captioning_config = dataset_in.captioning_config

    session.add(dataset)
    session.commit()
    session.refresh(dataset)
    return dataset


@router.delete("/{dataset_id}")
def delete_dataset(dataset_id: int, session: Session = Depends(get_session)):
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")
    session.delete(dataset)
    session.commit()
    return {"ok": True}


@router.get("/{dataset_id}/items", response_model=List[DatasetItemRead])
def get_dataset_items(dataset_id: int, session: Session = Depends(get_session)):
    items = session.exec(
        select(DatasetItem).where(DatasetItem.dataset_id == dataset_id)
    ).all()
    result = []
    for item in items:
        # Fetch original image to get path
        img = session.get(LocalImage, item.original_image_id)
        if img:
            result.append(
                DatasetItemRead(
                    id=item.id,
                    original_image_id=item.original_image_id,
                    processed_image_path=item.processed_image_path,
                    pair_image_path=item.pair_image_path,
                    caption=item.caption,
                    original_path=img.path,
                )
            )
    return result


@router.patch("/{dataset_id}/items/{item_id}", response_model=DatasetItemRead)
def update_dataset_item(
    dataset_id: int,
    item_id: int,
    item_in: DatasetItemUpdate,
    session: Session = Depends(get_session),
):
    item = session.get(DatasetItem, item_id)
    if not item or item.dataset_id != dataset_id:
        raise HTTPException(status_code=404, detail="Dataset item not found")

    if item_in.processed_image_path is not None:
        item.processed_image_path = item_in.processed_image_path
    if item_in.pair_image_path is not None:
        item.pair_image_path = item_in.pair_image_path
    if item_in.caption is not None:
        item.caption = item_in.caption

    session.add(item)
    session.commit()
    session.refresh(item)

    img = session.get(LocalImage, item.original_image_id)
    return DatasetItemRead(
        id=item.id,
        original_image_id=item.original_image_id,
        processed_image_path=item.processed_image_path,
        pair_image_path=item.pair_image_path,
        caption=item.caption,
        original_path=img.path if img else "",
    )


@router.post("/{dataset_id}/export")
def export_dataset(dataset_id: int, session: Session = Depends(get_session)):
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")

    items = session.exec(
        select(DatasetItem).where(DatasetItem.dataset_id == dataset_id)
    ).all()

    # Create export directory
    export_base = get_data_dir() / "exports"
    os.makedirs(export_base, exist_ok=True)

    dataset_dir = export_base / f"{dataset.name}_{dataset.id}"
    if os.path.exists(dataset_dir):
        shutil.rmtree(dataset_dir)
    os.makedirs(dataset_dir)

    # Process items
    for i, item in enumerate(items):
        img = session.get(LocalImage, item.original_image_id)
        if not img:
            continue

        # Determine source image
        src_path = item.processed_image_path if item.processed_image_path else img.path
        if not os.path.exists(src_path):
            continue

        # Determine extension
        ext = os.path.splitext(src_path)[1]

        # Destination filename (e.g. 0001.png)
        dest_filename = f"{i:04d}{ext}"
        dest_path = dataset_dir / dest_filename

        # Copy image
        shutil.copy2(src_path, dest_path)

        # Write caption
        if item.caption:
            txt_filename = f"{i:04d}.txt"
            with open(dataset_dir / txt_filename, "w") as f:
                f.write(item.caption)

    return {
        "path": str(dataset_dir),
        "message": f"Exported {len(items)} items to {dataset_dir}",
    }


def run_auto_caption(dataset_id: int):
    # Create a new session
    engine = get_engine()
    with Session(engine) as session:
        dataset = session.get(Dataset, dataset_id)
        if not dataset:
            return

        # Parse config
        model_name = "vikhyatk/moondream2"
        kwargs = {}
        if dataset.captioning_config:
            try:
                config = json.loads(dataset.captioning_config)
                model_name = config.get("model", model_name)
                mode = config.get("mode", "caption")

                if mode == "query":
                    # Support both 'query' (old) and 'prompt' (new) keys
                    kwargs["prompt"] = config.get("prompt") or config.get("query")

                if "length" in config:
                    kwargs["length"] = config.get("length")
            except Exception as e:
                print(f"Error parsing caption config: {e}")

        items = session.exec(
            select(DatasetItem).where(DatasetItem.dataset_id == dataset_id)
        ).all()
        for item in items:
            if not item.caption:
                img = session.get(LocalImage, item.original_image_id)
                if img:
                    image_path = (
                        item.processed_image_path
                        if item.processed_image_path
                        else img.path
                    )
                    try:
                        caption = generate_caption(
                            image_path, model_name=model_name, **kwargs
                        )
                        item.caption = caption
                        session.add(item)
                        session.commit()
                    except Exception as e:
                        print(f"Failed to caption item {item.id}: {e}")


@router.post("/{dataset_id}/caption")
def auto_caption_dataset(
    dataset_id: int,
    background_tasks: BackgroundTasks,
    session: Session = Depends(get_session),
):
    dataset = session.get(Dataset, dataset_id)
    if not dataset:
        raise HTTPException(status_code=404, detail="Dataset not found")

    background_tasks.add_task(run_auto_caption, dataset_id)
    return {"message": "Auto-captioning started"}


@router.post("/{dataset_id}/items/{item_id}/caption")
def generate_item_caption(
    dataset_id: int, item_id: int, session: Session = Depends(get_session)
):
    item = session.get(DatasetItem, item_id)
    if not item or item.dataset_id != dataset_id:
        raise HTTPException(status_code=404, detail="Dataset item not found")

    dataset = session.get(Dataset, dataset_id)

    # Parse config
    model_name = "vikhyatk/moondream2"
    kwargs = {}
    if dataset.captioning_config:
        try:
            config = json.loads(dataset.captioning_config)
            model_name = config.get("model", model_name)
            mode = config.get("mode", "caption")

            if mode == "query":
                kwargs["prompt"] = config.get("prompt") or config.get("query")

            if "length" in config:
                kwargs["length"] = config.get("length")
        except Exception as e:
            print(f"Error parsing caption config: {e}")

    img = session.get(LocalImage, item.original_image_id)
    if not img:
        raise HTTPException(status_code=404, detail="Image not found")

    # Use processed image if available, else original
    image_path = item.processed_image_path if item.processed_image_path else img.path

    try:
        caption = generate_caption(image_path, model_name=model_name, **kwargs)
        item.caption = caption
        session.add(item)
        session.commit()
        session.refresh(item)
        return {"caption": caption}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
