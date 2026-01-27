from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from sqlalchemy.engine import Engine


class DatabaseAdapter(Protocol):
    name: str

    def create_engine(self, database_url: str) -> Engine:
        ...

    def render_as_batch(self) -> bool:
        ...

    def get_default_url(self, data_dir: Path) -> str:
        ...


@dataclass(frozen=True)
class BaseAdapter:
    name: str

    def render_as_batch(self) -> bool:
        return False

    def get_default_url(self, data_dir: Path) -> str:
        raise NotImplementedError("Default URL not defined for this adapter")
