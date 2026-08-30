"""add Kobo reading progress

Revision ID: d5f61f2a91bc
Revises: c84b276901f3
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f61f2a91bc"
down_revision: str | None = "c84b276901f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("reading_states") as batch:
        batch.add_column(sa.Column("progress_percent", sa.Float(), nullable=True))
        batch.add_column(
            sa.Column("location_json", sa.Text(), server_default="{}", nullable=False)
        )
        batch.add_column(sa.Column("spent_reading_minutes", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("remaining_time_minutes", sa.Integer(), nullable=True))
        batch.add_column(
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("reading_states") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("remaining_time_minutes")
        batch.drop_column("spent_reading_minutes")
        batch.drop_column("location_json")
        batch.drop_column("progress_percent")
