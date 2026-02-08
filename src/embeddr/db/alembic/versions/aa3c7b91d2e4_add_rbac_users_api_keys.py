"""add rbac users and api keys

Revision ID: aa3c7b91d2e4
Revises: f1b4e8c3c2a1
Create Date: 2026-02-01
"""

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "aa3c7b91d2e4"
down_revision = "f1b4e8c3c2a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    dialect = bind.dialect.name

    def has_index(table_name: str, index_name: str) -> bool:
        try:
            indexes = inspector.get_indexes(table_name)
        except Exception:
            return False
        return any(idx.get("name") == index_name for idx in indexes)

    def has_column(table_name: str, column_name: str) -> bool:
        try:
            cols = inspector.get_columns(table_name)
        except Exception:
            return False
        return any(col.get("name") == column_name for col in cols)
    if "useraccount" not in tables:
        op.create_table(
            "useraccount",
            sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
            sa.Column("username", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("display_name", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        tables.add("useraccount")
    if not has_index("useraccount", "ix_useraccount_username"):
        op.create_index("ix_useraccount_username", "useraccount", ["username"], unique=True)

    if "role" not in tables:
        op.create_table(
            "role",
            sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
            sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("description", sqlmodel.sql.sqltypes.AutoString(), nullable=True),
            sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )
        tables.add("role")
    if not has_index("role", "ix_role_name"):
        op.create_index("ix_role_name", "role", ["name"], unique=True)

    if "rolepermission" not in tables:
        op.create_table(
            "rolepermission",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("role_id", sa.Uuid(), nullable=False),
            sa.Column("permission", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.ForeignKeyConstraint(["role_id"], ["role.id"]),
        )
        tables.add("rolepermission")
    if not has_index("rolepermission", "ix_rolepermission_role_id"):
        op.create_index("ix_rolepermission_role_id", "rolepermission", ["role_id"], unique=False)
    if not has_index("rolepermission", "ix_rolepermission_permission"):
        op.create_index("ix_rolepermission_permission", "rolepermission", ["permission"], unique=False)

    if "userrole" not in tables:
        op.create_table(
            "userrole",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("role_id", sa.Uuid(), nullable=False),
            sa.ForeignKeyConstraint(["user_id"], ["useraccount.id"]),
            sa.ForeignKeyConstraint(["role_id"], ["role.id"]),
        )
        tables.add("userrole")
    if not has_index("userrole", "ix_userrole_user_id"):
        op.create_index("ix_userrole_user_id", "userrole", ["user_id"], unique=False)
    if not has_index("userrole", "ix_userrole_role_id"):
        op.create_index("ix_userrole_role_id", "userrole", ["role_id"], unique=False)

    if "apikey" not in tables:
        op.create_table(
            "apikey",
            sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("key_hash", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("key_prefix", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.Column("scopes", sa.JSON(), nullable=False),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["user_id"], ["useraccount.id"]),
        )
        tables.add("apikey")
    if not has_index("apikey", "ix_apikey_user_id"):
        op.create_index("ix_apikey_user_id", "apikey", ["user_id"], unique=False)
    if not has_index("apikey", "ix_apikey_key_hash"):
        op.create_index("ix_apikey_key_hash", "apikey", ["key_hash"], unique=True)
    if not has_index("apikey", "ix_apikey_key_prefix"):
        op.create_index("ix_apikey_key_prefix", "apikey", ["key_prefix"], unique=False)

    if "apikeypermission" not in tables:
        op.create_table(
            "apikeypermission",
            sa.Column("id", sa.Integer(), primary_key=True, nullable=False),
            sa.Column("api_key_id", sa.Uuid(), nullable=False),
            sa.Column("permission", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
            sa.ForeignKeyConstraint(["api_key_id"], ["apikey.id"]),
        )
        tables.add("apikeypermission")
    if not has_index("apikeypermission", "ix_apikeypermission_api_key_id"):
        op.create_index("ix_apikeypermission_api_key_id", "apikeypermission", ["api_key_id"], unique=False)
    if not has_index("apikeypermission", "ix_apikeypermission_permission"):
        op.create_index("ix_apikeypermission_permission", "apikeypermission", ["permission"], unique=False)

    if "artifact" in tables and not has_column("artifact", "owner_user_id"):
        if dialect == "sqlite":
            op.execute('DROP TABLE IF EXISTS "_alembic_tmp_artifact"')
        with op.batch_alter_table("artifact") as batch_op:
            batch_op.add_column(sa.Column("owner_user_id", sa.Uuid(), nullable=True))
            batch_op.create_index("ix_artifact_owner_user_id", ["owner_user_id"], unique=False)
            batch_op.create_foreign_key(
                "fk_artifact_owner_user_id_useraccount",
                "useraccount",
                ["owner_user_id"],
                ["id"],
            )

    if "collection" in tables and not has_column("collection", "owner_user_id"):
        if dialect == "sqlite":
            op.execute('DROP TABLE IF EXISTS "_alembic_tmp_collection"')
        with op.batch_alter_table("collection") as batch_op:
            batch_op.add_column(sa.Column("owner_user_id", sa.Uuid(), nullable=True))
            batch_op.create_index("ix_collection_owner_user_id", ["owner_user_id"], unique=False)
            batch_op.create_foreign_key(
                "fk_collection_owner_user_id_useraccount",
                "useraccount",
                ["owner_user_id"],
                ["id"],
            )


def downgrade() -> None:
    with op.batch_alter_table("collection") as batch_op:
        batch_op.drop_constraint("fk_collection_owner_user_id_useraccount", type_="foreignkey")
        batch_op.drop_index("ix_collection_owner_user_id")
        batch_op.drop_column("owner_user_id")

    with op.batch_alter_table("artifact") as batch_op:
        batch_op.drop_constraint("fk_artifact_owner_user_id_useraccount", type_="foreignkey")
        batch_op.drop_index("ix_artifact_owner_user_id")
        batch_op.drop_column("owner_user_id")

    op.drop_index("ix_apikeypermission_permission", table_name="apikeypermission")
    op.drop_index("ix_apikeypermission_api_key_id", table_name="apikeypermission")
    op.drop_table("apikeypermission")

    op.drop_index("ix_apikey_key_prefix", table_name="apikey")
    op.drop_index("ix_apikey_key_hash", table_name="apikey")
    op.drop_index("ix_apikey_user_id", table_name="apikey")
    op.drop_table("apikey")

    op.drop_index("ix_userrole_role_id", table_name="userrole")
    op.drop_index("ix_userrole_user_id", table_name="userrole")
    op.drop_table("userrole")

    op.drop_index("ix_rolepermission_permission", table_name="rolepermission")
    op.drop_index("ix_rolepermission_role_id", table_name="rolepermission")
    op.drop_table("rolepermission")

    op.drop_index("ix_role_name", table_name="role")
    op.drop_table("role")

    op.drop_index("ix_useraccount_username", table_name="useraccount")
    op.drop_table("useraccount")
