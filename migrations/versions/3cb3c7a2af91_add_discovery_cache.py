"""add discovery cache

Revision ID: 3cb3c7a2af91
Revises: f9a72c61d04e
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "3cb3c7a2af91"
down_revision: str | None = "f9a72c61d04e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "discovery_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("category", sa.String(length=100), nullable=False),
        sa.Column("source_id", sa.String(length=160), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("authors_json", sa.Text(), nullable=False),
        sa.Column("publication_date", sa.String(length=40), nullable=True),
        sa.Column("cover_url", sa.Text(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("provider", "kind", "category", "source_id"),
    )
    op.create_index("ix_discovery_items_provider", "discovery_items", ["provider"])
    op.create_index("ix_discovery_items_kind", "discovery_items", ["kind"])
    op.create_index("ix_discovery_items_category", "discovery_items", ["category"])
    op.create_index("ix_discovery_items_fetched_at", "discovery_items", ["fetched_at"])


def downgrade() -> None:
    op.drop_index("ix_discovery_items_fetched_at", table_name="discovery_items")
    op.drop_index("ix_discovery_items_category", table_name="discovery_items")
    op.drop_index("ix_discovery_items_kind", table_name="discovery_items")
    op.drop_index("ix_discovery_items_provider", table_name="discovery_items")
    op.drop_table("discovery_items")
