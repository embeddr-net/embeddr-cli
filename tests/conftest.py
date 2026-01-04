from embeddr.commands.serve import create_app
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool
import os

# Set env vars for testing
os.environ["EMBEDDR_DATA_DIR"] = "/tmp/embeddr_test_data"
os.environ["EMBEDDR_ENABLE_MCP"] = "false"


@pytest.fixture(name="engine")
def engine_fixture():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    # Import models to register them with SQLModel.metadata
    # We need to ensure all models are imported before create_all

    SQLModel.metadata.create_all(engine)
    return engine


@pytest.fixture(name="client")
def client_fixture(engine):
    app = create_app()

    # Override dependencies
    from embeddr.db.session import get_session as db_get_session
    from embeddr.api.endpoints.system import get_session as system_get_session

    def get_session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[db_get_session] = get_session_override
    app.dependency_overrides[system_get_session] = get_session_override

    client = TestClient(app)

    yield client

    app.dependency_overrides.clear()
