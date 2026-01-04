from datetime import datetime
from typing import Optional, List
from enum import Enum
from sqlmodel import Field, SQLModel, Relationship
from embeddr_core.models.library import LocalImage


class DatasetType(str, Enum):
    REGULAR = "regular"
    IMAGE_PAIR = "image_pair"


class Dataset(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: Optional[str] = None
    type: DatasetType = Field(default=DatasetType.REGULAR)
    collection_id: int = Field(index=True)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # JSON string: { "model": "...", "mode": "...", "query": "..." }
    captioning_config: Optional[str] = Field(default=None)

    items: List["DatasetItem"] = Relationship(
        back_populates="dataset", sa_relationship_kwargs={"cascade": "all, delete"}
    )


class DatasetItem(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    dataset_id: int = Field(foreign_key="dataset.id")

    # We link to the original image for reference
    original_image_id: int = Field(foreign_key="localimage.id")

    # If the image was processed (cropped, etc), this path is set.
    # If None, we use the original image.
    processed_image_path: Optional[str] = None

    # For image pair datasets, this stores the path to the paired/target image
    pair_image_path: Optional[str] = None

    caption: Optional[str] = None

    dataset: Dataset = Relationship(back_populates="items")
    original_image: LocalImage = Relationship()
