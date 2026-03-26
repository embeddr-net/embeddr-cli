"""Security & RBAC tests for embeddr-cli.

Tests that security boundaries hold under multi-tenant conditions:
- Cross-operator artifact isolation
- Permission enforcement on endpoints
- Inactive user/operator rejection
- API key scoping
- Unauthenticated access rejection

These tests are expected to START RED and be fixed incrementally.
"""
from __future__ import annotations

import os
from uuid import uuid4
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Depends
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from embeddr.api.v1 import security as security_router
from embeddr.api.v1 import artifacts as artifacts_router
from embeddr.api.security import get_auth_context, get_api_key
from embeddr.db.session import get_session as db_get_session
from embeddr.services import auth_service
from embeddr_core.models.operator import Operator
from embeddr_core.models.user_account import UserAccount
from embeddr_core.models.artifact import Artifact


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    """Force multi-user auth mode for all tests in this module."""
    monkeypatch.setenv("EMBEDDR_AUTH_MODE", "db")
    monkeypatch.setenv("EMBEDDR_AUTH_SALT", "test-rbac-salt-0123456789abcdef0123456789abcdef")


def _build_app(engine) -> FastAPI:
    """Build a test app with security + artifact routes, auth enabled."""
    # Ensure all tables exist
    from sqlmodel import SQLModel
    import embeddr_core.models  # noqa: F401 — register all models
    SQLModel.metadata.create_all(engine)

    app = FastAPI()
    api_deps = [Depends(get_api_key)]

    app.include_router(
        security_router.router,
        prefix="/api/v1/security",
        tags=["security"],
    )
    app.include_router(
        artifacts_router.router,
        prefix="/api/v1/artifacts",
        tags=["artifacts"],
        dependencies=api_deps,
    )

    def get_session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[db_get_session] = get_session_override
    return app


def _seed_operator(session: Session, name: str, *, is_root: bool = False) -> Operator:
    operator = Operator(
        name=name,
        display_name=name.title(),
        is_root=is_root,
        is_active=True,
    )
    session.add(operator)
    session.flush()
    return operator


def _seed_user(
    session: Session,
    operator: Operator,
    username: str,
    password: str = "password",
    *,
    is_admin: bool = False,
    is_active: bool = True,
) -> UserAccount:
    password_hash, password_salt = auth_service.hash_password(password)
    user = UserAccount(
        username=username,
        display_name=username.title(),
        password_hash=password_hash,
        password_salt=password_salt,
        is_active=is_active,
        is_admin=is_admin,
        operator_id=operator.id,
    )
    session.add(user)
    session.flush()
    return user


def _seed_artifact(
    session: Session,
    operator: Operator,
    name: str = "test-artifact",
    type_name: str = "artifact",
) -> Artifact:
    """Create an artifact owned by the given operator."""
    from embeddr_core.models.artifact_type import ArtifactType

    # Ensure the artifact type exists
    existing = session.get(ArtifactType, type_name)
    if not existing:
        session.add(ArtifactType(name=type_name))
        session.flush()

    artifact = Artifact(
        name=name,
        type_name=type_name,
        owner_operator_id=operator.id,
        metadata_json={"test": True},
    )
    session.add(artifact)
    session.flush()
    return artifact


def _login(client: TestClient, username: str, password: str = "password") -> dict:
    resp = client.post(
        "/api/v1/security/login",
        json={"username": username, "password": password},
    )
    assert resp.status_code == 200, f"Login failed for {username}: {resp.text}"
    return resp.json()


def _auth_header(login_result: dict) -> dict:
    return {"X-API-Key": login_result["key"]}


@pytest.fixture
def two_operator_setup(engine):
    """Seed two operators with users and artifacts for isolation testing."""
    with Session(engine) as session:
        op_a = _seed_operator(session, "operator-alpha")
        op_b = _seed_operator(session, "operator-beta")

        user_a = _seed_user(session, op_a, "alice", is_admin=True)
        user_b = _seed_user(session, op_b, "bob", is_admin=True)

        art_a = _seed_artifact(session, op_a, name="alice-secret-doc")
        art_b = _seed_artifact(session, op_b, name="bob-private-img")

        session.commit()

        return {
            "op_a": op_a.id, "op_b": op_b.id,
            "user_a": user_a.id, "user_b": user_b.id,
            "art_a": str(art_a.id), "art_b": str(art_b.id),
        }


