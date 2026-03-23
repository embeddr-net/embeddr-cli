"""add authorization_code table for OAuth2 authorization code flow

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
Create Date: 2026-03-22 12:00:00.000000

Changes:
  - authorization_code: new table for OAuth2 authorization code grant with PKCE
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, Sequence[str], None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "authorization_code",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(), nullable=False),
        sa.Column("code_prefix", sa.String(), nullable=False),
        sa.Column("service_client_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("operator_id", sa.Uuid(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("redirect_uri", sa.String(), nullable=False),
        sa.Column("code_challenge", sa.String(), nullable=True),
        sa.Column("code_challenge_method", sa.String(), nullable=False, server_default="S256"),
        sa.Column("state", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["service_client_id"], ["service_client.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["client.id"]),
        sa.ForeignKeyConstraint(["operator_id"], ["operator.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_authorization_code_code_hash",
        "authorization_code",
        ["code_hash"],
        unique=True,
    )
    op.create_index(
        "ix_authorization_code_code_prefix",
        "authorization_code",
        ["code_prefix"],
    )
    op.create_index(
        "ix_authorization_code_service_client_id",
        "authorization_code",
        ["service_client_id"],
    )
    op.create_index(
        "ix_authorization_code_user_id",
        "authorization_code",
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_authorization_code_user_id", table_name="authorization_code")
    op.drop_index("ix_authorization_code_service_client_id", table_name="authorization_code")
    op.drop_index("ix_authorization_code_code_prefix", table_name="authorization_code")
    op.drop_index("ix_authorization_code_code_hash", table_name="authorization_code")
    op.drop_table("authorization_code")
