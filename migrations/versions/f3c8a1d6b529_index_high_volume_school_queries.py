"""Add indexes for frequently filtered school records.

Revision ID: f3c8a1d6b529
Revises: e71b3c9a5d24
Create Date: 2026-11-23
"""
from alembic import op


revision = "f3c8a1d6b529"
down_revision = "e71b3c9a5d24"
branch_labels = None
depends_on = None


INDEXES = (
    ("ix_student_active_name", "student", ["active", "name", "id"]),
    (
        "ix_enrollment_class_status_student",
        "enrollment",
        ["class_id", "status", "student_id"],
    ),
    (
        "ix_enrollment_student_status_class",
        "enrollment",
        ["student_id", "status", "class_id"],
    ),
    ("ix_fee_student_id_id", "fee", ["student_id", "id"]),
    (
        "ix_attendance_class_date_student",
        "attendance",
        ["class_id", "date", "student_id"],
    ),
    ("ix_grade_class_id_id", "grade", ["class_id", "id"]),
    (
        "ix_grade_class_student_term",
        "grade",
        ["class_id", "student_id", "term"],
    ),
)


def upgrade():
    for name, table, columns in INDEXES:
        op.create_index(name, table, columns)


def downgrade():
    for name, table, _columns in reversed(INDEXES):
        op.drop_index(name, table_name=table)
