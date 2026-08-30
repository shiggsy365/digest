"""store edition metadata

Revision ID: 65dd1de6cffd
Revises: ffc2077a8a67
Create Date: 2026-08-28 10:11:39.407095
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "65dd1de6cffd"
down_revision: str | None = "ffc2077a8a67"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "book_files",
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("book_files", "metadata_json")
