"""SSO sessions.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-11

Service owners (`owners`) need no migration: new catalog fields are kept in services.extra.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

Json = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade():
    op.create_table(
        "sessions",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("subject", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("data", Json, nullable=False),
        sa.Column("csrf_token", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])


def downgrade():
    op.drop_table("sessions")
