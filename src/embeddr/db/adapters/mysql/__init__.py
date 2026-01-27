from __future__ import annotations

from pathlib import Path

from sqlalchemy.engine import Engine
from sqlalchemy.pool import QueuePool
from sqlmodel import create_engine

from embeddr.db.adapters.base import BaseAdapter


class MySQLAdapter(BaseAdapter):
    def __init__(self) -> None:
        super().__init__(name="mysql")

    def create_engine(self, database_url: str) -> Engine:
        return create_engine(
            database_url,
            poolclass=QueuePool,
            pool_size=10,
            max_overflow=20,
            pool_pre_ping=True,
        )

    def get_default_url(self, data_dir: Path) -> str:
        return "mysql+pymysql://user:password@localhost:3306/embeddr"
