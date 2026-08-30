"""track Kobo synced books

Revision ID: e4872a6bd93f
Revises: d5f61f2a91bc
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e4872a6bd93f"
down_revision: str | None = "d5f61f2a91bc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kobo_synced_books",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("book_id", sa.String(length=36), nullable=False),
        sa.Column("book_revision", sa.String(length=32), nullable=False),
        sa.Column("reading_revision", sa.String(length=32), nullable=False),
        sa.Column(
            "synced_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "book_id"),
    )
    op.create_index("ix_kobo_synced_books_user_id", "kobo_synced_books", ["user_id"])
    op.create_index("ix_kobo_synced_books_book_id", "kobo_synced_books", ["book_id"])


def downgrade() -> None:
    op.drop_index("ix_kobo_synced_books_book_id", table_name="kobo_synced_books")
    op.drop_index("ix_kobo_synced_books_user_id", table_name="kobo_synced_books")
    op.drop_table("kobo_synced_books")
