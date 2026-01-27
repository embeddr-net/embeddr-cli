"""merge heads after dropping legacy tables

Revision ID: d2e4f6a8b0c1
Revises: 1c2d3e4f5a6b, c9f3b1b0d7e0
Create Date: 2026-01-21
"""

from alembic import op  # noqa: F401

revision = "d2e4f6a8b0c1"
down_revision = ("1c2d3e4f5a6b", "c9f3b1b0d7e0")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    raise RuntimeError("Irreversible merge revision.")
