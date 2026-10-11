"""Dedupe marks for Kubernetes Events recorded in history.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11

The events themselves go to `events` (type runtime-event, history-gap); Instance.last_oom_at lives in
instances.data and needs no migration.
"""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "runtime_event_marks",
        sa.Column("source_id", sa.String(), primary_key=True),
        sa.Column("uid", sa.String(), primary_key=True),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("last_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_runtime_event_marks_received", "runtime_event_marks", ["received_at"])


def downgrade():
    op.drop_table("runtime_event_marks")
