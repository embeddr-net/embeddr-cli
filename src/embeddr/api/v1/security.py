from __future__ import annotations
from embeddr.api.security import require_admin

from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field
from sqlmodel import Session, select, func

from embeddr.api.security import COOKIE_NAME, get_auth_context
from embeddr.db.session import get_session
from embeddr.services import auth_service
from embeddr_core.models.api_key import ApiKey, ApiKeyPermission
from embeddr_core.models.auth_session import AuthSession
from embeddr_core.models.role import Role, RolePermission
from embeddr_core.models.operator import Operator
from embeddr_core.models.user_account import UserAccount, UserRole

router = APIRouter()


class SecurityOverviewUser(BaseModel):
    id: str
    username: str
    display_name: str
    avatar_url: Optional[str] = None
    is_admin: bool = False


class SecurityOverview(BaseModel):
    auth_mode: str
    auth_enabled: bool
    users: int
    roles: int
    api_keys: int
    current_user: Optional[SecurityOverviewUser] = None


class SessionOperatorInfo(BaseModel):
    id: str
    name: str
    display_name: Optional[str] = None
    is_root: bool = False


class SessionUserInfo(BaseModel):
    id: str
    username: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    is_admin: bool = False


class SessionClientKeyInfo(BaseModel):
    id: str
    name: str
    key_prefix: Optional[str] = None
    scopes: List[str] = Field(default_factory=list)
    permissions: List[str] = Field(default_factory=list)
    is_active: bool = True
    expires_at: Optional[str] = None
    last_used_at: Optional[str] = None


class AuthSessionInfo(BaseModel):
    auth_mode: str
    auth_enabled: bool
    operator: Optional[SessionOperatorInfo] = None
    user: Optional[SessionUserInfo] = None
    client_key: Optional[SessionClientKeyInfo] = None
    permissions: List[str] = Field(default_factory=list)


class AuthSessionSummary(BaseModel):
    id: str
    session_name: Optional[str] = None
    auth_method: Optional[str] = None
    user_agent: Optional[str] = None
    ip_address: Optional[str] = None
    created_at: Optional[str] = None
    last_used_at: Optional[str] = None
    expires_at: Optional[str] = None
    revoked_at: Optional[str] = None
    revoked_reason: Optional[str] = None
    rotated_from_id: Optional[str] = None
    api_key_id: Optional[str] = None
    current: bool = False


class OperatorSummary(BaseModel):
    id: str
    name: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    is_root: bool = False
    is_active: bool = True
    created_at: Optional[str] = None
    user_count: int = 0
    active_user_count: int = 0
    api_key_count: int = 0
    last_activity_at: Optional[str] = None


class BootstrapRequest(BaseModel):
    username: str = "local"
    display_name: str = "Local User"
    confirm: bool = False


class RoleCreateRequest(BaseModel):
    name: str
    description: Optional[str] = None
    permissions: List[str] = Field(default_factory=list)
    confirm: bool = False


class RoleUpdateRequest(BaseModel):
    description: Optional[str] = None
    permissions: Optional[List[str]] = None
    confirm: bool = False


class UserCreateRequest(BaseModel):
    username: str
    display_name: Optional[str] = None
    is_admin: bool = False
    role_ids: List[UUID] = Field(default_factory=list)
    password: Optional[str] = None
    confirm: bool = False


class UserUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    is_admin: Optional[bool] = None
    role_ids: Optional[List[UUID]] = None
    password: Optional[str] = None
    confirm: bool = False


class ApiKeyCreateRequest(BaseModel):
    user_id: UUID
    name: str
    scopes: List[str] = Field(default_factory=list)
    permissions: List[str] = Field(default_factory=list)
    confirm: bool = False


class ApiKeyDisableRequest(BaseModel):
    disabled: bool = True
    confirm: bool = False


class ApiKeySelfCreateRequest(BaseModel):
    name: str
    scopes: List[str] = Field(default_factory=list)
    permissions: List[str] = Field(default_factory=list)
    confirm: bool = False


class LoginRequest(BaseModel):
    username: str
    password: str
    session_name: Optional[str] = None


class RefreshSessionRequest(BaseModel):
    session_name: Optional[str] = None


class LogoutAllRequest(BaseModel):
    confirm: bool = False
    include_current: bool = False
    user_id: Optional[UUID] = None


class SwitchClientSessionRequest(BaseModel):
    user_id: UUID
    session_name: Optional[str] = None


class UserProfileResponse(BaseModel):
    id: str
    username: str
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None


class UserProfileUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None


class OperatorProfileUpdateRequest(BaseModel):
    display_name: Optional[str] = None
    avatar_url: Optional[str] = None
    confirm: bool = False


# Use the centralized require_admin from api/security.py


