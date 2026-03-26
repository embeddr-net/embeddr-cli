from __future__ import annotations

import os
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from embeddr.api.v1 import security as security_router
from embeddr.db.session import get_session as db_get_session
from embeddr.services import auth_service
from embeddr_core.models.operator import Operator
from embeddr_core.models.user_account import UserAccount


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch):
    """Set auth env vars for this module only, restore after each test."""
    monkeypatch.setenv("EMBEDDR_AUTH_MODE", "multi")
    monkeypatch.setenv("EMBEDDR_AUTH_SALT", "test-auth-salt-0123456789abcdef0123456789abcdef")


def _build_app(engine) -> FastAPI:
    app = FastAPI()
    app.include_router(
        security_router.router,
        prefix="/api/v1/security",
        tags=["security"],
    )

    def get_session_override():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[db_get_session] = get_session_override
    return app


def _seed_user(engine, username: str = "local", password: str = "password"):
    with Session(engine) as session:
        operator = Operator(
            name="operator-local",
            display_name="Operator Local",
            is_root=False,
            is_active=True,
        )
        session.add(operator)
        session.flush()

        password_hash, password_salt = auth_service.hash_password(password)
        user = UserAccount(
            username=username,
            display_name="Local User",
            password_hash=password_hash,
            password_salt=password_salt,
            is_active=True,
            is_admin=True,
            operator_id=operator.id,
        )
        session.add(user)
        session.commit()


def test_multiple_logins_do_not_invalidate_existing_sessions(engine):
    _seed_user(engine)
    app = _build_app(engine)

    with patch("embeddr.db.session.get_engine", return_value=engine), patch(
        "embeddr.api.security.get_engine", return_value=engine
    ):
        with TestClient(app) as client_one, TestClient(app) as client_two:
            login_one = client_one.post(
                "/api/v1/security/login",
                json={"username": "local", "password": "password"},
            )
            assert login_one.status_code == 200
            payload_one = login_one.json()

            login_two = client_two.post(
                "/api/v1/security/login",
                json={"username": "local", "password": "password"},
            )
            assert login_two.status_code == 200
            payload_two = login_two.json()

            assert payload_one["session_id"] != payload_two["session_id"]

            whoami_one = client_one.get("/api/v1/security/whoami")
            whoami_two = client_two.get("/api/v1/security/whoami")

            assert whoami_one.status_code == 200
            assert whoami_two.status_code == 200
            assert whoami_one.json()["user"]["username"] == "local"
            assert whoami_two.json()["user"]["username"] == "local"


def test_logout_revokes_only_current_session(engine):
    _seed_user(engine)
    app = _build_app(engine)

    with patch("embeddr.db.session.get_engine", return_value=engine), patch(
        "embeddr.api.security.get_engine", return_value=engine
    ):
        with TestClient(app) as client_one, TestClient(app) as client_two:
            login_one = client_one.post(
                "/api/v1/security/login",
                json={"username": "local", "password": "password"},
            )
            assert login_one.status_code == 200

            login_two = client_two.post(
                "/api/v1/security/login",
                json={"username": "local", "password": "password"},
            )
            assert login_two.status_code == 200

            logout_one = client_one.post("/api/v1/security/logout")
            assert logout_one.status_code == 200

            whoami_one = client_one.get("/api/v1/security/whoami")
            whoami_two = client_two.get("/api/v1/security/whoami")

            assert whoami_one.status_code == 401
            assert whoami_two.status_code == 200


