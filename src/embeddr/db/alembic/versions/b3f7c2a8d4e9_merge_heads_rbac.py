"""merge heads after rbac

Revision ID: b3f7c2a8d4e9
Revises: 1c9d491b7a06, aa3c7b91d2e4
Create Date: 2026-02-01
"""

from typing import Sequence, Union

revision: str = "b3f7c2a8d4e9"
down_revision: Union[str, Sequence[str], None] = ("1c9d491b7a06", "aa3c7b91d2e4")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
