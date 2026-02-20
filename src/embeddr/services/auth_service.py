from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import binascii
import logging
from pathlib import Path
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable, List, Optional, Set, Tuple
from uuid import UUID

from sqlmodel import Session, col, select

from embeddr_core.models.api_key import ApiKey, ApiKeyPermission
from embeddr_core.models.auth_session import AuthSession
from embeddr_core.models.operator import Operator
from embeddr_core.models.role import Role, RolePermission
from embeddr_core.models.user_account import UserAccount, UserRole
from embeddr.core.project import find_project_root, load_project_config

logger = logging.getLogger(__name__)

AUTH_MODE_ENV = "EMBEDDR_AUTH_MODE"
AUTH_SALT_ENV = "EMBEDDR_AUTH_SALT"
ALLOW_INSECURE_AUTH_SALT_ENV = "EMBEDDR_ALLOW_INSECURE_AUTH_SALT"
PASSWORD_ITERATIONS = 120_000
SESSION_TOKEN_PREFIX = "es_"
SESSION_TTL_DAYS_ENV = "EMBEDDR_AUTH_SESSION_TTL_DAYS"


def _workspace_data_dir() -> Optional[Path]:
    env_data_dir = os.environ.get("EMBEDDR_DATA_DIR", "").strip()
    if env_data_dir:
        return Path(env_data_dir).expanduser().resolve()

    try:
        root = find_project_root(Path(os.environ.get("PWD") or Path.cwd()))
        if not root:
            return None
        config = load_project_config(root)
        paths = config.get("paths", {}) if isinstance(config, dict) else {}
        data_dir_cfg = paths.get("data_dir") if isinstance(
            paths, dict) else None
        if data_dir_cfg:
            candidate = Path(str(data_dir_cfg))
            return candidate if candidate.is_absolute() else (root / candidate).resolve()
        return (root / ".embeddr").resolve()
    except Exception:
        return None


def _recover_or_create_auth_salt() -> Optional[str]:
    # 1) Try auth.salt in workspace config
    try:
        root = find_project_root(Path(os.environ.get("PWD") or Path.cwd()))
        if root:
            config = load_project_config(root)
            auth_cfg = config.get("auth", {}) if isinstance(
                config, dict) else {}
            config_salt = (
                str(auth_cfg.get("salt", "")).strip()
                if isinstance(auth_cfg, dict)
                else ""
            )
            if config_salt and config_salt != "embeddr-local":
                os.environ[AUTH_SALT_ENV] = config_salt
                return config_salt
    except Exception:
        pass

    # 2) Try persisted file in data dir
    data_dir = _workspace_data_dir()
    if not data_dir:
        return None
    salt_file = data_dir / "auth_salt"
    if salt_file.exists():
        try:
            file_salt = salt_file.read_text(encoding="utf-8").strip()
            if file_salt and file_salt != "embeddr-local":
                os.environ[AUTH_SALT_ENV] = file_salt
                return file_salt
        except Exception:
            pass

    # 3) Generate/persist secure fallback so requests don't 500
    try:
        generated = secrets.token_urlsafe(32)
        salt_file.parent.mkdir(parents=True, exist_ok=True)
        salt_file.write_text(generated, encoding="utf-8")
        os.environ[AUTH_SALT_ENV] = generated
        logger.warning(
            "Generated missing auth salt for secured mode at %s",
            salt_file,
        )
        return generated
    except Exception:
        return None


@dataclass
class AuthContext:
    mode: str
    authenticated: bool
    raw_key: Optional[str] = None
    user_id: Optional[UUID] = None
    operator_id: Optional[UUID] = None
    operator_name: Optional[str] = None
    username: Optional[str] = None
    display_name: Optional[str] = None
    api_key_id: Optional[UUID] = None
    api_key_name: Optional[str] = None
    session_id: Optional[UUID] = None
    roles: List[str] = field(default_factory=list)
    permissions: Set[str] = field(default_factory=set)
    is_admin: bool = False
    is_root: bool = False

    @property
    def is_open(self) -> bool:
        return self.mode == "open"

    @property
    def is_privileged(self) -> bool:
        return self.is_open or self.is_admin or self.is_root

    def can(self, permission: str) -> bool:
        return any(permission_matches(grant, permission) for grant in self.permissions)


@dataclass
class BootstrapAdminResult:
    root_user: UserAccount
    root_key: str
    user_operator_name: Optional[str] = None
    user_username: Optional[str] = None
    user_key: Optional[str] = None


