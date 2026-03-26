"""Tests for auth_service — password hashing, session cleanup, bootstrap result."""

import os
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from sqlmodel import Session

from embeddr.services.auth_service import (
    BootstrapAdminResult,
    cleanup_expired_sessions,
    hash_password,
    verify_password,
)
from embeddr_core.models.auth_session import AuthSession

os.environ.setdefault("EMBEDDR_AUTH_SALT", "test-salt-for-unit-tests-1234567890ab")


class TestBootstrapAdminResult:
    def test_has_password_fields(self):
        result = BootstrapAdminResult(
            root_user=None,  # type: ignore[arg-type]
            root_key="em_abc",
            root_password="rootpass",
            user_password="userpass",
        )
        assert result.root_password == "rootpass"
        assert result.user_password == "userpass"

    def test_defaults_to_none(self):
        result = BootstrapAdminResult(
            root_user=None,  # type: ignore[arg-type]
            root_key="em_abc",
        )
        assert result.root_password is None
        assert result.user_password is None
        assert result.user_key is None


class TestHashAndVerifyPassword:
    def test_round_trip(self):
        hashed, salt = hash_password("my-secret")
        assert verify_password("my-secret", hashed, salt) is True
        assert verify_password("wrong", hashed, salt) is False

    def test_explicit_salt_deterministic(self):
        h1, s1 = hash_password("pw", salt="aabbccdd00112233")
        h2, s2 = hash_password("pw", salt="aabbccdd00112233")
        assert h1 == h2 and s1 == s2


class TestCleanupExpiredSessions:
    def test_deletes_expired_and_revoked(self, session: Session):
        now = datetime.now(timezone.utc)
        uid = uuid4()

        expired = AuthSession(user_id=uid, token_hash="h_exp", token_prefix="es_", expires_at=now - timedelta(hours=1))
        revoked = AuthSession(user_id=uid, token_hash="h_rev", token_prefix="es_", revoked_at=now - timedelta(minutes=30))
        active = AuthSession(user_id=uid, token_hash="h_act", token_prefix="es_", expires_at=now + timedelta(days=7))

        session.add_all([expired, revoked, active])
        session.commit()

        count = cleanup_expired_sessions(session)
        assert count == 2
        assert session.get(AuthSession, active.id) is not None
        assert session.get(AuthSession, expired.id) is None
        assert session.get(AuthSession, revoked.id) is None

    def test_returns_zero_when_nothing_to_clean(self, session: Session):
        assert cleanup_expired_sessions(session) == 0
