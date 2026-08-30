"""add wanted items

Revision ID: 08d145529ac4
Revises: 3cb3c7a2af91
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "08d145529ac4"
down_revision: str | None = "3cb3c7a2af91"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    status = sa.Enum(
        "WANTED", "SEARCHING", "DOWNLOADING", "IMPORTING", "AVAILABLE", "FAILED", "CANCELLED",
        name="wantedstatus",
    )
    op.create_table(
        "wanted_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("request_key", sa.String(length=64), nullable=False),
        sa.Column("source", sa.String(length=40), nullable=False),
        sa.Column("source_id", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("author", sa.String(length=300), nullable=False),
        sa.Column("isbn", sa.String(length=40), nullable=False),
        sa.Column("cover_url", sa.Text(), nullable=True),
        sa.Column("status", status, nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_search_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("acquired_book_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["acquired_book_id"], ["books.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "request_key"),
    )
    for name, columns in (
        ("ix_wanted_items_user_id", ["user_id"]),
        ("ix_wanted_items_status", ["status"]),
        ("ix_wanted_items_next_search_at", ["next_search_at"]),
        ("ix_wanted_items_acquired_book_id", ["acquired_book_id"]),
    ):
        op.create_index(name, "wanted_items", columns)


def downgrade() -> None:
    op.drop_table("wanted_items")
    sa.Enum(name="wantedstatus").drop(op.get_bind(), checkfirst=True)
