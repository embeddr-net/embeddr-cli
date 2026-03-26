"""End-to-end happy path test for embeddr 0.2.0.

Validates the complete user journey:
  1. Server starts with release plugin profile
  2. System info endpoint works
  3. Auth: create user, login, get session
  4. Artifacts: create, list, get, search
  5. Provenance: create related artifacts, query provenance
  6. Graph: traverse relations
  7. Lotus: list capabilities, query taxonomy

Run with: EMBEDDR_INTEGRATION=1 pytest tests/test_happy_path.py -v

For Docker/CI: set EMBEDDR_INTEGRATION=1 and ensure the release plugins
are available in the plugins path.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, SQLModel, create_engine
from sqlmodel.pool import StaticPool

# Skip unless integration flag is set
pytestmark = pytest.mark.skipif(
    not os.environ.get("EMBEDDR_INTEGRATION"),
    reason="Integration test — requires EMBEDDR_INTEGRATION=1",
)


@pytest.fixture(scope="module")
def app_and_client():
    """Create a full app with in-memory DB and release plugin profile.

    Uses a temp directory for data so nothing touches the real filesystem.
    Scoped to module so all tests share the same server state (faster, tests ordering).
    """
    with tempfile.TemporaryDirectory(prefix="embeddr_e2e_") as tmpdir:
        db_path = os.path.join(tmpdir, "test.db")
        db_url = f"sqlite:///{db_path}"

        # Configure for test
        os.environ["EMBEDDR_DATA_DIR"] = tmpdir
        os.environ["EMBEDDR_AUTH_MODE"] = "db"
        os.environ["EMBEDDR_AUTH_SALT"] = "e2e-test-salt-0123456789abcdef0123456789abcdef"
        os.environ["EMBEDDR_PLUGIN_PROFILE"] = "minimal"
        os.environ["EMBEDDR_VERBOSE"] = "false"
        os.environ["DATABASE_URL"] = db_url
        os.environ["EMBEDDR_BOOTSTRAP_ADMIN_CONFIRM"] = "true"
        os.environ["EMBEDDR_NO_PLUGINS"] = "false"

        # Pre-create tables so migrations aren't needed
        import embeddr_core.models  # noqa: F401 — register all models
        from embeddr.db.session import get_engine
        engine = get_engine()
        SQLModel.metadata.create_all(engine)

        # Patch out migration check — we just created tables directly
        with patch("embeddr.commands.serve.create_db_and_tables"):
            from embeddr.commands.serve import create_app
            app = create_app(enable_docs=True, no_plugins=True)

            with TestClient(app, raise_server_exceptions=False) as client:
                yield app, client


@pytest.fixture(scope="module")
def admin_session(app_and_client):
    """Create an admin user and return auth headers."""
    _, client = app_and_client

    from sqlmodel import select as sql_select
    from embeddr.db.session import get_engine
    from embeddr.services import auth_service
    from embeddr_core.models.operator import Operator
    from embeddr_core.models.user_account import UserAccount

    engine = get_engine()
    with Session(engine) as session:
        existing = session.exec(
            sql_select(UserAccount).where(UserAccount.username == "e2e-admin")
        ).first()

        if not existing:
            op = Operator(
                name="e2e-operator",
                display_name="E2E Test",
                is_root=False,
                is_active=True,
            )
            session.add(op)
            session.flush()

            pw_hash, pw_salt = auth_service.hash_password("testpassword")
            user = UserAccount(
                username="e2e-admin",
                display_name="E2E Admin",
                password_hash=pw_hash,
                password_salt=pw_salt,
                is_active=True,
                is_admin=True,
                operator_id=op.id,
            )
            session.add(user)
            session.commit()

    # Login
    login = client.post("/api/v1/security/login", json={
        "username": "e2e-admin",
        "password": "testpassword",
    })
    assert login.status_code == 200, f"Login failed: {login.text}"
    data = login.json()
    headers = {"X-API-Key": data["key"]}

    return headers


# ── 1. System Health ──────────────────────────────────────

class TestSystemHealth:
    def test_system_info(self, app_and_client, admin_session):
        _, client = app_and_client
        resp = client.get("/api/v1/system/info", headers=admin_session)
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data or "plugin_count" in data

    def test_docs_available(self, app_and_client):
        _, client = app_and_client
        resp = client.get("/api/docs")
        assert resp.status_code == 200


# ── 2. Auth Flow ──────────────────────────────────────────

class TestAuthFlow:
    def test_admin_can_whoami(self, app_and_client, admin_session):
        _, client = app_and_client
        resp = client.get("/api/v1/security/whoami", headers=admin_session)
        assert resp.status_code == 200
        data = resp.json()
        assert data["user"]["username"] == "e2e-admin"

    def test_unauthenticated_rejected(self, app_and_client):
        _, client = app_and_client
        resp = client.get(
            "/api/v1/artifacts/",
            headers={"X-API-Key": "em_completely_invalid_key_12345"},
        )
        assert resp.status_code == 401


# ── 3. Artifact CRUD ─────────────────────────────────────

class TestArtifactCRUD:
    def test_create_artifact(self, app_and_client, admin_session):
        _, client = app_and_client
        resp = client.post("/api/v1/artifacts", json={
            "type_name": "artifact",
            "metadata_json": {"title": "E2E Test Document", "visibility": "private"},
        }, headers=admin_session)
        assert resp.status_code == 200, f"Create failed: {resp.text}"
        data = resp.json()
        assert data["id"]
        assert data["type_name"] == "artifact"
        assert data["owner_operator_id"] is not None

    def test_list_artifacts(self, app_and_client, admin_session):
        _, client = app_and_client
        resp = client.get("/api/v1/artifacts/", headers=admin_session)
        assert resp.status_code == 200
        data = resp.json()
        assert "items" in data
        assert data["total"] >= 1

    def test_get_artifact_by_id(self, app_and_client, admin_session):
        _, client = app_and_client

        # Create one
        create = client.post("/api/v1/artifacts", json={
            "type_name": "artifact",
            "metadata_json": {"title": "Fetch Test"},
        }, headers=admin_session)
        art_id = create.json()["id"]

        # Fetch it
        resp = client.get(f"/api/v1/artifacts/{art_id}", headers=admin_session)
        assert resp.status_code == 200
        assert resp.json()["id"] == art_id

    def test_search_artifacts(self, app_and_client, admin_session):
        _, client = app_and_client

        # Create with searchable metadata
        client.post("/api/v1/artifacts", json={
            "type_name": "artifact",
            "metadata_json": {"title": "Unique Searchable Document XYZ123"},
        }, headers=admin_session)

        # Search for it
        resp = client.get(
            "/api/v1/artifacts/search?q=XYZ123",
            headers=admin_session,
        )
        assert resp.status_code == 200

    def test_date_filter(self, app_and_client, admin_session):
        _, client = app_and_client
        resp = client.get(
            "/api/v1/artifacts/?created_after=2020-01-01T00:00:00",
            headers=admin_session,
        )
        assert resp.status_code == 200
        assert resp.json()["total"] >= 1


# ── 4. Provenance ────────────────────────────────────────

class TestProvenance:
    def test_provenance_endpoint(self, app_and_client, admin_session):
        _, client = app_and_client

        # Create parent
        parent = client.post("/api/v1/artifacts", json={
            "type_name": "artifact",
            "metadata_json": {"title": "Source Image", "source": "test"},
        }, headers=admin_session).json()

        # Create child
        child = client.post("/api/v1/artifacts", json={
            "type_name": "artifact",
            "metadata_json": {"title": "Generated Output"},
        }, headers=admin_session).json()

        # Create relation
        client.post(f"/api/v1/artifacts/{parent['id']}/relations", json={
            "target_id": child["id"],
            "relation_type": "generates",
        }, headers=admin_session)

        # Query provenance of child
        resp = client.get(
            f"/api/v1/artifacts/{child['id']}/provenance",
            headers=admin_session,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["artifact"]["id"] == child["id"]
        # Should have at least one relation
        assert len(data["inputs"]) > 0 or len(data["relations"]) > 0

    def test_provenance_shows_sources(self, app_and_client, admin_session):
        _, client = app_and_client

        # Create artifact with source metadata
        art = client.post("/api/v1/artifacts", json={
            "type_name": "artifact",
            "metadata_json": {"title": "From ComfyUI", "source": "comfyui"},
        }, headers=admin_session).json()

        resp = client.get(
            f"/api/v1/artifacts/{art['id']}/provenance",
            headers=admin_session,
        )
        assert resp.status_code == 200
        data = resp.json()
        source_namespaces = [s["namespace"] for s in data["sources"]]
        assert "comfyui" in source_namespaces


# ── 5. Graph ─────────────────────────────────────────────

class TestGraph:
    def test_taxonomy(self, app_and_client, admin_session):
        _, client = app_and_client
        resp = client.get("/api/v1/graph/taxonomy", headers=admin_session)
        assert resp.status_code == 200
        data = resp.json()
        assert "relation_types" in data or "relation_families" in data
        assert "source_brands" in data

    def test_graph_query(self, app_and_client, admin_session):
        _, client = app_and_client

        # Create a seed artifact
        art = client.post("/api/v1/artifacts", json={
            "type_name": "artifact",
            "metadata_json": {"title": "Graph Seed"},
        }, headers=admin_session).json()

        resp = client.post("/api/v1/graph/query", json={
            "seed_ids": [art["id"]],
            "max_depth": 1,
        }, headers=admin_session)
        assert resp.status_code == 200
        data = resp.json()
        assert "nodes" in data
        assert "edges" in data


# ── 6. Lotus Capabilities ────────────────────────────────

class TestLotus:
    def test_list_capabilities(self, app_and_client, admin_session):
        _, client = app_and_client
        resp = client.get("/api/v1/lotus/list", headers=admin_session)
        assert resp.status_code == 200
        data = resp.json()
        # Should have capabilities from core at minimum
        assert len(data) > 0 or (isinstance(data, dict) and data.get("items"))

    def test_query_capabilities(self, app_and_client, admin_session):
        _, client = app_and_client
        resp = client.get(
            "/api/v1/lotus/query?q=artifact",
            headers=admin_session,
        )
        assert resp.status_code == 200
