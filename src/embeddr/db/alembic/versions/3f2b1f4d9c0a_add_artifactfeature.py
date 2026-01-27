"""add_artifactfeature

Revision ID: 3f2b1f4d9c0a
Revises: b8c5529c95fa
Create Date: 2026-01-21 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "3f2b1f4d9c0a"
down_revision: Union[str, Sequence[str], None] = "b8c5529c95fa"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = "5a1f6b7c8d9e"


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "artifactfeature",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("feature_type", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("producer_plugin", sa.String(), nullable=True),
        sa.Column("producer_version", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("schema_version", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column("storage_kind", sa.String(), nullable=False),
        sa.Column("storage_ref", sa.JSON(), nullable=False),
        sa.Column("model_name", sa.String(), nullable=True),
        sa.Column("space", sa.String(), nullable=True),
        sa.Column("vector_dim", sa.Integer(), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact.id"], ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_artifactfeature_artifact_id"),
                    "artifactfeature", ["artifact_id"], unique=False)
    op.create_index(op.f("ix_artifactfeature_content_hash"),
                    "artifactfeature", ["content_hash"], unique=False)
    op.create_index(op.f("ix_artifactfeature_feature_type"),
                    "artifactfeature", ["feature_type"], unique=False)
    op.create_index(op.f("ix_artifactfeature_model_name"),
                    "artifactfeature", ["model_name"], unique=False)
    op.create_index(op.f("ix_artifactfeature_name"),
                    "artifactfeature", ["name"], unique=False)
    op.create_index(op.f("ix_artifactfeature_producer_plugin"),
                    "artifactfeature", ["producer_plugin"], unique=False)
    op.create_index(op.f("ix_artifactfeature_space"),
                    "artifactfeature", ["space"], unique=False)
    op.create_index(op.f("ix_artifactfeature_storage_kind"),
                    "artifactfeature", ["storage_kind"], unique=False)
    op.create_index(
        "ix_artifactfeature_artifact_feature_name",
        "artifactfeature",
        ["artifact_id", "feature_type", "name"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_artifactfeature_artifact_feature_name",
                  table_name="artifactfeature")
    op.drop_index(op.f("ix_artifactfeature_storage_kind"),
                  table_name="artifactfeature")
    op.drop_index(op.f("ix_artifactfeature_space"),
                  table_name="artifactfeature")
    op.drop_index(op.f("ix_artifactfeature_producer_plugin"),
                  table_name="artifactfeature")
    op.drop_index(op.f("ix_artifactfeature_name"),
                  table_name="artifactfeature")
    op.drop_index(op.f("ix_artifactfeature_model_name"),
                  table_name="artifactfeature")
    op.drop_index(op.f("ix_artifactfeature_feature_type"),
                  table_name="artifactfeature")
    op.drop_index(op.f("ix_artifactfeature_content_hash"),
                  table_name="artifactfeature")
    op.drop_index(op.f("ix_artifactfeature_artifact_id"),
                  table_name="artifactfeature")
    op.drop_table("artifactfeature")
