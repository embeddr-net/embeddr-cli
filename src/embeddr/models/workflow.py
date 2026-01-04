from datetime import datetime
from typing import Optional, Dict, Any
from sqlmodel import Field, SQLModel, Column, JSON


class Workflow(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)
    description: Optional[str] = None

    # The raw ComfyUI workflow JSON
    data: Dict[str, Any] = Field(default={}, sa_column=Column(JSON))

    # Metadata for inputs/outputs mapping
    # Structure: { "inputs": { "node_id": { "type": "image", "description": "..." } }, "outputs": ... }
    meta: Dict[str, Any] = Field(default={}, sa_column=Column(JSON))

    is_active: bool = Field(default=True)

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
