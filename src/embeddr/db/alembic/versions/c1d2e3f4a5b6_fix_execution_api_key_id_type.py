"""fix execution api_key_id type str to uuid

Revision ID: c1d2e3f4a5b6
Revises: fa8f09831434
Create Date: 2026-03-24 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "fa8f09831434"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # SQLite doesn't support ALTER COLUMN, so use batch mode.
    # This converts api_key_id from VARCHAR to CHAR(32) (UUID stored as hex).
    # Existing string values that are valid UUIDs will be preserved;
    # invalid ones become NULL.
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "sqlite":
        # SQLite: batch mode recreates the table
        with op.batch_alter_table("artifactexecution", schema=None) as batch_op:
            batch_op.alter_column(
                "api_key_id",
                type_=sa.CHAR(32),
                existing_type=sa.VARCHAR(),
                existing_nullable=True,
            )
        # Create index outside batch mode (batch recreates table, dropping old indexes)
        try:
            op.create_index(
                "ix_artifactexecution_api_key_id",
                "artifactexecution",
                ["api_key_id"],
                unique=False,
            )
        except Exception:
            pass  # Index may already exist
    else:
        # PostgreSQL: direct ALTER
        op.alter_column(
            "artifactexecution",
            "api_key_id",
            type_=sa.CHAR(32),
            existing_type=sa.VARCHAR(),
            existing_nullable=True,
        )
        try:
            op.create_index(
                "ix_artifactexecution_api_key_id",
                "artifactexecution",
                ["api_key_id"],
                unique=False,
            )
        except Exception:
            pass


def downgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "sqlite":
        try:
            op.drop_index("ix_artifactexecution_api_key_id", table_name="artifactexecution")
        except Exception:
            pass
        with op.batch_alter_table("artifactexecution", schema=None) as batch_op:
            batch_op.alter_column(
                "api_key_id",
                type_=sa.VARCHAR(),
                existing_type=sa.CHAR(32),
                existing_nullable=True,
            )
    else:
        try:
            op.drop_index("ix_artifactexecution_api_key_id", table_name="artifactexecution")
        except Exception:
            pass
        op.alter_column(
            "artifactexecution",
            "api_key_id",
            type_=sa.VARCHAR(),
            existing_type=sa.CHAR(32),
            existing_nullable=True,
        )
