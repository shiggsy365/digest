"""assign api tokens to users

Revision ID: b30e6c77a1af
Revises: 65dd1de6cffd
Create Date: 2026-08-28
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b30e6c77a1af"
down_revision: str | None = "65dd1de6cffd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("api_tokens", sa.Column("user_id", sa.Integer(), nullable=True))
    op.execute(sa.text("UPDATE api_tokens SET user_id = created_by"))
    with op.batch_alter_table("api_tokens") as batch:
        batch.alter_column("user_id", existing_type=sa.Integer(), nullable=False)
        batch.create_foreign_key(
            "fk_api_tokens_user_id_users", "users", ["user_id"], ["id"], ondelete="CASCADE"
        )
        batch.create_index("ix_api_tokens_user_id", ["user_id"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("api_tokens") as batch:
        batch.drop_index("ix_api_tokens_user_id")
        batch.drop_constraint("fk_api_tokens_user_id_users", type_="foreignkey")
        batch.drop_column("user_id")
