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
    root_password: Optional[str] = None
    user_operator_name: Optional[str] = None
    user_username: Optional[str] = None
    user_key: Optional[str] = None
    user_password: Optional[str] = None


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
    # Cloud mode defaults to "db" auth (full RBAC)
    from embeddr.core.config import is_cloud_mode
    if is_cloud_mode():
        return "db"
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
        from embeddr.core.config import is_cloud_mode
        if is_cloud_mode():
            raise RuntimeError(
                "Cloud mode requires EMBEDDR_AUTH_SALT to be set. "
                "Set EMBEDDR_AUTH_SALT (recommended >=32 random chars) before starting."
            )
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


def create_authorization_code(
    session: Session,
    *,
    service_client_id: UUID,
    user_id: UUID,
    operator_id: UUID,
    scopes: List[str],
    redirect_uri: str,
    code_challenge: Optional[str] = None,
    code_challenge_method: str = "S256",
    state: Optional[str] = None,
) -> tuple:
    """Create an OAuth2 authorization code for the consent flow."""
    from embeddr_core.models.authorization_code import AuthorizationCode

    raw_code = f"eac_{secrets.token_urlsafe(32)}"
    code_hash = hash_api_key(raw_code)
    now = datetime.now(timezone.utc)

    auth_code = AuthorizationCode(
        code_hash=code_hash,
        code_prefix=raw_code[:8],
        service_client_id=service_client_id,
        user_id=user_id,
        operator_id=operator_id,
        scopes=scopes,
        redirect_uri=redirect_uri,
        code_challenge=code_challenge,
        code_challenge_method=code_challenge_method,
        state=state,
        created_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    session.add(auth_code)
    return auth_code, raw_code


def hash_authorization_code(raw_code: str) -> str:
    """Hash an authorization code using the same scheme as API keys."""
    return hash_api_key(raw_code)


def exchange_authorization_code(
    session: Session,
    *,
    raw_code: str,
    client_id: str,
    client_secret: Optional[str] = None,
    redirect_uri: Optional[str] = None,
    code_verifier: Optional[str] = None,
) -> tuple:
    """Exchange an authorization code for a session token (OAuth2 token endpoint)."""
    from embeddr_core.models.authorization_code import AuthorizationCode
    from embeddr_core.models.service_client import ServiceClient

    code_hash = hash_authorization_code(raw_code)
    auth_code = session.exec(
        select(AuthorizationCode).where(AuthorizationCode.code_hash == code_hash)
    ).first()

    if not auth_code:
        raise ValueError("Invalid authorization code")
    if auth_code.used_at is not None:
        raise ValueError("Authorization code already used")
    if _is_datetime_expired(auth_code.expires_at):
        raise ValueError("Authorization code expired")

    # Validate client
    sc = session.exec(
        select(ServiceClient).where(ServiceClient.id == auth_code.service_client_id)
    ).first()
    if not sc or not sc.is_active:
        raise ValueError("Invalid or inactive service client")
    if sc.client_id != client_id:
        raise ValueError(f"Client ID mismatch: expected {sc.client_id}, got {client_id}")

    # For confidential clients, verify secret. Public clients skip this (use PKCE instead).
    if not sc.is_public:
        if not client_secret:
            raise ValueError("Client secret required for confidential clients")
        secret_hash = hash_api_key(client_secret)
        if secret_hash != sc.client_secret_hash:
            raise ValueError("Invalid client secret")

    # PKCE verification (required for public clients, optional for confidential)
    if auth_code.code_challenge and code_verifier:
        import base64
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        if challenge != auth_code.code_challenge:
            return None, None

    # Mark code as used
    auth_code.used_at = datetime.now(timezone.utc)
    session.add(auth_code)

    # Create session for the authorized user
    auth_session, raw_token = create_auth_session(
        session,
        user_id=auth_code.user_id,
        operator_id=auth_code.operator_id,
        session_name=f"oauth:{sc.name}",
        auth_method="authorization_code",
    )

    return auth_session, raw_token


def validate_client_credentials(
    session: Session, client_id: str, client_secret: str,
) -> Optional["ServiceClient"]:
    """Validate OAuth2 client credentials. Returns the ServiceClient if valid."""
    from embeddr_core.models.service_client import ServiceClient

    sc = session.exec(
        select(ServiceClient).where(ServiceClient.client_id == client_id)
    ).first()
    if not sc or not sc.is_active:
        return None

    secret_hash = hash_api_key(client_secret)
    if secret_hash != sc.client_secret_hash:
        return None

    sc.last_used_at = datetime.now(timezone.utc)
    session.add(sc)
    return sc


def create_service_session(
    session: Session,
    sc: "ServiceClient",
    requested_scopes: Optional[List[str]] = None,
) -> tuple:
    """Create a session for a service client (client_credentials grant)."""
    # Find the user who created this service client
    user = session.get(UserAccount, sc.created_by_user_id)
    if not user:
        raise ValueError("Service client has no associated user")

    auth_session, raw_token = create_auth_session(
        session,
        user_id=user.id,
        operator_id=sc.operator_id,
        session_name=f"service:{sc.name}",
        auth_method="client_credentials",
    )
    # Store service_client_id if the field exists
    if hasattr(auth_session, "service_client_id"):
        auth_session.service_client_id = sc.id
        session.add(auth_session)
        session.commit()

    return auth_session, raw_token


def create_service_client(
    session: Session,
    *,
    name: str,
    description: Optional[str] = None,
    operator_id: UUID,
    created_by_user_id: UUID,
    redirect_uris: Optional[List[str]] = None,
    allowed_scopes: Optional[List[str]] = None,
    grant_types: Optional[List[str]] = None,
) -> tuple:
    """Create a new OAuth2 service client. Returns (ServiceClient, raw_secret)."""
    from embeddr_core.models.service_client import ServiceClient

    client_id = f"embeddr:{name.lower().replace(' ', '-')}"
    raw_secret = f"esc_{secrets.token_urlsafe(32)}"
    secret_hash = hash_api_key(raw_secret)

    sc = ServiceClient(
        client_id=client_id,
        client_secret_hash=secret_hash,
        client_secret_prefix=raw_secret[:8],
        name=name,
        description=description,
        operator_id=operator_id,
        created_by_user_id=created_by_user_id,
        redirect_uris=redirect_uris or [],
        allowed_scopes=allowed_scopes or [],
        grant_types=grant_types or ["client_credentials"],
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    session.add(sc)
    session.commit()
    session.refresh(sc)
    return sc, raw_secret


def ensure_first_party_clients(
    session: Session,
    *,
    operator_id: UUID,
    created_by_user_id: UUID,
) -> Dict[str, str]:
    """Ensure built-in first-party service clients exist. Returns status per client."""
    from embeddr_core.models.service_client import ServiceClient

    # Only core first-party clients go here. Plugin-specific clients
    # (e.g. ComfyUI) should be registered by their respective plugins
    # via the service client API.
    first_party = {
        "embeddr:nelumbo": {
            "name": "Nelumbo",
            "description": "Embeddr admin panel",
            "redirect_uris": [],
            "allowed_scopes": ["*"],
            "grant_types": ["authorization_code", "client_credentials"],
        },
    }

    results: Dict[str, str] = {}
    for client_id, cfg in first_party.items():
        existing = session.exec(
            select(ServiceClient).where(ServiceClient.client_id == client_id)
        ).first()
        if existing:
            results[client_id] = "(already registered)"
            continue

        raw_secret = f"esc_{secrets.token_urlsafe(32)}"
        secret_hash = hash_api_key(raw_secret)

        sc = ServiceClient(
            client_id=client_id,
            client_secret_hash=secret_hash,
            client_secret_prefix=raw_secret[:8],
            name=cfg["name"],
            description=cfg["description"],
            operator_id=operator_id,
            created_by_user_id=created_by_user_id,
            redirect_uris=cfg["redirect_uris"],
            allowed_scopes=cfg["allowed_scopes"],
            grant_types=cfg["grant_types"],
            is_public=True,  # First-party clients use PKCE, no secret needed
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
        session.add(sc)
        results[client_id] = raw_secret
        logger.info("Registered first-party client: %s", client_id)

    session.commit()
    return results


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


def cleanup_expired_sessions(session: Session) -> int:
    """Remove expired and revoked sessions. Returns count of deleted rows."""
    now = datetime.now(timezone.utc)
    expired = session.exec(
        select(AuthSession).where(
            (AuthSession.expires_at != None) & (AuthSession.expires_at < now)  # noqa: E711
            | (AuthSession.revoked_at != None)  # noqa: E711
        )
    ).all()
    count = len(expired)
    for s in expired:
        session.delete(s)
    if count > 0:
        session.commit()
    return count


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

        # Verify the operator is still active
        if ctx and ctx.operator_id:
            operator = session.get(Operator, ctx.operator_id)
            if operator and not operator.is_active:
                logger.warning(
                    "auth_rejected reason=inactive_operator operator_id=%s user=%s",
                    ctx.operator_id, ctx.username,
                )
                return None

        touch_auth_session(session, auth_session)
        ctx.session_id = auth_session.id
        return ctx

    api_key = lookup_api_key(session, raw_credential)
    if not api_key:
        return None
    ctx = build_auth_context(session, api_key, raw_credential)

    # Verify the operator is still active for API key auth too
    if ctx and ctx.operator_id:
        operator = session.get(Operator, ctx.operator_id)
        if operator and not operator.is_active:
            logger.warning(
                "auth_rejected reason=inactive_operator operator_id=%s key_prefix=%s",
                ctx.operator_id, raw_credential[:8] if raw_credential else "?",
            )
            return None

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
    raw_key: Optional[str] = None,
) -> Tuple[ApiKey, str]:
    if not raw_key:
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

    # Use pre-determined API key from env if provided (cloud mode provisioning)
    bootstrap_key = os.environ.get("EMBEDDR_BOOTSTRAP_API_KEY", "").strip() or None

    root_api_key, root_raw_key = create_api_key(
        session,
        user_id=root_user.id,
        operator_id=root_operator.id,
        name="server-admin",
        scopes=["*"],
        raw_key=bootstrap_key,
    )

    result = BootstrapAdminResult(
        root_user=root_user,
        root_key=root_raw_key,
        root_password="password",
    )

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
        result.user_password = "password"

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
