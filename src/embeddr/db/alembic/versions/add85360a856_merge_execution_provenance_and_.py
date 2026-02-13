"""merge execution_provenance and panelsession heads

Revision ID: add85360a856
Revises: a1b2c3d4e5f6, e7a1b3c5d9f2
Create Date: 2026-02-10 14:57:39.659861

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'add85360a856'
down_revision: Union[str, Sequence[str], None] = ('a1b2c3d4e5f6', 'e7a1b3c5d9f2')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
