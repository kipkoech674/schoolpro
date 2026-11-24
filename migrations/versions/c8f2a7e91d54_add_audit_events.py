"""Add an audit trail for sensitive application actions.

Revision ID: c8f2a7e91d54
Revises: 9f6b2d3a7c41
Create Date: 2026-11-22

"""
from alembic import op
import sqlalchemy as sa


revision = "c8f2a7e91d54"
down_revision = "9f6b2d3a7c41"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "audit_event",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "actor_id",
            sa.Integer(),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("entity_type", sa.String(length=80), nullable=False),
        sa.Column("entity_id", sa.Integer(), nullable=True),
        sa.Column("summary", sa.String(length=255), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_audit_event_created_at", "audit_event", ["created_at"])


def downgrade():
    op.drop_index("ix_audit_event_created_at", table_name="audit_event")
    op.drop_table("audit_event")
