"""drop legacy tables

Revision ID: c9f3b1b0d7e0
Revises: b8c5529c95fa
Create Date: 2026-01-21
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "c9f3b1b0d7e0"
down_revision = "b8c5529c95fa"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Legacy tables to remove (artifact-first model replaces these)
    drop_tables = [
        "imagelineage",
        "datasetitem",
        "collectionitem",
        "generation",
        "workflow",
        "dataset",
        "localimage",
        "librarypath",
        "collection",
    ]

    dialect = op.get_bind().dialect.name
    use_cascade = dialect not in {"sqlite"}

    for table in drop_tables:
        if use_cascade:
            op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
        else:
            op.execute(f'DROP TABLE IF EXISTS "{table}"')


def downgrade() -> None:
    # Irreversible without a backup.
    raise RuntimeError("Irreversible migration: legacy tables were dropped.")
