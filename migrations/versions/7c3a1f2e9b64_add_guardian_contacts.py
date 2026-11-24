"""Add parent, guardian, and emergency contact records.

Revision ID: 7c3a1f2e9b64
Revises: 19f93532bad9
Create Date: 2026-11-21

"""
from alembic import op
import sqlalchemy as sa


revision = "7c3a1f2e9b64"
down_revision = "19f93532bad9"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "guardian",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("student_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("relationship", sa.String(length=50), nullable=False),
        sa.Column("phone", sa.String(length=30), nullable=False),
        sa.Column("email", sa.String(length=120), nullable=True),
        sa.Column("is_emergency_contact", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["student_id"], ["student.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade():
    op.drop_table("guardian")
