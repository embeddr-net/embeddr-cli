"""add artifactrelation traversal indexes

Revision ID: 9d4f2b6c7a8e
Revises: 7b4d8f2c1a9e
Create Date: 2026-03-05 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "9d4f2b6c7a8e"
down_revision: Union[str, Sequence[str], None] = "7b4d8f2c1a9e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_artifactrelation_source_id",
        "artifactrelation",
        ["source_id"],
        unique=False,
    )
    op.create_index(
        "ix_artifactrelation_target_id",
        "artifactrelation",
        ["target_id"],
        unique=False,
    )
    op.create_index(
        "ix_artifactrelation_relation_type_source_namespace",
        "artifactrelation",
        ["relation_type", "source_namespace"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_artifactrelation_relation_type_source_namespace",
        table_name="artifactrelation",
    )
    op.drop_index("ix_artifactrelation_target_id", table_name="artifactrelation")
    op.drop_index("ix_artifactrelation_source_id", table_name="artifactrelation")
