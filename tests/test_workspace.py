from fastapi.testclient import TestClient
from sqlmodel import Session, select
from embeddr_core.models.library import LibraryPath


def test_add_library_path(client: TestClient, engine):
    # Test adding a library path
    response = client.post(
        "/api/v1/workspace/paths",
        json={
            "path": "/tmp/test_images",
            "name": "Test Images",
            "create_if_missing": True,
        },
    )
    assert response.status_code == 200
    data = response.json()
    assert data["path"] == "/tmp/test_images"
    assert data["name"] == "Test Images"

    # Verify it's in the database
    with Session(engine) as session:
        lib_path = session.exec(
            select(LibraryPath).where(LibraryPath.path == "/tmp/test_images")
        ).first()
        assert lib_path is not None
        assert lib_path.name == "Test Images"


def test_list_library_paths(client: TestClient):
    # Add a path first
    client.post(
        "/api/v1/workspace/paths",
        json={
            "path": "/tmp/test_images_2",
            "name": "Test Images 2",
            "create_if_missing": True,
        },
    )

    response = client.get("/api/v1/workspace/paths")
    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 1
    found = False
    for item in data:
        if item["path"] == "/tmp/test_images_2":
            found = True
            break
    assert found
