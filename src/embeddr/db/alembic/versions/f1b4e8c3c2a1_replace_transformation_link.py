"""replace transformation link with execution id

Revision ID: f1b4e8c3c2a1
Revises: d2e4f6a8b0c1
Create Date: 2026-01-21
"""

from alembic import op
import sqlalchemy as sa


revision = "f1b4e8c3c2a1"
down_revision = "d2e4f6a8b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "sqlite":
        op.execute('DROP TABLE IF EXISTS "_alembic_tmp_artifactlineage"')
        op.execute('DROP INDEX IF EXISTS "ix_artifactlineage_transformation_id"')

    # Remove the transformation_id column and add execution_id
    with op.batch_alter_table("artifactlineage") as batch_op:
        # batch_op.drop_constraint(
        #     "fk_artifactlineage_transformation_id_transformation",
        #     type_="foreignkey",
        # )
        batch_op.drop_column("transformation_id")
        batch_op.add_column(
            sa.Column("execution_id", sa.UUID(), nullable=True))
        batch_op.create_index(
            "ix_artifactlineage_execution_id", ["execution_id"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_artifactlineage_execution_id_artifactexecution",
            "artifactexecution",
            ["execution_id"],
            ["id"],
        )

    if dialect == "sqlite":
        op.execute('DROP TABLE IF EXISTS "transformation"')
    else:
        op.execute('DROP TABLE IF EXISTS "transformation" CASCADE')


def downgrade() -> None:
    raise RuntimeError("Irreversible migration: transformation link removed.")
