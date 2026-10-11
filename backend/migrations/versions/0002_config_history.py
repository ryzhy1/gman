"""Instance name and environment on config versions.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-11

Config history outlives the instance (a removed container keeps its versions until retention),
so the label shown in the version list is stored with the version. Rows written before this
migration get empty values; the portal falls back to the instance ID.
"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("config_snapshots") as batch:
        batch.add_column(sa.Column("instance_name", sa.String(), nullable=False, server_default=""))
        batch.add_column(sa.Column("environment", sa.String(), nullable=False, server_default=""))


def downgrade():
    with op.batch_alter_table("config_snapshots") as batch:
        batch.drop_column("environment")
        batch.drop_column("instance_name")
