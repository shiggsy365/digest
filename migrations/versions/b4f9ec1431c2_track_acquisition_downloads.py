"""track acquisition downloads

Revision ID: b4f9ec1431c2
Revises: 797f8b8f13d0
Create Date: 2026-08-29
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b4f9ec1431c2"
down_revision: str | None = "797f8b8f13d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("wanted_items") as batch:
        batch.add_column(sa.Column("selected_release_id", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("download_adapter", sa.String(length=40), nullable=True))
        batch.add_column(sa.Column("external_download_id", sa.String(length=300), nullable=True))
        batch.add_column(sa.Column("status_payload_json", sa.Text(), server_default="{}", nullable=False))
        batch.create_foreign_key(
            "fk_wanted_items_selected_release",
            "acquisition_releases",
            ["selected_release_id"],
            ["id"],
            ondelete="SET NULL",
        )
        batch.create_index("ix_wanted_items_selected_release_id", ["selected_release_id"])
        batch.create_index("ix_wanted_items_external_download_id", ["external_download_id"])


def downgrade() -> None:
    with op.batch_alter_table("wanted_items") as batch:
        batch.drop_index("ix_wanted_items_external_download_id")
        batch.drop_index("ix_wanted_items_selected_release_id")
        batch.drop_constraint("fk_wanted_items_selected_release", type_="foreignkey")
        batch.drop_column("status_payload_json")
        batch.drop_column("external_download_id")
        batch.drop_column("download_adapter")
        batch.drop_column("selected_release_id")
