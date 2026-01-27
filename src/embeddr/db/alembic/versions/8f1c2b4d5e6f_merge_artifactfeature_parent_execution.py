"""merge artifactfeature + parent_execution_id

Revision ID: 8f1c2b4d5e6f
Revises: 3f2b1f4d9c0a, 7da09e23a056
Create Date: 2026-01-21 00:00:00.000000

"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "8f1c2b4d5e6f"
down_revision: Union[str, Sequence[str], None] = (
    "3f2b1f4d9c0a",
    "7da09e23a056",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema (merge heads)."""
    pass


def downgrade() -> None:
    """Downgrade schema (merge heads)."""
    pass
