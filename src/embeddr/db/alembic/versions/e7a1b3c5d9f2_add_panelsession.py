"""add panelsession table

Revision ID: e7a1b3c5d9f2
Revises: f546fae93712
Create Date: 2026-02-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7a1b3c5d9f2"
down_revision: Union[str, Sequence[str], None] = "f546fae93712"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    conn = op.get_bind()
    inspector = sa.inspect(conn)

    if "panelsession" in inspector.get_table_names():
        return

    op.create_table(
        "panelsession",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("client_id", sa.String(length=36),
                  sa.ForeignKey("client.id"), nullable=True),
        sa.Column("credential_id", sa.String(length=36),
                  sa.ForeignKey("clientcredential.id"), nullable=True),
        sa.Column("operator_id", sa.String(length=36),
                  sa.ForeignKey("operator.id"), nullable=True),
        sa.Column("panel_id", sa.String(), nullable=False),
        sa.Column("panel_type", sa.String(), nullable=False),
        sa.Column("window_id", sa.String(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("meta", sa.JSON(), nullable=True),
        sa.Column("items", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_active_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
    )

    op.create_index("ix_panelsession_client_id", "panelsession", ["client_id"])
    op.create_index("ix_panelsession_credential_id",
                    "panelsession", ["credential_id"])
    op.create_index("ix_panelsession_operator_id",
                    "panelsession", ["operator_id"])
    op.create_index("ix_panelsession_panel_id", "panelsession", ["panel_id"])
    op.create_index("ix_panelsession_panel_type",
                    "panelsession", ["panel_type"])
    op.create_index("ix_panelsession_window_id", "panelsession", ["window_id"])


def downgrade() -> None:
    op.drop_table("panelsession")
