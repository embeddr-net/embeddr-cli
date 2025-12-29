from sqlmodel import SQLModel, create_engine, Session
from embeddr.core.config import settings
from functools import lru_cache


@lru_cache()
def get_engine():
    connect_args = (
        {"check_same_thread": False} if "sqlite" in settings.DATABASE_URL else {}
    )
    return create_engine(settings.DATABASE_URL, connect_args=connect_args)


def create_db_and_tables():
    SQLModel.metadata.create_all(get_engine())


def get_session():
    with Session(get_engine()) as session:
        yield session
