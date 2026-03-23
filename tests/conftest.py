import asyncio
from sqlmodel.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine
from fastapi.testclient import TestClient
import os
import warnings
import pytest
from unittest.mock import patch

# Suppress pydantic deprecation warnings BEFORE any embeddr imports
# (Settings uses class Config: which triggers PydanticDeprecatedSince20 at import time)
warnings.filterwarnings(
    "ignore", category=DeprecationWarning, module="pydantic")


# Set env vars for testing BEFORE any embeddr imports
os.environ["EMBEDDR_DATA_DIR"] = "/tmp/embeddr_test_data"
os.environ["EMBEDDR_ENABLE_MCP"] = "false"
os.environ["DATABASE_URL"] = "sqlite://"


# ---------------------------------------------------------------------------
# Core DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(name="engine")
def engine_fixture():
    """In-memory SQLite engine with all models. Fresh per test."""
    # Import all models so SQLModel.metadata knows about them
    import embeddr_core.models  # noqa: F401

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(name="session")
def session_fixture(engine):
    """A DB session tied to the in-memory engine. Rolls back after each test."""
    with Session(engine) as session:
        yield session


# ---------------------------------------------------------------------------
# Execution spine fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(name="spine_engine")
def spine_engine_fixture(engine):
    """
    Patch get_engine / get_engine_isolated so the ExecutionSpine uses
    the test in-memory engine instead of the real database.
    """
    with patch("embeddr.core.execution_spine.get_engine", return_value=engine), \
            patch("embeddr.core.execution_spine.get_engine_isolated", return_value=engine):
        yield engine


@pytest.fixture(name="clean_spine")
def clean_spine_fixture(spine_engine):
    """
    Provide a clean ExecutionSpine with handlers cleared.
    Restores original handlers after the test.
    """
    from embeddr.core.execution_spine import ExecutionSpine

    original_handlers = dict(ExecutionSpine._handlers)
    original_pipelines = dict(ExecutionSpine._pipelines)
    original_resource_limits = dict(ExecutionSpine._resource_limits)
    ExecutionSpine._handlers.clear()
    ExecutionSpine._pipelines.clear()
    ExecutionSpine._resources = {
        name: asyncio.Semaphore(limit)
        for name, limit in original_resource_limits.items()
    }

    yield ExecutionSpine

    ExecutionSpine._handlers.clear()
    ExecutionSpine._handlers.update(original_handlers)
    ExecutionSpine._pipelines.clear()
    ExecutionSpine._pipelines.update(original_pipelines)
    ExecutionSpine._resources = {
        name: asyncio.Semaphore(limit)
        for name, limit in original_resource_limits.items()
    }


# ---------------------------------------------------------------------------
# FastAPI test client
# ---------------------------------------------------------------------------

@pytest.fixture(name="client")
def client_fixture(engine):
    """
    FastAPI TestClient backed by the test engine.
    Creates a minimal app mounting only the routers we test against,
    with no lifespan (avoids plugin loading, DB migrations, etc.).
    """
    from fastapi import FastAPI
    from embeddr.api.v1 import system, system_public

    app = FastAPI()
    app.include_router(system.router, prefix="/api/v1/system", tags=["system"])
    app.include_router(system_public.router, prefix="/api/v1", tags=["public"])

    from embeddr.db.session import get_session as db_get_session

    def get_session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[db_get_session] = get_session_override

    with patch("embeddr.db.session.get_engine", return_value=engine), \
            patch("embeddr.db.session.get_engine_isolated", return_value=engine):
        client = TestClient(app)
        yield client
    app.dependency_overrides.clear()
