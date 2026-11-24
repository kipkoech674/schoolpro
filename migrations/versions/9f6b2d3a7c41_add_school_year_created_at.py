"""Add school year creation timestamps.

Revision ID: 9f6b2d3a7c41
Revises: 8d4a2c7f1e63
Create Date: 2026-11-22

"""
from alembic import op
import sqlalchemy as sa


revision = "9f6b2d3a7c41"
down_revision = "8d4a2c7f1e63"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("school_year") as batch_op:
        batch_op.add_column(
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.func.now(),
                nullable=False,
            )
        )


def downgrade():
    with op.batch_alter_table("school_year") as batch_op:
        batch_op.drop_column("created_at")
