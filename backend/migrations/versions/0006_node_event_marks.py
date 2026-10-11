"""Node and reason of Kubernetes node Events in their dedupe marks.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-11

The latest time per node and reason feeds gman_k8s_node_event_last_timestamp_seconds (/metrics) and the node
card. Marks of pod Events keep an empty node; existing marks are pod Events.
"""
from alembic import op
import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("runtime_event_marks") as table:
        table.add_column(sa.Column("node", sa.String(), nullable=False, server_default=""))
        table.add_column(sa.Column("reason", sa.String(), nullable=False, server_default=""))
    op.create_index("ix_runtime_event_marks_node", "runtime_event_marks", ["source_id", "node", "reason"])


def downgrade():
    op.drop_index("ix_runtime_event_marks_node", "runtime_event_marks")
    with op.batch_alter_table("runtime_event_marks") as table:
        table.drop_column("reason")
        table.drop_column("node")
