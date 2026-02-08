"""add operator model and ownership

Revision ID: 4b2c1d9e7f0a
Revises: d4a2b6c1f9e0
Create Date: 2026-02-04
"""

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "4b2c1d9e7f0a"
down_revision = "d4a2b6c1f9e0"
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

    if "operator" not in tables:
        op.create_table(
            "operator",
            sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
            sa.Column("name", sqlmodel.sql.sqltypes.AutoString(),
                      nullable=False),
            sa.Column("display_name",
                      sqlmodel.sql.sqltypes.AutoString(), nullable=True),
            sa.Column("is_root", sa.Boolean(), nullable=False,
                      server_default=sa.false()),
            sa.Column("is_active", sa.Boolean(),
                      nullable=False, server_default=sa.true()),
            sa.Column("created_at", sa.DateTime(
                timezone=True), nullable=False),
        )
        tables.add("operator")
    if not has_index("operator", "ix_operator_name"):
        op.create_index("ix_operator_name", "operator", ["name"], unique=True)

    if "useraccount" in tables and not has_column("useraccount", "operator_id"):
        if dialect == "sqlite":
            op.execute('DROP TABLE IF EXISTS "_alembic_tmp_useraccount"')
        with op.batch_alter_table("useraccount") as batch_op:
            batch_op.add_column(
                sa.Column("operator_id", sa.Uuid(), nullable=True))
            batch_op.create_index("ix_useraccount_operator_id", [
                                  "operator_id"], unique=False)
            batch_op.create_foreign_key(
                "fk_useraccount_operator_id_operator",
                "operator",
                ["operator_id"],
                ["id"],
            )

    if "apikey" in tables and not has_column("apikey", "operator_id"):
        if dialect == "sqlite":
            op.execute('DROP TABLE IF EXISTS "_alembic_tmp_apikey"')
        with op.batch_alter_table("apikey") as batch_op:
            batch_op.add_column(
                sa.Column("operator_id", sa.Uuid(), nullable=True))
            batch_op.create_index("ix_apikey_operator_id", [
                                  "operator_id"], unique=False)
            batch_op.create_foreign_key(
                "fk_apikey_operator_id_operator",
                "operator",
                ["operator_id"],
                ["id"],
            )

    if "artifact" in tables and not has_column("artifact", "owner_operator_id"):
        if dialect == "sqlite":
            op.execute('DROP TABLE IF EXISTS "_alembic_tmp_artifact"')
        with op.batch_alter_table("artifact") as batch_op:
            batch_op.add_column(
                sa.Column("owner_operator_id", sa.Uuid(), nullable=True))
            batch_op.create_index("ix_artifact_owner_operator_id", [
                                  "owner_operator_id"], unique=False)
            batch_op.create_foreign_key(
                "fk_artifact_owner_operator_id_operator",
                "operator",
                ["owner_operator_id"],
                ["id"],
            )

    if "collection" in tables and not has_column("collection", "owner_operator_id"):
        if dialect == "sqlite":
            op.execute('DROP TABLE IF EXISTS "_alembic_tmp_collection"')
        with op.batch_alter_table("collection") as batch_op:
            batch_op.add_column(
                sa.Column("owner_operator_id", sa.Uuid(), nullable=True))
            batch_op.create_index("ix_collection_owner_operator_id", [
                                  "owner_operator_id"], unique=False)
            batch_op.create_foreign_key(
                "fk_collection_owner_operator_id_operator",
                "operator",
                ["owner_operator_id"],
                ["id"],
            )


def downgrade() -> None:
    with op.batch_alter_table("collection") as batch_op:
        batch_op.drop_constraint(
            "fk_collection_owner_operator_id_operator", type_="foreignkey")
        batch_op.drop_index("ix_collection_owner_operator_id")
        batch_op.drop_column("owner_operator_id")

    with op.batch_alter_table("artifact") as batch_op:
        batch_op.drop_constraint(
            "fk_artifact_owner_operator_id_operator", type_="foreignkey")
        batch_op.drop_index("ix_artifact_owner_operator_id")
        batch_op.drop_column("owner_operator_id")

    with op.batch_alter_table("apikey") as batch_op:
        batch_op.drop_constraint(
            "fk_apikey_operator_id_operator", type_="foreignkey")
        batch_op.drop_index("ix_apikey_operator_id")
        batch_op.drop_column("operator_id")

    with op.batch_alter_table("useraccount") as batch_op:
        batch_op.drop_constraint(
            "fk_useraccount_operator_id_operator", type_="foreignkey")
        batch_op.drop_index("ix_useraccount_operator_id")
        batch_op.drop_column("operator_id")

    op.drop_index("ix_operator_name", table_name="operator")
    op.drop_table("operator")
