"""add operator avatar url

Revision ID: 5c1e8a3f9b2d
Revises: 4b2c1d9e7f0a
Create Date: 2026-02-05
"""

from alembic import op
import sqlalchemy as sa
import sqlmodel

revision = "5c1e8a3f9b2d"
down_revision = "4b2c1d9e7f0a"
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

    if not has_column("operator", "avatar_url"):
        op.add_column(
            "operator",
            sa.Column(
                "avatar_url",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=True,
            ),
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

    if has_column("operator", "avatar_url"):
        op.drop_column("operator", "avatar_url")
