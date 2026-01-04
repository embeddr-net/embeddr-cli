from datetime import datetime
from typing import Optional, Dict, Any, List
from sqlmodel import Field, SQLModel, Column, JSON


class Generation(SQLModel, table=True):
    id: str = Field(primary_key=True)  # UUID
    workflow_id: int = Field(foreign_key="workflow.id")
    prompt_id: Optional[str] = Field(default=None, index=True)
    # pending, processing, completed, failed
    status: str = Field(default="pending")
    prompt: str = Field(default="")  # Display name/prompt
    inputs: Dict[str, Any] = Field(default={}, sa_column=Column(JSON))
    outputs: List[Dict[str, Any]] = Field(default=[], sa_column=Column(JSON))
    error_message: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)
