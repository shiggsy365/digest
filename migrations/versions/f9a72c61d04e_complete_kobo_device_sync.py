"""complete Kobo device sync tracking

Revision ID: f9a72c61d04e
Revises: e4872a6bd93f
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9a72c61d04e"
down_revision: str | None = "e4872a6bd93f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("kobo_synced_books") as batch:
        batch.add_column(
            sa.Column("archived", sa.Boolean(), server_default=sa.false(), nullable=False)
        )
    op.create_table(
        "kobo_synced_shelves",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("shelf_id", sa.Integer(), nullable=False),
        sa.Column("revision", sa.String(length=64), nullable=False),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "shelf_id"),
    )
    op.create_index("ix_kobo_synced_shelves_user_id", "kobo_synced_shelves", ["user_id"])
    op.create_index("ix_kobo_synced_shelves_shelf_id", "kobo_synced_shelves", ["shelf_id"])


def downgrade() -> None:
    op.drop_index("ix_kobo_synced_shelves_shelf_id", table_name="kobo_synced_shelves")
    op.drop_index("ix_kobo_synced_shelves_user_id", table_name="kobo_synced_shelves")
    op.drop_table("kobo_synced_shelves")
    with op.batch_alter_table("kobo_synced_books") as batch:
        batch.drop_column("archived")
