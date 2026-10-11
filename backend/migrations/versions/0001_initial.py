"""Normalized inventory schema.

Revision ID: 0001
Revises:
Create Date: 2026-10-11

The legacy key/value table `records` (if any) is left untouched: agents resend sources within
a minute, while services and history from it are not copied.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

Json = sa.JSON().with_variant(JSONB(), "postgresql")
Id = sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def time(name, nullable=False):
    return sa.Column(name, sa.DateTime(timezone=True), nullable=nullable)


def text(name, primary_key=False):
    return sa.Column(name, sa.String(), nullable=False, primary_key=primary_key)


def upgrade():
    op.create_table(
        "services",
        text("id", primary_key=True), text("name"), text("description"), text("owner"), text("team"),
        text("repository"), text("grafana"), text("splunk"), text("bamboo"),
        sa.Column("extra", Json, nullable=False), time("created_at"), time("updated_at"))
    op.create_table(
        "sources",
        text("source_id", primary_key=True), text("source_type"), text("hostname"),
        time("observed_at"), time("received_at"), sa.Column("snapshot", Json, nullable=False))
    op.create_table(
        "config_snapshots",
        sa.Column("id", Id, sa.Identity(), primary_key=True),
        text("source_id"), text("instance_id"), text("service_id"),
        sa.Column("config_hash", sa.String(64), nullable=False), sa.Column("config", Json, nullable=False),
        time("first_seen_at"), time("last_seen_at"))
    op.create_index("ix_config_snapshots_instance", "config_snapshots", ["source_id", "instance_id", "first_seen_at"])
    op.create_index("ix_config_snapshots_service", "config_snapshots", ["service_id", "first_seen_at"])
    op.create_index("ix_config_snapshots_last_seen", "config_snapshots", ["last_seen_at"])
    op.create_table(
        "instances",
        sa.Column("source_id", sa.String(), sa.ForeignKey("sources.source_id", ondelete="CASCADE"), primary_key=True),
        text("instance_id", primary_key=True),
        sa.Column("position", sa.Integer(), nullable=False),
        text("service_id"), text("environment"), text("runtime"), text("status"),
        sa.Column("config_hash", sa.String(64), nullable=False),
        sa.Column("config_snapshot_id", Id, sa.ForeignKey("config_snapshots.id", ondelete="SET NULL"), nullable=True),
        sa.Column("data", Json, nullable=False))
    op.create_index("ix_instances_service_id", "instances", ["service_id"])
    op.create_table(
        "events",
        sa.Column("id", Id, sa.Identity(), primary_key=True),
        time("received_at"), time("occurred_at", nullable=True),
        text("type"), text("service_id"), text("source_id"), text("instance_id"),
        sa.Column("data", Json, nullable=False))
    op.create_index("ix_events_received", "events", [sa.text("received_at DESC"), sa.text("id DESC")])
    op.create_index("ix_events_service", "events", ["service_id", sa.text("received_at DESC"), sa.text("id DESC")])
    op.create_table(
        "deployments",
        text("event_id", primary_key=True), sa.Column("request", Json, nullable=False), time("received_at"))


def downgrade():
    for table in ("deployments", "events", "instances", "config_snapshots", "sources", "services"):
        op.drop_table(table)