def get_auth_mode() -> str:
    mode = os.environ.get(AUTH_MODE_ENV, "").strip().lower()
    if mode in {"open", "single", "multi", "db"}:
        return mode
    try:
        root = find_project_root(Path(os.environ.get("PWD") or Path.cwd()))
        if root:
            config = load_project_config(root)
            cfg_mode = str(config.get("auth", {}).get(
                "mode", "")).strip().lower()
            if cfg_mode in {"open", "single", "multi", "db"}:
                return cfg_mode
    except Exception:
        pass
    return "open"


def is_auth_enabled() -> bool:
    return get_auth_mode() != "open"


def _auth_salt() -> str:
    mode = get_auth_mode()
    salt = os.environ.get(AUTH_SALT_ENV, "").strip()

    if mode == "open":
        return salt or "embeddr-local"

    insecure_override = os.environ.get(
        ALLOW_INSECURE_AUTH_SALT_ENV, "").strip().lower() in {"1", "true", "yes"}
    if insecure_override:
        fallback = salt or "embeddr-local"
        logger.warning(
            "Using insecure auth salt in secured auth mode because %s is enabled.",
            ALLOW_INSECURE_AUTH_SALT_ENV,
        )
        return fallback

    if not salt or salt == "embeddr-local":
        recovered = _recover_or_create_auth_salt()
        if recovered:
            return recovered
        raise RuntimeError(
            "Secured auth mode requires EMBEDDR_AUTH_SALT to be set to a non-default value. "
            "Set EMBEDDR_AUTH_SALT (recommended >=32 random chars) before starting the server."
        )

    return salt


def hash_api_key(raw_key: str) -> str:
    return hmac.new(_auth_salt().encode("utf-8"), raw_key.encode("utf-8"), hashlib.sha256).hexdigest()


def hash_session_token(raw_token: str) -> str:
    return hmac.new(
        _auth_salt().encode("utf-8"),
        raw_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def generate_session_token() -> str:
    return f"{SESSION_TOKEN_PREFIX}{secrets.token_urlsafe(40)}"


def _session_ttl_days() -> int:
    raw = os.environ.get(SESSION_TTL_DAYS_ENV, "").strip()
    if not raw:
        return 30
    try:
        parsed = int(raw)
        return parsed if parsed > 0 else 30
    except ValueError:
        return 30


def hash_password(password: str, *, salt: Optional[str] = None) -> tuple[str, str]:
    if not salt:
        salt_bytes = os.urandom(16)
        salt = binascii.hexlify(salt_bytes).decode("utf-8")
    salt_bytes = binascii.unhexlify(salt.encode("utf-8"))
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(
        "utf-8"), salt_bytes, PASSWORD_ITERATIONS)
    return binascii.hexlify(dk).decode("utf-8"), salt


def verify_password(password: str, password_hash: Optional[str], password_salt: Optional[str]) -> bool:
    if not password_hash or not password_salt:
        return False
    candidate_hash, _ = hash_password(password, salt=password_salt)
    return hmac.compare_digest(candidate_hash, password_hash)


def permission_matches(grant: str, permission: str) -> bool:
    if grant == "*":
        return True
    if grant.endswith("*"):
        return permission.startswith(grant[:-1])
    return grant == permission


def lotus_permission_for_capability(cap_id: str) -> str:
    return f"lotus:capability:{cap_id}"


def collect_role_permissions(session: Session, role_ids: Iterable[UUID]) -> Set[str]:
    if not role_ids:
        return set()
    rows = session.exec(
        select(RolePermission.permission).where(
            col(RolePermission.role_id).in_(list(role_ids)))
    ).all()
    return {row for row in rows if row}


def collect_api_key_permissions(session: Session, api_key_id: UUID) -> Set[str]:
    rows = session.exec(
        select(ApiKeyPermission.permission).where(
            ApiKeyPermission.api_key_id == api_key_id)
    ).all()
    return {row for row in rows if row}


def load_user_roles(session: Session, user_id: UUID) -> Tuple[List[Role], List[UUID]]:
    role_ids = session.exec(
        select(UserRole.role_id).where(UserRole.user_id == user_id)
    ).all()
    if not role_ids:
        return [], []
    roles = session.exec(select(Role).where(col(Role.id).in_(role_ids))).all()
    return list(roles), list(role_ids)


