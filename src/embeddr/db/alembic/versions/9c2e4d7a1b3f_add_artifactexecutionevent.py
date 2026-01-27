"""add artifactexecutionevent table

Revision ID: 9c2e4d7a1b3f
Revises: f1b4e8c3c2a1
Create Date: 2026-01-23
"""

from alembic import op
import sqlalchemy as sa


revision = "9c2e4d7a1b3f"
down_revision = "f1b4e8c3c2a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "artifactexecutionevent",
        sa.Column("id", sa.UUID(), primary_key=True, nullable=False),
        sa.Column("execution_id", sa.UUID(), nullable=False),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("level", sa.String(), nullable=False, server_default="info"),
        sa.Column("message", sa.String(), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["artifactexecution.id"],
            name="fk_artifactexecutionevent_execution_id_artifactexecution",
        ),
    )
    op.create_index(
        "ix_artifactexecutionevent_execution_id",
        "artifactexecutionevent",
        ["execution_id"],
    )
    op.create_index(
        "ix_artifactexecutionevent_event_type",
        "artifactexecutionevent",
        ["event_type"],
    )
    op.create_index(
        "ix_artifactexecutionevent_level",
        "artifactexecutionevent",
        ["level"],
    )
    op.create_index(
        "ix_artifactexecutionevent_created_at",
        "artifactexecutionevent",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_artifactexecutionevent_created_at",
                  table_name="artifactexecutionevent")
    op.drop_index("ix_artifactexecutionevent_level",
                  table_name="artifactexecutionevent")
    op.drop_index("ix_artifactexecutionevent_event_type",
                  table_name="artifactexecutionevent")
    op.drop_index("ix_artifactexecutionevent_execution_id",
                  table_name="artifactexecutionevent")
    op.drop_table("artifactexecutionevent")
