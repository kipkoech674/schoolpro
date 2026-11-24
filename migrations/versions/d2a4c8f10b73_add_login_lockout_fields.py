"""Add persistent login lockout state.

Revision ID: d2a4c8f10b73
Revises: f4b1d8c39a62
Create Date: 2026-11-22

"""
from alembic import op
import sqlalchemy as sa


revision = "d2a4c8f10b73"
down_revision = "f4b1d8c39a62"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(
            sa.Column(
                "failed_login_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(sa.Column("locked_until", sa.DateTime(), nullable=True))


def downgrade():
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_column("locked_until")
        batch_op.drop_column("failed_login_attempts")