def build_auth_context(session: Session, api_key: ApiKey, raw_key: str) -> AuthContext:
    user = session.get(UserAccount, api_key.user_id)
    operator_id = api_key.operator_id or (user.operator_id if user else None)
    operator = session.get(Operator, operator_id) if operator_id else None
    roles, role_ids = load_user_roles(session, api_key.user_id)
    role_permissions = collect_role_permissions(session, role_ids)
    key_permissions = collect_api_key_permissions(session, api_key.id)

    permissions = set(role_permissions) | set(key_permissions)
    if api_key.scopes:
        permissions = {p for p in permissions if any(
            permission_matches(scope, p) for scope in api_key.scopes)}

    return AuthContext(
        mode=get_auth_mode(),
        authenticated=True,
        raw_key=raw_key,
        user_id=api_key.user_id,
        operator_id=operator_id,
        operator_name=operator.name if operator else None,
        username=user.username if user else None,
        display_name=user.display_name if user else None,
        api_key_id=api_key.id,
        api_key_name=api_key.name,
        roles=[r.name for r in roles],
        permissions=permissions,
        is_admin=bool(user.is_admin) if user else False,
        is_root=bool(operator.is_root) if operator else False,
    )


def build_user_auth_context(
    session: Session,
    user: UserAccount,
    raw_key: str,
    *,
    operator_id: Optional[UUID] = None,
    api_key_id: Optional[UUID] = None,
    api_key_name: Optional[str] = None,
    session_id: Optional[UUID] = None,
) -> AuthContext:
    resolved_operator_id = operator_id or user.operator_id
    operator = session.get(
        Operator, resolved_operator_id) if resolved_operator_id else None
    roles, role_ids = load_user_roles(session, user.id)
    role_permissions = collect_role_permissions(session, role_ids)
    return AuthContext(
        mode=get_auth_mode(),
        authenticated=True,
        raw_key=raw_key,
        user_id=user.id,
        operator_id=resolved_operator_id,
        operator_name=operator.name if operator else None,
        username=user.username,
        display_name=user.display_name,
        api_key_id=api_key_id,
        api_key_name=api_key_name,
        session_id=session_id,
        roles=[r.name for r in roles],
        permissions=set(role_permissions),
        is_admin=bool(user.is_admin),
        is_root=bool(operator.is_root) if operator else False,
    )


def lookup_api_key(session: Session, raw_key: str) -> Optional[ApiKey]:
    key_hash = hash_api_key(raw_key)
    api_key = session.exec(select(ApiKey).where(
        ApiKey.key_hash == key_hash)).first()
    if not api_key:
        logger.warning(
            "api_key_lookup_failed reason=hash_not_found prefix=%s",
            raw_key[:8] if raw_key else "?",
        )
        return None
    if not api_key.is_active:
        logger.warning(
            "api_key_lookup_failed reason=inactive key_id=%s prefix=%s",
            api_key.id, raw_key[:8] if raw_key else "?",
        )
        return None
    if api_key.expires_at and _is_datetime_expired(api_key.expires_at):
        logger.warning(
            "api_key_lookup_failed reason=expired key_id=%s expires_at=%s",
            api_key.id, api_key.expires_at,
        )
        return None
    return api_key


def _is_datetime_expired(value: datetime) -> bool:
    if value.tzinfo is None:
        return value < datetime.now(timezone.utc).replace(tzinfo=None)
    return value < datetime.now(timezone.utc)


def touch_api_key(session: Session, api_key: ApiKey) -> None:
    api_key.last_used_at = datetime.now(timezone.utc)
    session.add(api_key)
    session.commit()


def create_auth_session(
    session: Session,
    *,
    user_id: UUID,
    operator_id: Optional[UUID] = None,
    api_key_id: Optional[UUID] = None,
    session_name: Optional[str] = None,
    auth_method: str = "session",
    user_agent: Optional[str] = None,
    ip_address: Optional[str] = None,
    rotated_from_id: Optional[UUID] = None,
    expires_at: Optional[datetime] = None,
) -> tuple[AuthSession, str]:
    raw_token = generate_session_token()
    token_hash = hash_session_token(raw_token)
    now = datetime.now(timezone.utc)
    expiry = expires_at or (now + timedelta(days=_session_ttl_days()))
    auth_session = AuthSession(
        user_id=user_id,
        operator_id=operator_id,
        api_key_id=api_key_id,
        token_hash=token_hash,
        token_prefix=raw_token[:8],
        session_name=session_name,
        auth_method=auth_method,
        user_agent=user_agent,
        ip_address=ip_address,
        rotated_from_id=rotated_from_id,
        created_at=now,
        last_used_at=now,
        expires_at=expiry,
        revoked_at=None,
        revoked_reason=None,
    )
    session.add(auth_session)
    session.commit()
    session.refresh(auth_session)
    return auth_session, raw_token


