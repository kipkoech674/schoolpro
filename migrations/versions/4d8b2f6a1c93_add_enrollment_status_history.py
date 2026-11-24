"""Allow enrollment history while enforcing one active enrollment per class.

Revision ID: 4d8b2f6a1c93
Revises: 2a6d9c4e81bf
Create Date: 2026-11-22

"""
from alembic import op
import sqlalchemy as sa


revision = "4d8b2f6a1c93"
down_revision = "2a6d9c4e81bf"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("enrollment") as batch_op:
        batch_op.drop_constraint("unique_enrollment", type_="unique")
        batch_op.create_check_constraint(
            "valid_enrollment_status",
            "status IN ('active', 'completed', 'withdrawn')",
        )

    op.create_index(
        "unique_active_enrollment",
        "enrollment",
        ["student_id", "class_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
        sqlite_where=sa.text("status = 'active'"),
    )


def downgrade():
    op.drop_index("unique_active_enrollment", table_name="enrollment")
    with op.batch_alter_table("enrollment") as batch_op:
        batch_op.drop_constraint("valid_enrollment_status", type_="check")
        batch_op.create_unique_constraint(
            "unique_enrollment",
            ["student_id", "class_id"],
        )
