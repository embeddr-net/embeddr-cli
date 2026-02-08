"""add user password fields

Revision ID: d4a2b6c1f9e0
Revises: c3b7f2a1d9e4
Create Date: 2026-02-02
"""

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "d4a2b6c1f9e0"
down_revision = "c3b7f2a1d9e4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def has_column(table_name: str, column_name: str) -> bool:
        try:
            cols = inspector.get_columns(table_name)
        except Exception:
            return False
        return any(col.get("name") == column_name for col in cols)

    if not has_column("useraccount", "password_hash"):
        op.add_column(
            "useraccount",
            sa.Column("password_hash",
                      sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        )
    if not has_column("useraccount", "password_salt"):
        op.add_column(
            "useraccount",
            sa.Column("password_salt",
                      sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    def has_column(table_name: str, column_name: str) -> bool:
        try:
            cols = inspector.get_columns(table_name)
        except Exception:
            return False
        return any(col.get("name") == column_name for col in cols)

    if has_column("useraccount", "password_salt"):
        op.drop_column("useraccount", "password_salt")
    if has_column("useraccount", "password_hash"):
        op.drop_column("useraccount", "password_hash")