def lookup_auth_session(session: Session, raw_token: str) -> Optional[AuthSession]:
    token_hash = hash_session_token(raw_token)
    auth_session = session.exec(
        select(AuthSession).where(AuthSession.token_hash == token_hash)
    ).first()
    if not auth_session:
        return None
    if auth_session.revoked_at is not None:
        return None
    if auth_session.expires_at and _is_datetime_expired(auth_session.expires_at):
        return None
    return auth_session


def touch_auth_session(session: Session, auth_session: AuthSession) -> None:
    auth_session.last_used_at = datetime.now(timezone.utc)
    session.add(auth_session)
    session.commit()


def revoke_auth_session(session: Session, auth_session: AuthSession) -> None:
    revoke_auth_session_with_reason(session, auth_session, "logout")


def revoke_auth_session_with_reason(
    session: Session,
    auth_session: AuthSession,
    reason: str,
) -> None:
    auth_session.revoked_at = datetime.now(timezone.utc)
    auth_session.revoked_reason = reason
    session.add(auth_session)
    session.commit()


def rotate_auth_session(
    session: Session,
    *,
    auth_session: AuthSession,
    session_name: Optional[str] = None,
    user_agent: Optional[str] = None,
    ip_address: Optional[str] = None,
) -> tuple[AuthSession, str]:
    new_session, raw_token = create_auth_session(
        session,
        user_id=auth_session.user_id,
        operator_id=auth_session.operator_id,
        api_key_id=auth_session.api_key_id,
        session_name=session_name or auth_session.session_name,
        auth_method=auth_session.auth_method or "session",
        user_agent=user_agent or auth_session.user_agent,
        ip_address=ip_address or auth_session.ip_address,
        rotated_from_id=auth_session.id,
        expires_at=auth_session.expires_at,
    )
    revoke_auth_session_with_reason(session, auth_session, "rotated")
    return new_session, raw_token


def revoke_all_auth_sessions(
    session: Session,
    *,
    user_id: UUID,
    exclude_session_id: Optional[UUID] = None,
    reason: str = "logout_all",
) -> int:
    rows = session.exec(
        select(AuthSession).where(
            AuthSession.user_id == user_id,
            AuthSession.revoked_at == None,  # noqa: E711
        )
    ).all()
    changed = 0
    for row in rows:
        if exclude_session_id and row.id == exclude_session_id:
            continue
        row.revoked_at = datetime.now(timezone.utc)
        row.revoked_reason = reason
        session.add(row)
        changed += 1
    if changed:
        session.commit()
    return changed


def resolve_auth_context(session: Session, raw_credential: str) -> Optional[AuthContext]:
    if raw_credential.startswith(SESSION_TOKEN_PREFIX):
        auth_session = lookup_auth_session(session, raw_credential)
        if not auth_session:
            return None

        ctx: Optional[AuthContext] = None
        if auth_session.api_key_id:
            api_key = session.get(ApiKey, auth_session.api_key_id)
            if (
                api_key
                and api_key.is_active
                and (
                    api_key.expires_at is None
                    or not _is_datetime_expired(api_key.expires_at)
                )
            ):
                ctx = build_auth_context(session, api_key, raw_credential)

        if ctx is None:
            user = session.get(UserAccount, auth_session.user_id)
            if not user or not user.is_active:
                return None
            ctx = build_user_auth_context(
                session,
                user,
                raw_credential,
                operator_id=auth_session.operator_id,
                api_key_id=auth_session.api_key_id,
                session_id=auth_session.id,
            )

        touch_auth_session(session, auth_session)
        ctx.session_id = auth_session.id
        return ctx

    api_key = lookup_api_key(session, raw_credential)
    if not api_key:
        return None
    ctx = build_auth_context(session, api_key, raw_credential)
    touch_api_key(session, api_key)
    return ctx


def create_api_key(
    session: Session,
    *,
    user_id: UUID,
    operator_id: Optional[UUID] = None,
    name: str,
    scopes: Optional[List[str]] = None,
    permissions: Optional[List[str]] = None,
) -> Tuple[ApiKey, str]:
    raw_key = f"em_{secrets.token_urlsafe(32)}"
    key_hash = hash_api_key(raw_key)
    key_prefix = raw_key[:8]

    if operator_id is None:
        user = session.get(UserAccount, user_id)
        operator_id = user.operator_id if user else None

    api_key = ApiKey(
        user_id=user_id,
        operator_id=operator_id,
        name=name,
        key_hash=key_hash,
        key_prefix=key_prefix,
        scopes=scopes or [],
        is_active=True,
    )
    session.add(api_key)
    session.flush()

    if permissions:
        for perm in permissions:
            session.add(ApiKeyPermission(
                api_key_id=api_key.id, permission=perm))

    session.commit()
    session.refresh(api_key)
    return api_key, raw_key


