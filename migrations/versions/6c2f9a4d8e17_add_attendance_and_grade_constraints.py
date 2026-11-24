"""Constrain attendance statuses and grade scores.

Revision ID: 6c2f9a4d8e17
Revises: 5a7c3e9d1b42
Create Date: 2026-11-22

"""
from alembic import op
import sqlalchemy as sa


revision = "6c2f9a4d8e17"
down_revision = "5a7c3e9d1b42"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("attendance") as batch_op:
        batch_op.create_check_constraint(
            "valid_attendance_status",
            "status IN ('Present', 'Absent', 'Late', 'Excused')",
        )
    with op.batch_alter_table("grade") as batch_op:
        batch_op.create_check_constraint(
            "valid_grade_score",
            "score >= 0 AND score <= 100",
        )


def downgrade():
    with op.batch_alter_table("grade") as batch_op:
        batch_op.drop_constraint("valid_grade_score", type_="check")
    with op.batch_alter_table("attendance") as batch_op:
        batch_op.drop_constraint("valid_attendance_status", type_="check")