from contextlib import contextmanager

@contextmanager
def _patched_client(engine, app):
    """Context manager yielding a TestClient with engine patches."""
    with patch("embeddr.db.session.get_engine", return_value=engine), \
         patch("embeddr.api.security.get_engine", return_value=engine), \
         patch("embeddr.api.v1.artifacts.get_engine", return_value=engine):
        with TestClient(app) as client:
            yield client


# ── 1. Unauthenticated Access Rejection ──────────────────

class TestUnauthenticatedAccess:
    """Endpoints should reject requests without credentials in non-open mode."""

    def test_artifact_list_requires_auth(self, engine):
        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            resp = client.get("/api/v1/artifacts/")
            assert resp.status_code == 401, (
                f"Expected 401 for unauthenticated artifact list, got {resp.status_code}"
            )

    def test_artifact_create_requires_auth(self, engine):
        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            resp = client.post("/api/v1/artifacts", json={
                "type_name": "artifact",
                "metadata_json": {},
            })
            assert resp.status_code == 401

    def test_artifact_search_requires_auth(self, engine):
        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            resp = client.get("/api/v1/artifacts/search?q=test")
            assert resp.status_code == 401

    def test_whoami_requires_auth(self, engine):
        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            resp = client.get("/api/v1/security/whoami")
            assert resp.status_code == 401

    def test_invalid_key_rejected(self, engine):
        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            resp = client.get(
                "/api/v1/artifacts/",
                headers={"X-API-Key": "em_completely_invalid_key_12345"},
            )
            assert resp.status_code == 401


# ── 2. Cross-Operator Artifact Isolation ──────────────────

class TestCrossOperatorIsolation:
    """Users from operator A must NOT see operator B's artifacts."""

    def test_artifact_list_only_shows_own_operator(self, engine, two_operator_setup):
        setup = two_operator_setup
        app = _build_app(engine)

        with _patched_client(engine, app) as client:
            # Alice logs in
            alice_auth = _login(client, "alice")
            resp = client.get(
                "/api/v1/artifacts/",
                headers=_auth_header(alice_auth),
            )
            assert resp.status_code == 200
            items = resp.json().get("items", [])
            artifact_ids = {item["id"] for item in items}

            # Alice should see her artifact
            assert setup["art_a"] in artifact_ids, (
                "Alice should see her own artifact"
            )
            # Alice should NOT see Bob's artifact
            assert setup["art_b"] not in artifact_ids, (
                "Alice should NOT see Bob's artifact — cross-operator leak!"
            )

    def test_artifact_get_rejects_other_operator(self, engine, two_operator_setup):
        setup = two_operator_setup
        app = _build_app(engine)

        with _patched_client(engine, app) as client:
            alice_auth = _login(client, "alice")
            # Try to access Bob's artifact directly by ID
            resp = client.get(
                f"/api/v1/artifacts/{setup['art_b']}",
                headers=_auth_header(alice_auth),
            )
            # Should be 403 or 404 — must NOT return the artifact
            assert resp.status_code in (403, 404), (
                f"Expected 403/404 when accessing other operator's artifact, got {resp.status_code}"
            )

    def test_artifact_search_scoped_to_operator(self, engine, two_operator_setup):
        setup = two_operator_setup
        app = _build_app(engine)

        with _patched_client(engine, app) as client:
            alice_auth = _login(client, "alice")
            resp = client.get(
                "/api/v1/artifacts/search?q=secret",
                headers=_auth_header(alice_auth),
            )
            assert resp.status_code == 200
            items = resp.json().get("items", [])
            for item in items:
                owner = item.get("owner_operator_id")
                assert owner is None or owner == str(setup["op_a"]), (
                    f"Search returned artifact from wrong operator: {owner}"
                )


# ── 3. Inactive User / Operator Rejection ─────────────────

