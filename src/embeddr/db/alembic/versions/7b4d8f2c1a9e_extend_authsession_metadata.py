"""extend authsession metadata

Revision ID: 7b4d8f2c1a9e
Revises: 6f2c91b7a3d4
Create Date: 2026-02-18 12:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "7b4d8f2c1a9e"
down_revision: Union[str, Sequence[str], None] = "6f2c91b7a3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("authsession") as batch_op:
        batch_op.add_column(
            sa.Column("auth_method", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("user_agent", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("ip_address", sa.String(), nullable=True))
        batch_op.add_column(
            sa.Column("rotated_from_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column("revoked_reason", sa.String(), nullable=True))
        batch_op.create_foreign_key(
            "fk_authsession_rotated_from_id_authsession",
            "authsession",
            ["rotated_from_id"],
            ["id"],
        )
        batch_op.create_index("ix_authsession_auth_method", [
                              "auth_method"], unique=False)
        batch_op.create_index("ix_authsession_rotated_from_id", [
                              "rotated_from_id"], unique=False)

    op.execute(
        sa.text(
            "UPDATE authsession SET auth_method = 'session' WHERE auth_method IS NULL"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("authsession") as batch_op:
        batch_op.drop_index("ix_authsession_rotated_from_id")
        batch_op.drop_index("ix_authsession_auth_method")
        batch_op.drop_constraint(
            "fk_authsession_rotated_from_id_authsession", type_="foreignkey"
        )
        batch_op.drop_column("revoked_reason")
        batch_op.drop_column("rotated_from_id")
        batch_op.drop_column("ip_address")
        batch_op.drop_column("user_agent")
        batch_op.drop_column("auth_method")
