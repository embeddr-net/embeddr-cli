"""rename clients and credentials

Revision ID: f546fae93712
Revises: 5c1e8a3f9b2d
Create Date: 2026-02-09 04:39:32.368563

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f546fae93712'
down_revision: Union[str, Sequence[str], None] = '5c1e8a3f9b2d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    def _has_table(name: str) -> bool:
        return name in inspector.get_table_names()

    def _get_index(table: str, name: str):
        for idx in inspector.get_indexes(table):
            if idx.get("name") == name:
                return idx
        return None

    def _index_exists(table: str, name: str) -> bool:
        return _get_index(table, name) is not None
        for idx in inspector.get_indexes(table):
            if idx.get("name") == name:
                return True
        return False

    def _rename_index(table: str, old: str, new: str) -> None:
        if not _index_exists(table, old) or _index_exists(table, new):
            return
        idx = _get_index(table, old)
        if not idx:
            return
        try:
            op.execute(f'ALTER INDEX "{old}" RENAME TO "{new}"')
            return
        except Exception:
            pass
        columns = idx.get("column_names") or []
        unique = bool(idx.get("unique", False))
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_index(old)
            if columns:
                batch_op.create_index(new, columns, unique=unique)

    if _has_table("useraccount") and not _has_table("client"):
        op.rename_table("useraccount", "client")
    if _has_table("userrole") and not _has_table("client_scope_preset"):
        op.rename_table("userrole", "client_scope_preset")
    if _has_table("role") and not _has_table("scopepreset"):
        op.rename_table("role", "scopepreset")
    if _has_table("rolepermission") and not _has_table("scopepresetpermission"):
        op.rename_table("rolepermission", "scopepresetpermission")
    if _has_table("apikey") and not _has_table("clientcredential"):
        op.rename_table("apikey", "clientcredential")
    if _has_table("apikeypermission") and not _has_table("clientcredentialpermission"):
        op.rename_table("apikeypermission", "clientcredentialpermission")

    _rename_index("client", "ix_useraccount_operator_id", "ix_client_operator_id")
    _rename_index("client", "ix_useraccount_username", "ix_client_username")
    _rename_index(
        "client_scope_preset",
        "ix_userrole_role_id",
        "ix_client_scope_preset_role_id",
    )
    _rename_index(
        "client_scope_preset",
        "ix_userrole_user_id",
        "ix_client_scope_preset_user_id",
    )
    _rename_index("scopepreset", "ix_role_name", "ix_scopepreset_name")
    _rename_index(
        "scopepresetpermission",
        "ix_rolepermission_permission",
        "ix_scopepresetpermission_permission",
    )
    _rename_index(
        "scopepresetpermission",
        "ix_rolepermission_role_id",
        "ix_scopepresetpermission_role_id",
    )
    _rename_index(
        "clientcredential",
        "ix_apikey_key_hash",
        "ix_clientcredential_key_hash",
    )
    _rename_index(
        "clientcredential",
        "ix_apikey_key_prefix",
        "ix_clientcredential_key_prefix",
    )
    _rename_index(
        "clientcredential",
        "ix_apikey_operator_id",
        "ix_clientcredential_operator_id",
    )
    _rename_index(
        "clientcredential",
        "ix_apikey_user_id",
        "ix_clientcredential_user_id",
    )
    _rename_index(
        "clientcredentialpermission",
        "ix_apikeypermission_api_key_id",
        "ix_clientcredentialpermission_api_key_id",
    )
    _rename_index(
        "clientcredentialpermission",
        "ix_apikeypermission_permission",
        "ix_clientcredentialpermission_permission",
    )


def downgrade() -> None:
    """Downgrade schema."""
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    def _has_table(name: str) -> bool:
        return name in inspector.get_table_names()

    def _get_index(table: str, name: str):
        for idx in inspector.get_indexes(table):
            if idx.get("name") == name:
                return idx
        return None

    def _index_exists(table: str, name: str) -> bool:
        return _get_index(table, name) is not None
        for idx in inspector.get_indexes(table):
            if idx.get("name") == name:
                return True
        return False

    def _rename_index(table: str, old: str, new: str) -> None:
        if not _index_exists(table, old) or _index_exists(table, new):
            return
        idx = _get_index(table, old)
        if not idx:
            return
        try:
            op.execute(f'ALTER INDEX "{old}" RENAME TO "{new}"')
            return
        except Exception:
            pass
        columns = idx.get("column_names") or []
        unique = bool(idx.get("unique", False))
        with op.batch_alter_table(table) as batch_op:
            batch_op.drop_index(old)
            if columns:
                batch_op.create_index(new, columns, unique=unique)

    _rename_index("client", "ix_client_operator_id", "ix_useraccount_operator_id")
    _rename_index("client", "ix_client_username", "ix_useraccount_username")
    _rename_index(
        "client_scope_preset",
        "ix_client_scope_preset_role_id",
        "ix_userrole_role_id",
    )
    _rename_index(
        "client_scope_preset",
        "ix_client_scope_preset_user_id",
        "ix_userrole_user_id",
    )
    _rename_index("scopepreset", "ix_scopepreset_name", "ix_role_name")
    _rename_index(
        "scopepresetpermission",
        "ix_scopepresetpermission_permission",
        "ix_rolepermission_permission",
    )
    _rename_index(
        "scopepresetpermission",
        "ix_scopepresetpermission_role_id",
        "ix_rolepermission_role_id",
    )
    _rename_index(
        "clientcredential",
        "ix_clientcredential_key_hash",
        "ix_apikey_key_hash",
    )
    _rename_index(
        "clientcredential",
        "ix_clientcredential_key_prefix",
        "ix_apikey_key_prefix",
    )
    _rename_index(
        "clientcredential",
        "ix_clientcredential_operator_id",
        "ix_apikey_operator_id",
    )
    _rename_index(
        "clientcredential",
        "ix_clientcredential_user_id",
        "ix_apikey_user_id",
    )
    _rename_index(
        "clientcredentialpermission",
        "ix_clientcredentialpermission_api_key_id",
        "ix_apikeypermission_api_key_id",
    )
    _rename_index(
        "clientcredentialpermission",
        "ix_clientcredentialpermission_permission",
        "ix_apikeypermission_permission",
    )

    if _has_table("clientcredentialpermission") and not _has_table("apikeypermission"):
        op.rename_table("clientcredentialpermission", "apikeypermission")
    if _has_table("clientcredential") and not _has_table("apikey"):
        op.rename_table("clientcredential", "apikey")
    if _has_table("scopepresetpermission") and not _has_table("rolepermission"):
        op.rename_table("scopepresetpermission", "rolepermission")
    if _has_table("scopepreset") and not _has_table("role"):
        op.rename_table("scopepreset", "role")
    if _has_table("client_scope_preset") and not _has_table("userrole"):
        op.rename_table("client_scope_preset", "userrole")
    if _has_table("client") and not _has_table("useraccount"):
        op.rename_table("client", "useraccount")