def test_sessions_endpoint_lists_and_revokes_owned_sessions(engine):
    _seed_user(engine)
    app = _build_app(engine)

    with patch("embeddr.db.session.get_engine", return_value=engine), patch(
        "embeddr.api.security.get_engine", return_value=engine
    ):
        with TestClient(app) as client:
            login = client.post(
                "/api/v1/security/login",
                json={"username": "local", "password": "password"},
            )
            assert login.status_code == 200
            session_id = login.json()["session_id"]

            session_list = client.get("/api/v1/security/sessions")
            assert session_list.status_code == 200
            items = session_list.json()["items"]
            assert any(item["id"] == session_id for item in items)

            revoke = client.delete(
                f"/api/v1/security/sessions/{session_id}?confirm=true"
            )
            assert revoke.status_code == 200

            whoami = client.get("/api/v1/security/whoami")
            assert whoami.status_code == 401


def test_refresh_session_rotates_token_and_keeps_auth(engine):
    _seed_user(engine)
    app = _build_app(engine)

    with patch("embeddr.db.session.get_engine", return_value=engine), patch(
        "embeddr.api.security.get_engine", return_value=engine
    ):
        with TestClient(app) as client:
            login = client.post(
                "/api/v1/security/login",
                json={"username": "local", "password": "password"},
            )
            assert login.status_code == 200
            before = login.json()

            refreshed = client.post(
                "/api/v1/security/sessions/refresh",
                json={"session_name": "rotated-session"},
            )
            assert refreshed.status_code == 200
            after = refreshed.json()

            assert after["session_id"] != before["session_id"]
            assert after["key"] != before["key"]

            whoami = client.get("/api/v1/security/whoami")
            assert whoami.status_code == 200


def test_logout_all_revokes_other_sessions_but_keeps_current_by_default(engine):
    _seed_user(engine)
    app = _build_app(engine)

    with patch("embeddr.db.session.get_engine", return_value=engine), patch(
        "embeddr.api.security.get_engine", return_value=engine
    ):
        with TestClient(app) as client_one, TestClient(app) as client_two:
            login_one = client_one.post(
                "/api/v1/security/login",
                json={"username": "local", "password": "password"},
            )
            assert login_one.status_code == 200

            login_two = client_two.post(
                "/api/v1/security/login",
                json={"username": "local", "password": "password"},
            )
            assert login_two.status_code == 200

            logout_all = client_one.post(
                "/api/v1/security/logout-all",
                json={"confirm": True, "include_current": False},
            )
            assert logout_all.status_code == 200
            assert logout_all.json()["revoked"] >= 1

            whoami_one = client_one.get("/api/v1/security/whoami")
            whoami_two = client_two.get("/api/v1/security/whoami")
            assert whoami_one.status_code == 200
            assert whoami_two.status_code == 401


def test_switch_client_session_within_operator(engine):
    _seed_user(engine, username="admin", password="password")
    with Session(engine) as session:
        admin = session.exec(
            select(UserAccount).where(UserAccount.username == "admin")
        ).first()
        assert admin is not None
        bot = UserAccount(
            username="bot-agent",
            display_name="Bot Agent",
            is_active=True,
            is_admin=False,
            operator_id=admin.operator_id,
        )
        session.add(bot)
        session.commit()
        session.refresh(bot)
        bot_id = str(bot.id)

    app = _build_app(engine)
    with patch("embeddr.db.session.get_engine", return_value=engine), patch(
        "embeddr.api.security.get_engine", return_value=engine
    ):
        with TestClient(app) as client:
            login = client.post(
                "/api/v1/security/login",
                json={"username": "admin", "password": "password"},
            )
            assert login.status_code == 200

            clients = client.get("/api/v1/security/clients/me")
            assert clients.status_code == 200
            usernames = {row["username"] for row in clients.json()["items"]}
            assert "admin" in usernames
            assert "bot-agent" in usernames

            switched = client.post(
                "/api/v1/security/sessions/switch-client",
                json={"user_id": bot_id, "session_name": "bot-context"},
            )
            assert switched.status_code == 200
            assert switched.json()["user"]["username"] == "bot-agent"

            whoami = client.get("/api/v1/security/whoami")
            assert whoami.status_code == 200
            assert whoami.json()["user"]["username"] == "bot-agent"