def _validate_self_key_permissions(auth, permissions: List[str], scopes: List[str]) -> None:
    if auth.is_open or auth.is_admin or getattr(auth, "is_root", False):
        return
    for perm in permissions:
        if not auth.can(perm):
            raise HTTPException(
                status_code=403, detail="Permission not allowed")
    for scope in scopes:
        if not auth.can(scope):
            raise HTTPException(status_code=403, detail="Scope not allowed")


def _request_client_ip(request: Request) -> Optional[str]:
    forwarded_for = request.headers.get("x-forwarded-for", "").strip()
    if forwarded_for:
        primary = forwarded_for.split(",")[0].strip()
        if primary:
            return primary
    return request.client.host if request.client else None


def _get_operator_stats(
    session: Session,
    operator_id: UUID,
) -> tuple[int, int, int, Optional[datetime]]:
    total_users = session.exec(
        select(func.count(UserAccount.id))
        .where(UserAccount.operator_id == operator_id)
    ).one()
    active_users = session.exec(
        select(func.count(UserAccount.id))
        .where(UserAccount.operator_id == operator_id)
        .where(UserAccount.is_active.is_(True))
    ).one()
    key_row = session.exec(
        select(func.count(ApiKey.id), func.max(ApiKey.last_used_at))
        .join(UserAccount, ApiKey.user_id == UserAccount.id)
        .where(UserAccount.operator_id == operator_id)
    ).one()
    key_count = key_row[0] or 0
    last_activity = key_row[1]
    return int(total_users or 0), int(active_users or 0), int(key_count), last_activity


def _ensure_operator_for_user(
    session: Session,
    user: UserAccount,
) -> Operator:
    if user.operator_id:
        operator = session.get(Operator, user.operator_id)
        if operator:
            return operator

    base_name = f"operator-{user.username}" if user.username else None
    if base_name:
        existing = session.exec(
            select(Operator).where(Operator.name == base_name)
        ).first()
        if existing:
            base_name = None

    name = base_name or f"operator-{user.id}"
    operator = Operator(
        name=name,
        display_name=user.display_name or user.username,
        is_root=False,
        is_active=True,
    )
    session.add(operator)
    session.flush()

    user.operator_id = operator.id
    session.add(user)
    session.flush()
    return operator


@router.get("/overview", response_model=SecurityOverview)
def get_security_overview(
    auth=Depends(get_auth_context),
    session: Session = Depends(get_session),
):
    users = session.exec(select(func.count()).select_from(UserAccount)).one()
    roles = session.exec(select(func.count()).select_from(Role)).one()
    keys = session.exec(select(func.count()).select_from(ApiKey)).one()

    current_user = None
    if auth.user_id and auth.username:
        user = session.get(UserAccount, auth.user_id)
        current_user = SecurityOverviewUser(
            id=str(auth.user_id),
            username=auth.username,
            display_name=auth.display_name or auth.username,
            avatar_url=user.avatar_url if user else None,
            is_admin=bool(user.is_admin) if user else False,
        )

    return SecurityOverview(
        auth_mode=auth_service.get_auth_mode(),
        auth_enabled=auth_service.is_auth_enabled(),
        users=users,
        roles=roles,
        api_keys=keys,
        current_user=current_user,
    )


@router.get("/whoami", response_model=AuthSessionInfo)
def get_whoami(
    auth=Depends(get_auth_context),
    session: Session = Depends(get_session),
):
    operator_info = None
    if auth.operator_id:
        operator = session.get(Operator, auth.operator_id)
        if operator:
            operator_info = SessionOperatorInfo(
                id=str(operator.id),
                name=operator.name,
                display_name=operator.display_name,
                is_root=bool(operator.is_root),
            )

    user_info = None
    if auth.user_id:
        user = session.get(UserAccount, auth.user_id)
        if user:
            user_info = SessionUserInfo(
                id=str(user.id),
                username=user.username,
                display_name=user.display_name,
                avatar_url=user.avatar_url,
                is_admin=bool(user.is_admin),
            )

    client_key_info = None
    if auth.api_key_id:
        api_key = session.get(ApiKey, auth.api_key_id)
        if api_key:
            client_key_info = SessionClientKeyInfo(
                id=str(api_key.id),
                name=api_key.name,
                key_prefix=api_key.key_prefix,
                scopes=list(api_key.scopes or []),
                permissions=[
                    row
                    for row in session.exec(
                        select(ApiKeyPermission.permission).where(
                            ApiKeyPermission.api_key_id == api_key.id
                        )
                    ).all()
                    if row
                ],
                is_active=bool(api_key.is_active),
                expires_at=api_key.expires_at.isoformat()
                if api_key.expires_at
                else None,
                last_used_at=api_key.last_used_at.isoformat()
                if api_key.last_used_at
                else None,
            )

    return AuthSessionInfo(
        auth_mode=auth.mode,
        auth_enabled=auth_service.is_auth_enabled(),
        operator=operator_info,
        user=user_info,
        client_key=client_key_info,
        permissions=sorted(auth.permissions) if auth.permissions else [],
    )


