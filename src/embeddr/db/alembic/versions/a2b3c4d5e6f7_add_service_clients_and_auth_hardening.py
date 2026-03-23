"""add service_client table, must_change_password, service_client_id on authsession

Revision ID: a2b3c4d5e6f7
Revises: b1c2d3e4f5a6
Create Date: 2026-03-22 00:00:00.000000

Changes:
  - client: add must_change_password boolean column
  - service_client: new table for OAuth2-like external app registration
  - authsession: add service_client_id foreign key column
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, Sequence[str], None] = "b1c2d3e4f5a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- client: add must_change_password ---------------------------------
    op.add_column(
        "client",
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )

    # -- service_client table ---------------------------------------------
    op.create_table(
        "service_client",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.String(), nullable=False),
        sa.Column("client_secret_hash", sa.String(), nullable=False),
        sa.Column("client_secret_prefix", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("description", sa.String(), nullable=True),
        sa.Column("operator_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_user_id", sa.Uuid(), nullable=False),
        sa.Column("redirect_uris", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("allowed_scopes", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "grant_types",
            sa.JSON(),
            nullable=False,
            server_default='["client_credentials"]',
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["operator_id"], ["operator.id"]),
        sa.ForeignKeyConstraint(["created_by_user_id"], ["client.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_service_client_client_id", "service_client", ["client_id"], unique=True
    )
    op.create_index(
        "ix_service_client_name", "service_client", ["name"]
    )
    op.create_index(
        "ix_service_client_operator_id", "service_client", ["operator_id"]
    )
    op.create_index(
        "ix_service_client_created_by_user_id", "service_client", ["created_by_user_id"]
    )
    op.create_index(
        "ix_service_client_client_secret_prefix",
        "service_client",
        ["client_secret_prefix"],
    )

    # -- authsession: add service_client_id --------------------------------
    # NOTE: SQLite does not support ALTER TABLE ADD CONSTRAINT FOREIGN KEY,
    # so we add the column only. The FK is declared in the SQLModel model
    # and will be enforced on newly created tables / future DB engines.
    op.add_column(
        "authsession",
        sa.Column("service_client_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_authsession_service_client_id",
        "authsession",
        ["service_client_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_authsession_service_client_id", table_name="authsession")
    op.drop_column("authsession", "service_client_id")

    op.drop_index("ix_service_client_client_secret_prefix", table_name="service_client")
    op.drop_index("ix_service_client_created_by_user_id", table_name="service_client")
    op.drop_index("ix_service_client_operator_id", table_name="service_client")
    op.drop_index("ix_service_client_name", table_name="service_client")
    op.drop_index("ix_service_client_client_id", table_name="service_client")
    op.drop_table("service_client")

    op.drop_column("client", "must_change_password")
