"""add acquisition releases

Revision ID: 797f8b8f13d0
Revises: 08d145529ac4
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "797f8b8f13d0"
down_revision: str | None = "08d145529ac4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "acquisition_releases",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("wanted_id", sa.Integer(), nullable=False),
        sa.Column("adapter", sa.String(length=40), nullable=False),
        sa.Column("source", sa.String(length=80), nullable=False),
        sa.Column("source_id", sa.String(length=300), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("format", sa.String(length=20), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("seeders", sa.Integer(), nullable=True),
        sa.Column("download_payload_json", sa.Text(), nullable=False),
        sa.Column("match_score", sa.Float(), nullable=False),
        sa.Column("search_stage", sa.String(length=30), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["wanted_id"], ["wanted_items.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("wanted_id", "adapter", "source_id"),
    )
    op.create_index("ix_acquisition_releases_wanted_id", "acquisition_releases", ["wanted_id"])


def downgrade() -> None:
    op.drop_table("acquisition_releases")
