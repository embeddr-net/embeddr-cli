from __future__ import annotations

from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import StaticPool, QueuePool
from sqlmodel import create_engine
from sqlalchemy.engine.url import make_url

from embeddr.db.adapters.base import BaseAdapter
from embeddr.core.config import settings


class SqliteAdapter(BaseAdapter):
    def __init__(self) -> None:
        super().__init__(name="sqlite")

    def create_engine(self, database_url: str) -> Engine:
        connect_args = {
            "check_same_thread": False,
            "timeout": 60,
        }
        url = make_url(database_url)
        is_memory = url.database in {None, "", ":memory:"}
        poolclass = StaticPool if is_memory else QueuePool

        engine_kwargs = {
            "connect_args": connect_args,
            "poolclass": poolclass,
        }

        if poolclass is QueuePool:
            engine_kwargs.update(
                {
                    "pool_size": settings.DB_POOL_SIZE,
                    "max_overflow": settings.DB_MAX_OVERFLOW,
                    "pool_timeout": settings.DB_POOL_TIMEOUT,
                    "pool_pre_ping": True,
                }
            )

        engine = create_engine(database_url, **engine_kwargs)

        @event.listens_for(engine, "connect")
        def set_sqlite_pragma(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.execute("PRAGMA busy_timeout=60000")
            cursor.close()

        return engine

    def render_as_batch(self) -> bool:
        return True

    def get_default_url(self, data_dir: Path) -> str:
        return f"sqlite:///{data_dir / 'embeddr.db'}"