@router.get("/operators")
def list_operators(
    auth=Depends(require_admin),
    session: Session = Depends(get_session),
):
    operators = session.exec(select(Operator)).all()
    operator_ids = [op.id for op in operators]
    counts: Dict[UUID, int] = {}
    active_counts: Dict[UUID, int] = {}
    api_key_counts: Dict[UUID, int] = {}
    last_activity: Dict[UUID, datetime] = {}

    if operator_ids:
        rows = session.exec(
            select(UserAccount.operator_id, func.count(UserAccount.id))
            .where(UserAccount.operator_id.in_(operator_ids))
            .group_by(UserAccount.operator_id)
        ).all()
        counts = {row[0]: row[1] for row in rows if row[0]}

        active_rows = session.exec(
            select(UserAccount.operator_id, func.count(UserAccount.id))
            .where(UserAccount.operator_id.in_(operator_ids))
            .where(UserAccount.is_active.is_(True))
            .group_by(UserAccount.operator_id)
        ).all()
        active_counts = {row[0]: row[1] for row in active_rows if row[0]}

        key_rows = session.exec(
            select(
                UserAccount.operator_id,
                func.count(ApiKey.id),
                func.max(ApiKey.last_used_at),
            )
            .join(UserAccount, ApiKey.user_id == UserAccount.id)
            .where(UserAccount.operator_id.in_(operator_ids))
            .group_by(UserAccount.operator_id)
        ).all()
        api_key_counts = {row[0]: row[1] for row in key_rows if row[0]}
        last_activity = {row[0]: row[2]
                         for row in key_rows if row[0] and row[2]}

    return {
        "items": [
            OperatorSummary(
                id=str(op.id),
                name=op.name,
                display_name=op.display_name,
                avatar_url=op.avatar_url,
                is_root=op.is_root,
                is_active=op.is_active,
                created_at=op.created_at.isoformat() if op.created_at else None,
                user_count=counts.get(op.id, 0),
                active_user_count=active_counts.get(op.id, 0),
                api_key_count=api_key_counts.get(op.id, 0),
                last_activity_at=(
                    last_activity[op.id].isoformat()
                    if op.id in last_activity
                    else None
                ),
            ).model_dump()
            for op in operators
        ]
    }


@router.get("/operator", response_model=OperatorSummary)
def get_current_operator(
    auth=Depends(get_auth_context),
    session: Session = Depends(get_session),
):
    if auth.is_open:
        raise HTTPException(status_code=400, detail="Operator not available")
    if not auth.operator_id and not auth.user_id:
        raise HTTPException(status_code=404, detail="Operator not set")

    operator: Optional[Operator] = None
    if auth.operator_id:
        operator = session.get(Operator, auth.operator_id)

    if operator is None and auth.user_id:
        user = session.get(UserAccount, auth.user_id)
        if not user:
            raise HTTPException(status_code=404, detail="User not found")
        operator = _ensure_operator_for_user(session, user)
        if auth.api_key_id:
            api_key = session.get(ApiKey, auth.api_key_id)
            if api_key and api_key.operator_id is None:
                api_key.operator_id = operator.id
                session.add(api_key)
        session.commit()

    if not operator:
        raise HTTPException(status_code=404, detail="Operator not found")

    total_users, active_users, key_count, last_activity = _get_operator_stats(
        session, operator.id
    )

    return OperatorSummary(
        id=str(operator.id),
        name=operator.name,
        display_name=operator.display_name,
        avatar_url=operator.avatar_url,
        is_root=operator.is_root,
        is_active=operator.is_active,
        created_at=operator.created_at.isoformat() if operator.created_at else None,
        user_count=total_users,
        active_user_count=active_users,
        api_key_count=key_count,
        last_activity_at=last_activity.isoformat() if last_activity else None,
    )


@router.put("/operator", response_model=OperatorSummary)
def update_current_operator(
    payload: OperatorProfileUpdateRequest,
    auth=Depends(get_auth_context),
    session: Session = Depends(get_session),
):
    if auth.is_open:
        raise HTTPException(status_code=400, detail="Operator not available")
    if not auth.operator_id:
        raise HTTPException(status_code=404, detail="Operator not set")
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")

    operator = session.get(Operator, auth.operator_id)
    if not operator:
        raise HTTPException(status_code=404, detail="Operator not found")

    if payload.display_name is not None:
        operator.display_name = payload.display_name
    if payload.avatar_url is not None:
        operator.avatar_url = payload.avatar_url

    session.add(operator)
    session.commit()
    session.refresh(operator)

    total_users, active_users, key_count, last_activity = _get_operator_stats(
        session, operator.id
    )

    return OperatorSummary(
        id=str(operator.id),
        name=operator.name,
        display_name=operator.display_name,
        avatar_url=operator.avatar_url,
        is_root=operator.is_root,
        is_active=operator.is_active,
        created_at=operator.created_at.isoformat() if operator.created_at else None,
        user_count=total_users,
        active_user_count=active_users,
        api_key_count=key_count,
        last_activity_at=last_activity.isoformat() if last_activity else None,
    )


