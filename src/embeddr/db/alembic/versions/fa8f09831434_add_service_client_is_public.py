"""add service_client is_public

Revision ID: fa8f09831434
Revises: b3c4d5e6f7a8
Create Date: 2026-03-23 20:33:12.421191

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'fa8f09831434'
down_revision: Union[str, Sequence[str], None] = 'b3c4d5e6f7a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Only add the missing column — nothing else
    with op.batch_alter_table('service_client', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('is_public', sa.Boolean(), nullable=True, server_default=sa.text('0'))
        )


def downgrade() -> None:
    with op.batch_alter_table('service_client', schema=None) as batch_op:
        batch_op.drop_column('is_public')
