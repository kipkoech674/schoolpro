"""Assign teachers to classes.

Revision ID: 2a6d9c4e81bf
Revises: 7c3a1f2e9b64
Create Date: 2026-11-22

"""
from alembic import op
import sqlalchemy as sa


revision = "2a6d9c4e81bf"
down_revision = "7c3a1f2e9b64"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "teacher_class_assignment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("teacher_id", sa.Integer(), nullable=False),
        sa.Column("class_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["class_id"], ["school_class.id"]),
        sa.ForeignKeyConstraint(["teacher_id"], ["user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "teacher_id",
            "class_id",
            name="unique_teacher_class",
        ),
    )


def downgrade():
    op.drop_table("teacher_class_assignment")