class TestInactiveRejection:
    """Deactivated users and operators should not be able to authenticate."""

    def test_inactive_user_cannot_login(self, engine):
        with Session(engine) as session:
            op = _seed_operator(session, "operator-inactive-test")
            _seed_user(session, op, "inactive-user", is_active=False, is_admin=True)
            session.commit()

        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            resp = client.post(
                "/api/v1/security/login",
                json={"username": "inactive-user", "password": "password"},
            )
            # Should reject — user is inactive
            assert resp.status_code in (401, 403), (
                f"Inactive user was able to login! Got {resp.status_code}"
            )

    def test_inactive_operator_session_rejected(self, engine):
        """If an operator is deactivated AFTER login, the session should be invalid."""
        with Session(engine) as session:
            op = _seed_operator(session, "operator-deactivate-test")
            _seed_user(session, op, "soon-orphaned", is_admin=True)
            session.commit()
            op_id = op.id

        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            # Login while operator is active
            login_result = _login(client, "soon-orphaned")

            # Verify auth works
            whoami = client.get(
                "/api/v1/security/whoami",
                headers=_auth_header(login_result),
            )
            assert whoami.status_code == 200

            # Deactivate the operator
            with Session(engine) as session:
                op = session.get(Operator, op_id)
                op.is_active = False
                session.add(op)
                session.commit()

            # Subsequent requests should fail
            whoami2 = client.get(
                "/api/v1/security/whoami",
                headers=_auth_header(login_result),
            )
            assert whoami2.status_code in (401, 403), (
                f"Deactivated operator's user can still auth! Got {whoami2.status_code}"
            )

    def test_deactivated_user_session_rejected(self, engine):
        """If a user is deactivated AFTER login, subsequent requests should fail."""
        with Session(engine) as session:
            op = _seed_operator(session, "operator-deact-user")
            user = _seed_user(session, op, "will-be-deactivated", is_admin=True)
            session.commit()
            user_id = user.id

        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            login_result = _login(client, "will-be-deactivated")

            # Deactivate the user
            with Session(engine) as session:
                user = session.get(UserAccount, user_id)
                user.is_active = False
                session.add(user)
                session.commit()

            # Should now be rejected
            whoami = client.get(
                "/api/v1/security/whoami",
                headers=_auth_header(login_result),
            )
            assert whoami.status_code in (401, 403), (
                f"Deactivated user can still auth! Got {whoami.status_code}"
            )


# ── 4. Permission Enforcement ─────────────────────────────

class TestPermissionEnforcement:
    """Verify that non-admin users cannot access admin-only endpoints."""

    def test_non_admin_cannot_list_users(self, engine):
        with Session(engine) as session:
            op = _seed_operator(session, "operator-perms")
            _seed_user(session, op, "admin-user", is_admin=True)
            _seed_user(session, op, "regular-user", is_admin=False)
            session.commit()

        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            regular_auth = _login(client, "regular-user")

            resp = client.get(
                "/api/v1/security/users",
                headers=_auth_header(regular_auth),
            )
            assert resp.status_code == 403, (
                f"Non-admin could list users! Got {resp.status_code}"
            )

    def test_non_admin_cannot_create_user(self, engine):
        with Session(engine) as session:
            op = _seed_operator(session, "operator-perms2")
            _seed_user(session, op, "regular-only", is_admin=False)
            session.commit()

        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            regular_auth = _login(client, "regular-only")

            resp = client.post(
                "/api/v1/security/users",
                json={
                    "username": "hacker-created",
                    "password": "password",
                    "display_name": "Hacked User",
                },
                headers=_auth_header(regular_auth),
            )
            assert resp.status_code == 403, (
                f"Non-admin could create user! Got {resp.status_code}"
            )

    def test_non_admin_cannot_create_keys_for_others(self, engine):
        with Session(engine) as session:
            op = _seed_operator(session, "operator-keys")
            admin = _seed_user(session, op, "key-admin", is_admin=True)
            regular = _seed_user(session, op, "key-regular", is_admin=False)
            session.commit()
            admin_id = str(admin.id)

        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            regular_auth = _login(client, "key-regular")

            # Try to create an API key for the admin user
            resp = client.post(
                "/api/v1/security/keys",
                json={
                    "user_id": admin_id,
                    "name": "stolen-key",
                },
                headers=_auth_header(regular_auth),
            )
            assert resp.status_code == 403, (
                f"Non-admin could create keys for others! Got {resp.status_code}"
            )

    def test_admin_can_list_users(self, engine):
        with Session(engine) as session:
            op = _seed_operator(session, "operator-admin-ok")
            _seed_user(session, op, "real-admin", is_admin=True)
            session.commit()

        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            admin_auth = _login(client, "real-admin")

            resp = client.get(
                "/api/v1/security/users",
                headers=_auth_header(admin_auth),
            )
            assert resp.status_code == 200


# ── 5. API Key Scoping ────────────────────────────────────

