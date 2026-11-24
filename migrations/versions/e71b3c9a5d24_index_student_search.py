"""Index normalized student fields used by the typeahead search.

Revision ID: e71b3c9a5d24
Revises: b98a41c5d207
Create Date: 2026-11-23
"""
from alembic import op
import sqlalchemy as sa


revision = "e71b3c9a5d24"
down_revision = "b98a41c5d207"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_student_lower_name",
        "student",
        [sa.text("lower(name)")],
    )
    op.create_index(
        "ix_student_lower_number",
        "student",
        [sa.text("lower(student_number)")],
    )


def downgrade():
    op.drop_index("ix_student_lower_number", table_name="student")
    op.drop_index("ix_student_lower_name", table_name="student")
