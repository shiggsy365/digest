"""use bigint for file statistics

Revision ID: ffc2077a8a67
Revises: a2edc11bfc9e
Create Date: 2026-08-27 23:37:26.838194
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ffc2077a8a67"
down_revision: str | None = "a2edc11bfc9e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("book_files") as batch:
        batch.alter_column(
            "size_bytes",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )
        batch.alter_column(
            "modified_ns",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("book_files") as batch:
        batch.alter_column(
            "modified_ns",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
        )
        batch.alter_column(
            "size_bytes",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
        )
