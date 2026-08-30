"""add per-user Kobo sync shelf

Revision ID: c84b276901f3
Revises: b30e6c77a1af
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c84b276901f3"
down_revision: str | None = "b30e6c77a1af"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.add_column(sa.Column("kobo_sync_shelf_id", sa.Integer(), nullable=True))
        batch.create_foreign_key(
            "fk_users_kobo_sync_shelf_id_shelves",
            "shelves",
            ["kobo_sync_shelf_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_users_kobo_sync_shelf_id", ["kobo_sync_shelf_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.drop_index("ix_users_kobo_sync_shelf_id")
        batch.drop_constraint("fk_users_kobo_sync_shelf_id_shelves", type_="foreignkey")
        batch.drop_column("kobo_sync_shelf_id")
