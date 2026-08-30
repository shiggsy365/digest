"""add all-books Kobo sync option

Revision ID: d2a4137b92e1
Revises: b4f9ec1431c2
Create Date: 2026-08-30
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2a4137b92e1"
down_revision: str | None = "b4f9ec1431c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("kobo_sync_all_books", sa.Boolean(),
                                   server_default=sa.false(), nullable=False))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_column("kobo_sync_all_books")
