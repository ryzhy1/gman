"""Releases: Bamboo deployments correlated with runtime observations.

Revision ID: 0002_releases
Revises: 0001
Create Date: 2026-10-11

Existing events keep release_id NULL: history before this migration is not re-correlated.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0002_releases"
down_revision = "0001"
branch_labels = None
depends_on = None

Json = sa.JSON().with_variant(JSONB(), "postgresql")
Id = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def time(name, nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def text(name):
    return sa.Column(name, sa.String(), nullable=False)


def upgrade():
    op.create_table(
        "releases",
        sa.Column("id", Id, sa.Identity(), primary_key=True),
        text("service_id"), text("environment"), text("key"), text("release"), text("version"),
        text("build_id"), text("build_url"), text("commit"), text("image"), text("image_digest"),
        text("initiator"), text("initiator_kind"), text("status"), text("status_reason"),
        text("bamboo_status"), text("correlation"),
        time("started_at"), time("window_start"), time("rollout_started_at", nullable=True),
        time("bamboo_finished_at", nullable=True), time("finished_at", nullable=True),
        sa.Column("superseded_by", Id, nullable=True),
        sa.Column("progress", Json, nullable=False), time("updated_at"))
    op.create_index("ix_releases_service", "releases",
                    ["service_id", "environment", sa.text("started_at DESC"), sa.text("id DESC")])
    op.create_index("ix_releases_key", "releases", ["service_id", "environment", "key"])
    op.create_index("ix_releases_status", "releases", ["status", "started_at"])
    with op.batch_alter_table("events") as batch:
        batch.add_column(sa.Column("release_id", Id, nullable=True))
    op.create_index("ix_events_release", "events", ["release_id"])


def downgrade():
    op.drop_index("ix_events_release", "events")
    with op.batch_alter_table("events") as batch:
        batch.drop_column("release_id")
    op.drop_table("releases")
