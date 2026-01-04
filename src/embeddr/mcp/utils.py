from sqlmodel import Session
from embeddr.db.session import get_engine


def get_db_session():
    """Helper to get a new database session."""
    return Session(get_engine())
