"""add pluginconfig config_id

Revision ID: 1c2d3e4f5a6b
Revises: 8f1c2b4d5e6f
Create Date: 2026-01-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel

# revision identifiers, used by Alembic.
revision: str = "1c2d3e4f5a6b"
down_revision: Union[str, Sequence[str], None] = "8f1c2b4d5e6f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    with op.batch_alter_table("pluginconfig", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "config_id",
                sqlmodel.sql.sqltypes.AutoString(),
                nullable=False,
                server_default="default",
            )
        )
        batch_op.create_index(
            batch_op.f("ix_pluginconfig_config_id"), ["config_id"], unique=False
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("pluginconfig", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_pluginconfig_config_id"))
        batch_op.drop_column("config_id")
