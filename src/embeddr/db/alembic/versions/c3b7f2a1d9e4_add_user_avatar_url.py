"""add user avatar url

Revision ID: c3b7f2a1d9e4
Revises: b3f7c2a8d4e9
Create Date: 2026-02-01
"""

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "c3b7f2a1d9e4"
down_revision = "b3f7c2a8d4e9"
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

    if not has_column("useraccount", "avatar_url"):
        op.add_column(
            "useraccount",
            sa.Column("avatar_url",
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

    if has_column("useraccount", "avatar_url"):
        op.drop_column("useraccount", "avatar_url")