class TestApiKeyScoping:
    """API keys with restricted scopes should only grant matching permissions."""

    def test_self_created_key_works(self, engine):
        with Session(engine) as session:
            op = _seed_operator(session, "operator-apikey")
            _seed_user(session, op, "key-user", is_admin=True)
            session.commit()

        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            login_result = _login(client, "key-user")

            # Create a self-key
            resp = client.post(
                "/api/v1/security/keys/self",
                json={"name": "my-key", "confirm": True},
                headers=_auth_header(login_result),
            )
            assert resp.status_code in (200, 201), (
                f"Failed to create self key: {resp.status_code} {resp.text}"
            )
            key_data = resp.json()
            raw_key = key_data.get("raw_key") or key_data.get("key")
            assert raw_key, "No raw key returned"

            # Use the API key for auth
            whoami = client.get(
                "/api/v1/security/whoami",
                headers={"X-API-Key": raw_key},
            )
            assert whoami.status_code == 200
            assert whoami.json()["user"]["username"] == "key-user"


# ── 6. Wrong Password Rejection ───────────────────────────

class TestPasswordSecurity:
    """Basic password security checks."""

    def test_wrong_password_rejected(self, engine):
        with Session(engine) as session:
            op = _seed_operator(session, "operator-pw")
            _seed_user(session, op, "pw-user")
            session.commit()

        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            resp = client.post(
                "/api/v1/security/login",
                json={"username": "pw-user", "password": "wrong-password"},
            )
            assert resp.status_code in (401, 403)

    def test_nonexistent_user_rejected(self, engine):
        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            resp = client.post(
                "/api/v1/security/login",
                json={"username": "ghost-user", "password": "password"},
            )
            assert resp.status_code in (401, 403, 404)

    def test_empty_password_rejected(self, engine):
        with Session(engine) as session:
            op = _seed_operator(session, "operator-empty-pw")
            _seed_user(session, op, "empty-pw-user")
            session.commit()

        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            resp = client.post(
                "/api/v1/security/login",
                json={"username": "empty-pw-user", "password": ""},
            )
            assert resp.status_code in (401, 403, 422)


# ── 7. Public/Private Visibility Sharing ──────────────────

class TestVisibilitySharing:
    """Public artifacts should be visible cross-operator. Private should not."""

    def test_public_artifact_visible_to_other_operator(self, engine):
        """Bob marks an artifact as public — Alice should be able to see it."""
        with Session(engine) as session:
            from embeddr_core.models.artifact_type import ArtifactType
            session.add(ArtifactType(name="artifact"))
            session.flush()

            op_a = _seed_operator(session, "op-vis-alpha")
            op_b = _seed_operator(session, "op-vis-beta")
            _seed_user(session, op_a, "vis-alice", is_admin=True)
            _seed_user(session, op_b, "vis-bob", is_admin=True)

            # Bob's public artifact
            public_art = Artifact(
                name="bob-public-doc",
                type_name="artifact",
                owner_operator_id=op_b.id,
                metadata_json={"visibility": "public", "title": "Shared Document"},
            )
            session.add(public_art)
            session.flush()
            public_art_id = str(public_art.id)

            # Bob's private artifact
            private_art = Artifact(
                name="bob-private-doc",
                type_name="artifact",
                owner_operator_id=op_b.id,
                metadata_json={"visibility": "private", "title": "Secret Document"},
            )
            session.add(private_art)
            session.commit()
            private_art_id = str(private_art.id)

        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            alice_auth = _login(client, "vis-alice")

            # Alice should see Bob's public artifact in the list
            resp = client.get(
                "/api/v1/artifacts/",
                headers=_auth_header(alice_auth),
            )
            assert resp.status_code == 200
            ids = {item["id"] for item in resp.json().get("items", [])}
            assert public_art_id in ids, (
                "Alice should see Bob's public artifact"
            )
            assert private_art_id not in ids, (
                "Alice should NOT see Bob's private artifact"
            )

    def test_public_artifact_accessible_by_id(self, engine):
        """Alice can GET a public artifact by ID from another operator."""
        with Session(engine) as session:
            from embeddr_core.models.artifact_type import ArtifactType
            session.add(ArtifactType(name="artifact"))
            session.flush()

            op_a = _seed_operator(session, "op-pub-alpha")
            op_b = _seed_operator(session, "op-pub-beta")
            _seed_user(session, op_a, "pub-alice", is_admin=True)
            _seed_user(session, op_b, "pub-bob", is_admin=True)

            public_art = Artifact(
                name="shared-thing",
                type_name="artifact",
                owner_operator_id=op_b.id,
                metadata_json={"visibility": "public"},
            )
            session.add(public_art)
            session.commit()
            public_art_id = str(public_art.id)

        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            alice_auth = _login(client, "pub-alice")

            resp = client.get(
                f"/api/v1/artifacts/{public_art_id}",
                headers=_auth_header(alice_auth),
            )
            assert resp.status_code == 200, (
                f"Alice should be able to read Bob's public artifact, got {resp.status_code}"
            )

    def test_private_artifact_not_accessible_by_id(self, engine):
        """Alice CANNOT GET a private artifact by ID from another operator."""
        with Session(engine) as session:
            from embeddr_core.models.artifact_type import ArtifactType
            session.add(ArtifactType(name="artifact"))
            session.flush()

            op_a = _seed_operator(session, "op-priv-alpha")
            op_b = _seed_operator(session, "op-priv-beta")
            _seed_user(session, op_a, "priv-alice", is_admin=True)
            _seed_user(session, op_b, "priv-bob", is_admin=True)

            private_art = Artifact(
                name="secret-thing",
                type_name="artifact",
                owner_operator_id=op_b.id,
                metadata_json={"visibility": "private"},
            )
            session.add(private_art)
            session.commit()
            private_art_id = str(private_art.id)

        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            alice_auth = _login(client, "priv-alice")

            resp = client.get(
                f"/api/v1/artifacts/{private_art_id}",
                headers=_auth_header(alice_auth),
            )
            assert resp.status_code in (403, 404), (
                f"Alice should NOT read Bob's private artifact, got {resp.status_code}"
            )


