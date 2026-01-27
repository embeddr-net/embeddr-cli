"""add automation metadata_json

Revision ID: e2b5c3d1a9f0
Revises: f1b4e8c3c2a1
Create Date: 2026-02-05
"""

from alembic import op
import sqlalchemy as sa


revision = "e2b5c3d1a9f0"
down_revision = "f1b4e8c3c2a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("automation") as batch_op:
        batch_op.add_column(
            sa.Column(
                "metadata_json",
                sa.JSON(),
                nullable=False,
                server_default="{}",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("automation") as batch_op:
        batch_op.drop_column("metadata_json")
