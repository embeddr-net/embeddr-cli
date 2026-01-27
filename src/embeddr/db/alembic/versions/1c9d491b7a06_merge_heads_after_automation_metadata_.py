"""merge heads after automation metadata_json and artifactexecutionevent

Revision ID: 1c9d491b7a06
Revises: e2b5c3d1a9f0, 9c2e4d7a1b3f
Create Date: 2026-01-26 00:27:28.677475

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1c9d491b7a06'
down_revision: Union[str, Sequence[str], None] = ('e2b5c3d1a9f0', '9c2e4d7a1b3f')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