# ── 8. Artifact Ownership Enforcement ─────────────────────

class TestArtifactOwnership:
    """Artifacts created via the API should always have ownership set."""

    def test_created_artifact_has_owner(self, engine):
        """Artifacts created by an authenticated user must have owner_operator_id set."""
        with Session(engine) as session:
            from embeddr_core.models.artifact_type import ArtifactType
            session.add(ArtifactType(name="artifact"))
            session.flush()
            op = _seed_operator(session, "op-ownership")
            user = _seed_user(session, op, "owner-user", is_admin=True)
            session.commit()
            op_id = str(op.id)
            user_id = str(user.id)

        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            auth = _login(client, "owner-user")

            resp = client.post(
                "/api/v1/artifacts",
                json={
                    "type_name": "artifact",
                    "metadata_json": {"title": "My Document"},
                },
                headers=_auth_header(auth),
            )
            assert resp.status_code == 200
            data = resp.json()

            assert data.get("owner_operator_id") == op_id, (
                f"Created artifact should have owner_operator_id={op_id}, got {data.get('owner_operator_id')}"
            )
            assert data.get("owner_user_id") == user_id, (
                f"Created artifact should have owner_user_id={user_id}, got {data.get('owner_user_id')}"
            )

    def test_default_visibility_is_private(self, engine):
        """Artifacts without explicit visibility should default to private."""
        with Session(engine) as session:
            from embeddr_core.models.artifact_type import ArtifactType
            session.add(ArtifactType(name="artifact"))
            session.flush()

            op_a = _seed_operator(session, "op-defvis-a")
            op_b = _seed_operator(session, "op-defvis-b")
            _seed_user(session, op_a, "defvis-alice", is_admin=True)
            _seed_user(session, op_b, "defvis-bob", is_admin=True)
            session.commit()

        app = _build_app(engine)
        with _patched_client(engine, app) as client:
            # Bob creates an artifact with no visibility set
            bob_auth = _login(client, "defvis-bob")
            resp = client.post(
                "/api/v1/artifacts",
                json={
                    "type_name": "artifact",
                    "metadata_json": {"title": "No Visibility Set"},
                },
                headers=_auth_header(bob_auth),
            )
            assert resp.status_code == 200
            art_id = resp.json()["id"]

            # Alice should NOT see it (default is private)
            alice_auth = _login(client, "defvis-alice")
            resp = client.get(
                f"/api/v1/artifacts/{art_id}",
                headers=_auth_header(alice_auth),
            )
            assert resp.status_code in (403, 404), (
                f"Artifact with no visibility should be private, but Alice got {resp.status_code}"
            )