@router.post("/bootstrap")
def bootstrap_security(
    payload: BootstrapRequest,
    session: Session = Depends(get_session),
):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    if session.exec(select(UserAccount.id)).first():
        raise HTTPException(status_code=409, detail="Users already exist")

    password_hash, password_salt = auth_service.hash_password("password")
    user = UserAccount(
        username=payload.username,
        display_name=payload.display_name,
        is_active=True,
        is_admin=True,
        password_hash=password_hash,
        password_salt=password_salt,
    )
    session.add(user)
    session.flush()

    _ensure_operator_for_user(session, user)

    admin_role = Role(
        name="admin",
        description="Full access",
        is_system=True,
    )
    session.add(admin_role)
    session.flush()
    session.add(RolePermission(role_id=admin_role.id, permission="*"))
    session.add(UserRole(user_id=user.id, role_id=admin_role.id))
    session.commit()

    api_key, raw_key = auth_service.create_api_key(
        session,
        user_id=user.id,
        name="bootstrap",
        scopes=["*"],
    )

    return {
        "user": {
            "id": str(user.id),
            "username": user.username,
            "display_name": user.display_name,
        },
        "api_key": {
            "id": str(api_key.id),
            "name": api_key.name,
            "key": raw_key,
            "key_prefix": api_key.key_prefix,
        },
    }


@router.get("/roles")
def list_roles(
    auth=Depends(require_admin),
    session: Session = Depends(get_session),
):
    roles = session.exec(select(Role)).all()
    role_ids = [r.id for r in roles]
    perms = session.exec(select(RolePermission).where(
        RolePermission.role_id.in_(role_ids))).all() if role_ids else []
    perm_map: Dict[UUID, List[str]] = {}
    for row in perms:
        perm_map.setdefault(row.role_id, []).append(row.permission)

    return {
        "items": [
            {
                "id": str(role.id),
                "name": role.name,
                "description": role.description,
                "is_system": role.is_system,
                "permissions": perm_map.get(role.id, []),
            }
            for role in roles
        ]
    }


@router.post("/roles")
def create_role(
    payload: RoleCreateRequest,
    auth=Depends(require_admin),
    session: Session = Depends(get_session),
):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    role = Role(
        name=payload.name,
        description=payload.description,
        is_system=False,
    )
    session.add(role)
    session.flush()

    for perm in payload.permissions:
        session.add(RolePermission(role_id=role.id, permission=perm))

    session.commit()
    session.refresh(role)
    return {"id": str(role.id), "name": role.name}


