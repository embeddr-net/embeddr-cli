from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Set, Tuple
from uuid import UUID

from sqlmodel import Session, select

from embeddr_core.models.api_key import ApiKey, ApiKeyPermission
from embeddr_core.models.operator import Operator
from embeddr_core.models.role import Role, RolePermission
from embeddr_core.models.user_account import UserAccount, UserRole
from embeddr.core.project import find_project_root, load_project_config

AUTH_MODE_ENV = "EMBEDDR_AUTH_MODE"
AUTH_SALT_ENV = "EMBEDDR_AUTH_SALT"
PASSWORD_ITERATIONS = 120_000


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
    roles: List[str] = field(default_factory=list)
    permissions: Set[str] = field(default_factory=set)
    is_admin: bool = False

    @property
    def is_open(self) -> bool:
        return self.mode == "open"

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
        root = find_project_root()
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
    return os.environ.get(AUTH_SALT_ENV, "embeddr-local")


def hash_api_key(raw_key: str) -> str:
    return hmac.new(_auth_salt().encode("utf-8"), raw_key.encode("utf-8"), hashlib.sha256).hexdigest()


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
            RolePermission.role_id.in_(list(role_ids)))
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
    roles = session.exec(select(Role).where(Role.id.in_(role_ids))).all()
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
    )


def lookup_api_key(session: Session, raw_key: str) -> Optional[ApiKey]:
    key_hash = hash_api_key(raw_key)
    api_key = session.exec(select(ApiKey).where(
        ApiKey.key_hash == key_hash)).first()
    if not api_key:
        return None
    if not api_key.is_active:
        return None
    if api_key.expires_at and api_key.expires_at < datetime.now(timezone.utc):
        return None
    return api_key


def touch_api_key(session: Session, api_key: ApiKey) -> None:
    api_key.last_used_at = datetime.now(timezone.utc)
    session.add(api_key)
    session.commit()


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
) -> UserAccount:
    user = session.exec(select(UserAccount).where(
        UserAccount.username == username)).first()
    if user:
        return user
    user = UserAccount(
        username=username,
        display_name=display_name,
        is_active=True,
        is_admin=is_admin,
        operator_id=operator_id,
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
    )

    operator = _ensure_operator(session, name=operator_name, is_root=False)
    user = _ensure_user(
        session,
        operator_id=operator.id,
        username=admin_username,
        display_name=admin_username.title(),
        is_admin=True,
        role=admin_role,
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
