"""Add one-time staff invitation state.

Revision ID: a81d6f3c9b20
Revises: f3c8a1d6b529
Create Date: 2026-11-24

"""
from alembic import op
import sqlalchemy as sa


revision = "a81d6f3c9b20"
down_revision = "f3c8a1d6b529"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(sa.Column("display_name", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("invitation_token_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("invitation_sent_at", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_column("invitation_sent_at")
        batch_op.drop_column("invitation_token_hash")
        batch_op.drop_column("display_name")
