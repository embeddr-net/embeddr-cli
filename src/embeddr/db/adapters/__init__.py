from __future__ import annotations

from typing import Optional

from sqlalchemy.engine.url import make_url

from embeddr.db.adapters.base import DatabaseAdapter
from embeddr.db.adapters.mysql import MySQLAdapter
from embeddr.db.adapters.postgresql import PostgresAdapter
from embeddr.db.adapters.sqlite import SqliteAdapter

_ADAPTERS = {
    "sqlite": SqliteAdapter(),
    "postgres": PostgresAdapter(),
    "postgresql": PostgresAdapter(),
    "mysql": MySQLAdapter(),
}


def get_adapter(database_url: str, provider: Optional[str] = None) -> DatabaseAdapter:
    if provider:
        key = provider.lower().strip()
        adapter = _ADAPTERS.get(key)
        if adapter:
            return adapter

    try:
        url = make_url(database_url)
        driver = url.drivername.split("+")[0]
        adapter = _ADAPTERS.get(driver)
        if adapter:
            return adapter
    except Exception:
        pass

    return _ADAPTERS["sqlite"]
