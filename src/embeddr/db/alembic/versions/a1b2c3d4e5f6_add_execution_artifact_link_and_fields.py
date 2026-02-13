"""add execution_artifact_link table and execution provenance fields

Revision ID: a1b2c3d4e5f6
Revises: f546fae93712
Create Date: 2026-02-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import sqlmodel


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'f546fae93712'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- New table: execution_artifact_link ---
    op.create_table(
        'execution_artifact_link',
        sa.Column('id', sa.Uuid(), nullable=False),
        sa.Column('execution_id', sa.Uuid(), nullable=False),
        sa.Column('artifact_id', sa.Uuid(), nullable=False),
        sa.Column('action', sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column('detail', sqlmodel.sql.sqltypes.AutoString(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['execution_id'], ['artifactexecution.id']),
        sa.ForeignKeyConstraint(['artifact_id'], ['artifact.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_execution_artifact_link_execution_id',
                    'execution_artifact_link', ['execution_id'])
    op.create_index('ix_execution_artifact_link_artifact_id',
                    'execution_artifact_link', ['artifact_id'])
    op.create_index('ix_execution_artifact_link_action',
                    'execution_artifact_link', ['action'])

    # --- New columns on artifactexecution ---
    with op.batch_alter_table('artifactexecution', schema=None) as batch_op:
        batch_op.add_column(sa.Column('operator_id', sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column('api_key_id', sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column('trigger_event_type',
                            sqlmodel.sql.sqltypes.AutoString(), nullable=True))
        batch_op.add_column(sa.Column('tags', sa.JSON(),
                            nullable=True, server_default='{}'))
        batch_op.create_index(
            'ix_artifactexecution_operator_id', ['operator_id'])


def downgrade() -> None:
    with op.batch_alter_table('artifactexecution', schema=None) as batch_op:
        batch_op.drop_index('ix_artifactexecution_operator_id')
        batch_op.drop_column('tags')
        batch_op.drop_column('trigger_event_type')
        batch_op.drop_column('api_key_id')
        batch_op.drop_column('operator_id')

    op.drop_index('ix_execution_artifact_link_action',
                  table_name='execution_artifact_link')
    op.drop_index('ix_execution_artifact_link_artifact_id',
                  table_name='execution_artifact_link')
    op.drop_index('ix_execution_artifact_link_execution_id',
                  table_name='execution_artifact_link')
    op.drop_table('execution_artifact_link')