def _ensure_admin_role(session: Session) -> Role:
    role = session.exec(select(Role).where(Role.name == "admin")).first()
    if role:
        return role
    role = Role(
        name="admin",
        description="Full access",
        is_system=True,
    )
    session.add(role)
    session.flush()
    session.add(RolePermission(role_id=role.id, permission="*"))
    session.commit()
    return role


def _ensure_operator(session: Session, *, name: str, is_root: bool = False) -> Operator:
    operator = session.exec(select(Operator).where(
        Operator.name == name)).first()
    if operator:
        return operator
    operator = Operator(
        name=name,
        display_name=name.title(),
        is_root=is_root,
        is_active=True,
    )
    session.add(operator)
    session.commit()
    session.refresh(operator)
    return operator


def _ensure_user(
    session: Session,
    *,
    operator_id: UUID,
    username: str,
    display_name: str,
    is_admin: bool,
    role: Role,
    default_password: Optional[str] = None,
) -> UserAccount:
    user = session.exec(select(UserAccount).where(
        UserAccount.username == username)).first()
    if user:
        return user

    password_hash_val = None
    password_salt_val = None
    if default_password:
        password_hash_val, password_salt_val = hash_password(default_password)

    user = UserAccount(
        username=username,
        display_name=display_name,
        is_active=True,
        is_admin=is_admin,
        operator_id=operator_id,
        password_hash=password_hash_val,
        password_salt=password_salt_val,
    )
    session.add(user)
    session.flush()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.commit()
    session.refresh(user)
    return user


def ensure_default_admin(
    session: Session,
    *,
    mode: str,
    operator_name: str = "user",
    admin_username: str = "user",
) -> Optional[BootstrapAdminResult]:
    existing_users = session.exec(select(UserAccount.id)).first()
    if existing_users:
        return None

    admin_role = _ensure_admin_role(session)
    root_operator = _ensure_operator(session, name="root", is_root=True)
    root_user = _ensure_user(
        session,
        operator_id=root_operator.id,
        username="root",
        display_name="Root Client",
        is_admin=True,
        role=admin_role,
        default_password="password",
    )

    root_api_key, root_raw_key = create_api_key(
        session,
        user_id=root_user.id,
        operator_id=root_operator.id,
        name="server-admin",
        scopes=["*"],
    )

    result = BootstrapAdminResult(root_user=root_user, root_key=root_raw_key)

    if mode == "single":
        user_operator = _ensure_operator(
            session, name=operator_name, is_root=False
        )
        user = _ensure_user(
            session,
            operator_id=user_operator.id,
            username=admin_username,
            display_name=admin_username.title(),
            is_admin=True,
            role=admin_role,
            default_password="password",
        )

        user_api_key, user_raw_key = create_api_key(
            session,
            user_id=user.id,
            operator_id=user_operator.id,
            name="operator",
            scopes=["*"],
        )
        result.user_operator_name = user_operator.name
        result.user_username = user.username
        result.user_key = user_raw_key

    return result


def bootstrap_operator_flow(
    session: Session,
    *,
    mode: str,
    operator_name: str,
    admin_username: str,
) -> Optional[Tuple[UserAccount, ApiKey, str]]:
    existing_users = session.exec(select(UserAccount.id)).first()
    if existing_users:
        return None

    admin_role = _ensure_admin_role(session)
    root_operator = _ensure_operator(session, name="root", is_root=True)
    _ensure_user(
        session,
        operator_id=root_operator.id,
        username="root",
        display_name="Root User",
        is_admin=True,
        role=admin_role,
        default_password="password",
    )

    operator = _ensure_operator(session, name=operator_name, is_root=False)
    user = _ensure_user(
        session,
        operator_id=operator.id,
        username=admin_username,
        display_name=admin_username.title(),
        is_admin=True,
        role=admin_role,
        default_password="password",
    )

    key_name = "operator" if mode == "single" else "server-admin"
    api_key, raw_key = create_api_key(
        session,
        user_id=user.id,
        operator_id=operator.id,
        name=key_name,
        scopes=["*"],
    )
    return user, api_key, raw_key