@router.put("/roles/{role_id}")
def update_role(
    role_id: UUID,
    payload: RoleUpdateRequest,
    auth=Depends(require_admin),
    session: Session = Depends(get_session),
):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    role = session.get(Role, role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    if payload.description is not None:
        role.description = payload.description

    if payload.permissions is not None:
        session.exec(
            RolePermission.__table__.delete().where(RolePermission.role_id == role_id)
        )
        for perm in payload.permissions:
            session.add(RolePermission(role_id=role_id, permission=perm))

    session.add(role)
    session.commit()
    return {"ok": True}


@router.delete("/roles/{role_id}")
def delete_role(
    role_id: UUID,
    confirm: bool = Query(False),
    auth=Depends(require_admin),
    session: Session = Depends(get_session),
):
    """Delete a role. System roles cannot be deleted."""
    if not confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    role = session.get(Role, role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.is_system:
        raise HTTPException(
            status_code=400, detail="Cannot delete system roles")

    # Remove role-permission entries
    session.exec(
        RolePermission.__table__.delete().where(RolePermission.role_id == role_id)
    )
    # Remove user-role assignments
    session.exec(
        UserRole.__table__.delete().where(UserRole.role_id == role_id)
    )
    session.delete(role)
    session.commit()
    return {"ok": True}


@router.get("/users")
def list_users(
    auth=Depends(require_admin),
    session: Session = Depends(get_session),
):
    users = session.exec(select(UserAccount)).all()
    user_ids = [u.id for u in users]
    roles = session.exec(select(UserRole).where(
        UserRole.user_id.in_(user_ids))).all() if user_ids else []
    role_map: Dict[UUID, List[str]] = {}
    role_id_map: Dict[UUID, List[str]] = {}
    if roles:
        role_ids = list({r.role_id for r in roles})
        role_rows = session.exec(
            select(Role).where(Role.id.in_(role_ids))).all()
        role_name_map = {r.id: r.name for r in role_rows}
        for r in roles:
            role_map.setdefault(r.user_id, []).append(
                role_name_map.get(r.role_id, "unknown"))
            role_id_map.setdefault(r.user_id, []).append(str(r.role_id))

    return {
        "items": [
            {
                "id": str(u.id),
                "username": u.username,
                "display_name": u.display_name,
                "avatar_url": u.avatar_url,
                "is_admin": u.is_admin,
                "is_active": u.is_active,
                "operator_id": str(u.operator_id) if u.operator_id else None,
                "roles": role_map.get(u.id, []),
                "role_ids": role_id_map.get(u.id, []),
            }
            for u in users
        ]
    }


@router.put("/users/{user_id}")
def update_user(
    user_id: UUID,
    payload: UserUpdateRequest,
    auth=Depends(require_admin),
    session: Session = Depends(get_session),
):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    user = session.get(UserAccount, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.avatar_url is not None:
        user.avatar_url = payload.avatar_url
    if payload.is_admin is not None:
        user.is_admin = payload.is_admin
    if payload.role_ids is not None:
        session.exec(UserRole.__table__.delete().where(
            UserRole.user_id == user_id))
        for role_id in payload.role_ids:
            session.add(UserRole(user_id=user_id, role_id=role_id))
    if payload.password:
        password_hash, password_salt = auth_service.hash_password(
            payload.password)
        user.password_hash = password_hash
        user.password_salt = password_salt
    session.add(user)
    session.commit()
    session.refresh(user)
    return {
        "id": str(user.id),
        "username": user.username,
        "display_name": user.display_name,
        "avatar_url": user.avatar_url,
        "is_admin": user.is_admin,
    }


@router.patch("/users/{user_id}/activate")
def toggle_user_active(
    user_id: UUID,
    active: bool = Query(..., description="Set user active or inactive"),
    confirm: bool = Query(False),
    auth=Depends(require_admin),
    session: Session = Depends(get_session),
):
    """Activate or deactivate a user account."""
    if not confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    user = session.get(UserAccount, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.is_active = active
    session.add(user)
    session.commit()
    return {
        "id": str(user.id),
        "username": user.username,
        "is_active": user.is_active,
    }


@router.get("/profile", response_model=UserProfileResponse)
def get_profile(
    auth=Depends(get_auth_context),
    session: Session = Depends(get_session),
):
    if not auth.user_id:
        raise HTTPException(status_code=403, detail="No authenticated user")
    user = session.get(UserAccount, auth.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return UserProfileResponse(
        id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
    )


@router.put("/profile", response_model=UserProfileResponse)
def update_profile(
    payload: UserProfileUpdateRequest,
    auth=Depends(get_auth_context),
    session: Session = Depends(get_session),
):
    if not auth.user_id:
        raise HTTPException(status_code=403, detail="No authenticated user")
    user = session.get(UserAccount, auth.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.avatar_url is not None:
        user.avatar_url = payload.avatar_url
    session.add(user)
    session.commit()
    session.refresh(user)
    return UserProfileResponse(
        id=str(user.id),
        username=user.username,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
    )


@router.post("/users")
def create_user(
    payload: UserCreateRequest,
    auth=Depends(require_admin),
    session: Session = Depends(get_session),
):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    user = UserAccount(
        username=payload.username,
        display_name=payload.display_name,
        is_active=True,
        is_admin=payload.is_admin,
    )
    session.add(user)
    session.flush()

    _ensure_operator_for_user(session, user)

    if payload.password:
        password_hash, password_salt = auth_service.hash_password(
            payload.password)
        user.password_hash = password_hash
        user.password_salt = password_salt
        session.add(user)

    for role_id in payload.role_ids:
        session.add(UserRole(user_id=user.id, role_id=role_id))

    session.commit()
    session.refresh(user)
    return {"id": str(user.id), "username": user.username}


@router.get("/keys")
def list_api_keys(
    auth=Depends(require_admin),
    session: Session = Depends(get_session),
):
    rows = session.exec(
        select(ApiKey, UserAccount)
        .join(UserAccount, ApiKey.user_id == UserAccount.id, isouter=True)
    ).all()
    keys = [row[0] for row in rows]
    user_map: Dict[UUID, Optional[UserAccount]] = {
        row[0].id: row[1] for row in rows
    }
    key_ids = [k.id for k in keys]
    perms = session.exec(select(ApiKeyPermission).where(
        ApiKeyPermission.api_key_id.in_(key_ids))).all() if key_ids else []
    perm_map: Dict[UUID, List[str]] = {}
    for row in perms:
        perm_map.setdefault(row.api_key_id, []).append(row.permission)

    return {
        "items": [
            {
                "id": str(k.id),
                "user_id": str(k.user_id),
                "operator_id": str(k.operator_id) if k.operator_id else (
                    str(user_map.get(k.id).operator_id)
                    if user_map.get(k.id) and user_map.get(k.id).operator_id
                    else None
                ),
                "owner_username": user_map.get(k.id).username if user_map.get(k.id) else None,
                "owner_display_name": user_map.get(k.id).display_name if user_map.get(k.id) else None,
                "owner_avatar_url": user_map.get(k.id).avatar_url if user_map.get(k.id) else None,
                "name": k.name,
                "key_prefix": k.key_prefix,
                "scopes": k.scopes,
                "is_active": k.is_active,
                "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
                "expires_at": k.expires_at.isoformat() if k.expires_at else None,
                "permissions": perm_map.get(k.id, []),
            }
            for k in keys
        ]
    }


@router.post("/keys/self")
def create_api_key_self(
    payload: ApiKeySelfCreateRequest,
    auth=Depends(get_auth_context),
    session: Session = Depends(get_session),
):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    if not auth.user_id:
        raise HTTPException(status_code=403, detail="No authenticated user")
    if not (auth.is_open or auth.is_admin or getattr(auth, "is_root", False) or auth.can("keys:create:self")):
        raise HTTPException(status_code=403, detail="Not authorized")
    _validate_self_key_permissions(auth, payload.permissions, payload.scopes)
    if auth.user_id:
        user = session.get(UserAccount, auth.user_id)
        if user and not user.operator_id:
            _ensure_operator_for_user(session, user)
            session.commit()
    api_key, raw_key = auth_service.create_api_key(
        session,
        user_id=auth.user_id,
        name=payload.name,
        scopes=payload.scopes,
        permissions=payload.permissions,
    )
    return {
        "id": str(api_key.id),
        "name": api_key.name,
        "key": raw_key,
        "key_prefix": api_key.key_prefix,
        "created_at": api_key.created_at.isoformat(),
    }


@router.post("/keys")
def create_api_key(
    payload: ApiKeyCreateRequest,
    auth=Depends(require_admin),
    session: Session = Depends(get_session),
):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    api_key, raw_key = auth_service.create_api_key(
        session,
        user_id=payload.user_id,
        name=payload.name,
        scopes=payload.scopes,
        permissions=payload.permissions,
    )

    return {
        "id": str(api_key.id),
        "name": api_key.name,
        "key": raw_key,
        "key_prefix": api_key.key_prefix,
        "created_at": api_key.created_at.isoformat(),
    }


@router.patch("/keys/{key_id}")
def disable_api_key(
    key_id: UUID,
    payload: ApiKeyDisableRequest,
    auth=Depends(require_admin),
    session: Session = Depends(get_session),
):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    api_key = session.get(ApiKey, key_id)
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    api_key.is_active = not payload.disabled
    api_key.last_used_at = api_key.last_used_at or datetime.now(timezone.utc)
    session.add(api_key)
    session.commit()
    return {"ok": True, "disabled": payload.disabled}


@router.delete("/keys/{key_id}")
def delete_api_key(
    key_id: UUID,
    confirm: bool = Query(False),
    auth=Depends(require_admin),
    session: Session = Depends(get_session),
):
    if not confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    api_key = session.get(ApiKey, key_id)
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    session.exec(ApiKeyPermission.__table__.delete().where(
        ApiKeyPermission.api_key_id == key_id))
    session.delete(api_key)
    session.commit()
    return {"ok": True}


@router.post("/login")
def login_with_password(
    payload: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
):
    if not auth_service.is_auth_enabled():
        raise HTTPException(
            status_code=400, detail="Authentication is disabled")

    user = session.exec(
        select(UserAccount).where(UserAccount.username == payload.username)
    ).first()
    if not user or not user.is_active:
        raise HTTPException(status_code=403, detail="Invalid credentials")

    if not auth_service.verify_password(
        payload.password, user.password_hash, user.password_salt
    ):
        raise HTTPException(status_code=403, detail="Invalid credentials")

    operator_id = user.operator_id
    auth_session, session_token = auth_service.create_auth_session(
        session,
        user_id=user.id,
        operator_id=operator_id,
        api_key_id=None,
        session_name=payload.session_name or "password-login",
        auth_method="password",
        user_agent=request.headers.get("user-agent"),
        ip_address=_request_client_ip(request),
    )

    secure = request.url.scheme == "https"
    samesite = "none" if secure else "lax"
    response.set_cookie(
        COOKIE_NAME,
        session_token,
        httponly=True,
        secure=secure,
        samesite=samesite,
        path="/",
    )

    return {
        "ok": True,
        "key": session_token,
        "key_prefix": session_token[:8],
        "session_id": str(auth_session.id),
        "expires_at": auth_session.expires_at.isoformat() if auth_session.expires_at else None,
        "user": {
            "id": str(user.id),
            "username": user.username,
            "display_name": user.display_name,
        },
    }


@router.post("/logout")
def logout(
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
    auth=Depends(get_auth_context),
):
    """
    Log out the current session by revoking the active API key and clearing the auth cookie.
    """
    if not auth_service.is_auth_enabled():
        raise HTTPException(
            status_code=400, detail="Authentication is disabled")

    if not auth.authenticated:
        raise HTTPException(status_code=401, detail="Not authenticated")

    raw_cookie_credential = request.cookies.get(COOKIE_NAME)

    if auth.session_id:
        current_session = session.get(AuthSession, auth.session_id)
        if current_session:
            auth_service.revoke_auth_session_with_reason(
                session, current_session, "logout"
            )
    elif raw_cookie_credential and raw_cookie_credential.startswith(auth_service.SESSION_TOKEN_PREFIX):
        current_session = auth_service.lookup_auth_session(
            session, raw_cookie_credential)
        if current_session:
            auth_service.revoke_auth_session_with_reason(
                session, current_session, "logout"
            )

    # Legacy compatibility: if old disposable login API keys still exist, revoke on logout.
    if auth.api_key_id and auth.api_key_name == "login":
        api_key = session.get(ApiKey, auth.api_key_id)
        if api_key:
            for perm in session.exec(select(ApiKeyPermission).where(
                    ApiKeyPermission.api_key_id == api_key.id)):
                session.delete(perm)
            session.delete(api_key)
            session.commit()

    # Clear the auth cookie regardless
    secure = request.url.scheme == "https"
    samesite = "none" if secure else "lax"
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        httponly=True,
        secure=secure,
        samesite=samesite,
    )

    return {"ok": True, "message": "Logged out successfully"}


@router.get("/sessions")
def list_my_sessions(
    auth=Depends(get_auth_context),
    session: Session = Depends(get_session),
):
    if not auth.user_id:
        raise HTTPException(status_code=403, detail="No authenticated user")

    rows = session.exec(
        select(AuthSession)
        .where(AuthSession.user_id == auth.user_id)
        .order_by(AuthSession.last_used_at.desc())
    ).all()

    items = [
        AuthSessionSummary(
            id=str(row.id),
            session_name=row.session_name,
            auth_method=row.auth_method,
            user_agent=row.user_agent,
            ip_address=row.ip_address,
            created_at=row.created_at.isoformat() if row.created_at else None,
            last_used_at=row.last_used_at.isoformat()
            if row.last_used_at
            else None,
            expires_at=row.expires_at.isoformat() if row.expires_at else None,
            revoked_at=row.revoked_at.isoformat() if row.revoked_at else None,
            revoked_reason=row.revoked_reason,
            rotated_from_id=str(row.rotated_from_id)
            if row.rotated_from_id
            else None,
            api_key_id=str(row.api_key_id) if row.api_key_id else None,
            current=bool(auth.session_id and row.id == auth.session_id),
        )
        for row in rows
    ]
    return {"items": [item.model_dump() for item in items]}


@router.post("/sessions/refresh")
def refresh_my_session(
    request: Request,
    response: Response,
    payload: RefreshSessionRequest,
    auth=Depends(get_auth_context),
    session: Session = Depends(get_session),
):
    if not auth_service.is_auth_enabled():
        raise HTTPException(
            status_code=400, detail="Authentication is disabled")
    if not auth.session_id:
        raise HTTPException(
            status_code=400, detail="Current credential is not a session")

    current = session.get(AuthSession, auth.session_id)
    if not current or current.revoked_at is not None:
        raise HTTPException(status_code=401, detail="Session is not active")

    rotated, raw_token = auth_service.rotate_auth_session(
        session,
        auth_session=current,
        session_name=payload.session_name,
        user_agent=request.headers.get("user-agent"),
        ip_address=_request_client_ip(request),
    )

    secure = request.url.scheme == "https"
    samesite = "none" if secure else "lax"
    response.set_cookie(
        COOKIE_NAME,
        raw_token,
        httponly=True,
        secure=secure,
        samesite=samesite,
        path="/",
    )

    return {
        "ok": True,
        "session_id": str(rotated.id),
        "key": raw_token,
        "key_prefix": raw_token[:8],
        "expires_at": rotated.expires_at.isoformat() if rotated.expires_at else None,
    }


@router.delete("/sessions/{session_id}")
def revoke_session(
    session_id: UUID,
    confirm: bool = Query(False),
    auth=Depends(get_auth_context),
    session: Session = Depends(get_session),
):
    if not confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")

    target = session.get(AuthSession, session_id)
    if not target:
        raise HTTPException(status_code=404, detail="Session not found")

    if not auth.is_admin and auth.user_id != target.user_id:
        raise HTTPException(status_code=403, detail="Not authorized")

    auth_service.revoke_auth_session_with_reason(session, target, "revoked")
    return {"ok": True, "id": str(target.id), "revoked": True}


@router.post("/logout-all")
def logout_all_sessions(
    request: Request,
    response: Response,
    payload: LogoutAllRequest,
    auth=Depends(get_auth_context),
    session: Session = Depends(get_session),
):
    if not payload.confirm:
        raise HTTPException(status_code=400, detail="Confirmation required")
    if not auth.user_id:
        raise HTTPException(status_code=403, detail="No authenticated user")

    target_user_id = payload.user_id or auth.user_id
    if payload.user_id and not auth.is_admin:
        raise HTTPException(status_code=403, detail="Admin access required")

    exclude_session_id = None if payload.include_current else auth.session_id
    revoked_count = auth_service.revoke_all_auth_sessions(
        session,
        user_id=target_user_id,
        exclude_session_id=exclude_session_id,
        reason="logout_all",
    )

    if payload.include_current:
        secure = request.url.scheme == "https"
        samesite = "none" if secure else "lax"
        response.delete_cookie(
            COOKIE_NAME,
            path="/",
            httponly=True,
            secure=secure,
            samesite=samesite,
        )

    return {
        "ok": True,
        "revoked": revoked_count,
        "target_user_id": str(target_user_id),
        "included_current": payload.include_current,
    }


@router.get("/clients/me")
def list_operator_clients_for_session(
    auth=Depends(get_auth_context),
    session: Session = Depends(get_session),
):
    if not auth.user_id:
        raise HTTPException(status_code=403, detail="No authenticated user")

    current_user = session.get(UserAccount, auth.user_id)
    if not current_user:
        raise HTTPException(status_code=404, detail="User not found")

    if not current_user.operator_id:
        return {
            "items": [
                {
                    "id": str(current_user.id),
                    "username": current_user.username,
                    "display_name": current_user.display_name,
                    "is_admin": bool(current_user.is_admin),
                    "is_active": bool(current_user.is_active),
                    "current": True,
                }
            ]
        }

    users = session.exec(
        select(UserAccount)
        .where(UserAccount.operator_id == current_user.operator_id)
        .order_by(UserAccount.username)
    ).all()
    return {
        "items": [
            {
                "id": str(user.id),
                "username": user.username,
                "display_name": user.display_name,
                "is_admin": bool(user.is_admin),
                "is_active": bool(user.is_active),
                "current": bool(auth.user_id == user.id),
            }
            for user in users
        ]
    }


@router.post("/sessions/switch-client")
def switch_client_session(
    request: Request,
    response: Response,
    payload: SwitchClientSessionRequest,
    auth=Depends(get_auth_context),
    session: Session = Depends(get_session),
):
    if not auth_service.is_auth_enabled():
        raise HTTPException(
            status_code=400, detail="Authentication is disabled")
    if not auth.user_id:
        raise HTTPException(status_code=403, detail="No authenticated user")

    source_user = session.get(UserAccount, auth.user_id)
    if not source_user:
        raise HTTPException(status_code=404, detail="Current user not found")

    target_user = session.get(UserAccount, payload.user_id)
    if not target_user or not target_user.is_active:
        raise HTTPException(status_code=404, detail="Target user not found")

    if source_user.operator_id != target_user.operator_id:
        raise HTTPException(
            status_code=403, detail="Target user is outside current operator")

    if not auth.is_admin and target_user.id != source_user.id:
        raise HTTPException(
            status_code=403, detail="Admin access required to switch client")

    if auth.session_id:
        current_session = session.get(AuthSession, auth.session_id)
        if current_session:
            auth_service.revoke_auth_session_with_reason(
                session, current_session, "switch_identity"
            )

    new_session, raw_token = auth_service.create_auth_session(
        session,
        user_id=target_user.id,
        operator_id=target_user.operator_id,
        api_key_id=None,
        session_name=payload.session_name or f"switch:{target_user.username}",
        auth_method="switch",
        user_agent=request.headers.get("user-agent"),
        ip_address=_request_client_ip(request),
    )

    secure = request.url.scheme == "https"
    samesite = "none" if secure else "lax"
    response.set_cookie(
        COOKIE_NAME,
        raw_token,
        httponly=True,
        secure=secure,
        samesite=samesite,
        path="/",
    )

    return {
        "ok": True,
        "session_id": str(new_session.id),
        "key": raw_token,
        "key_prefix": raw_token[:8],
        "user": {
            "id": str(target_user.id),
            "username": target_user.username,
            "display_name": target_user.display_name,
            "is_admin": bool(target_user.is_admin),
        },
        "operator_id": str(target_user.operator_id) if target_user.operator_id else None,
    }


# ── Permissions catalogue ──────────────────────────────────────────────

@router.get("/permissions")
def list_available_permissions(auth=Depends(get_auth_context)):
    """
    Return the full catalogue of defined permission constants and preset role templates.
    Useful for UI role-builder or debugging.
    """
    from embeddr.auth.permissions import Permissions

    return {
        "permissions": Permissions.all_permissions(),
        "presets": {
            "admin": sorted(Permissions.admin_permissions()),
            "editor": sorted(Permissions.editor_permissions()),
            "viewer": sorted(Permissions.viewer_permissions()),
        },
    }
