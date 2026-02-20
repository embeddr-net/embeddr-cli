"""add authsession table

Revision ID: 6f2c91b7a3d4
Revises: add85360a856
Create Date: 2026-02-18 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "6f2c91b7a3d4"
down_revision: Union[str, Sequence[str], None] = "add85360a856"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "authsession",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("operator_id", sa.Uuid(), nullable=True),
        sa.Column("api_key_id", sa.Uuid(), nullable=True),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("token_prefix", sa.String(), nullable=False),
        sa.Column("session_name", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["api_key_id"], ["clientcredential.id"]),
        sa.ForeignKeyConstraint(["operator_id"], ["operator.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["client.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_authsession_user_id", "authsession", ["user_id"])
    op.create_index(
        "ix_authsession_operator_id", "authsession", ["operator_id"]
    )
    op.create_index("ix_authsession_api_key_id", "authsession", ["api_key_id"])
    op.create_index("ix_authsession_token_hash",
                    "authsession", ["token_hash"], unique=True)
    op.create_index("ix_authsession_token_prefix",
                    "authsession", ["token_prefix"])


def downgrade() -> None:
    op.drop_index("ix_authsession_token_prefix", table_name="authsession")
    op.drop_index("ix_authsession_token_hash", table_name="authsession")
    op.drop_index("ix_authsession_api_key_id", table_name="authsession")
    op.drop_index("ix_authsession_operator_id", table_name="authsession")
    op.drop_index("ix_authsession_user_id", table_name="authsession")
    op.drop_table("authsession")
