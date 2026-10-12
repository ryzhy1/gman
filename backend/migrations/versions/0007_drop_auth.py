"""Remove sign-in: SSO sessions and service owners.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-12

The portal and API no longer authenticate anyone, so the sessions table and the `owners` catalog field
(kept in services.extra) that granted edit rights are dropped.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

Json = sa.JSON().with_variant(JSONB(), "postgresql")


def upgrade():
    op.drop_table("sessions")
    services = sa.table("services", sa.column("id", sa.String()), sa.column("extra", Json))
    connection = op.get_bind()
    for row in connection.execute(sa.select(services.c.id, services.c.extra)).all():
        if row.extra and "owners" in row.extra:
            extra = {k: v for k, v in row.extra.items() if k != "owners"}
            connection.execute(services.update().where(services.c.id == row.id).values(extra=extra))


def downgrade():
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
