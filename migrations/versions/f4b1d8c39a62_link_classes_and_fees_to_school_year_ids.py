"""Link classes and fees to school years by stable ID.

Revision ID: f4b1d8c39a62
Revises: c8f2a7e91d54
Create Date: 2026-11-22

"""
from alembic import op
import sqlalchemy as sa


revision = "f4b1d8c39a62"
down_revision = "c8f2a7e91d54"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("school_class") as batch_op:
        batch_op.add_column(sa.Column("school_year_id", sa.Integer(), nullable=True))
    with op.batch_alter_table("fee") as batch_op:
        batch_op.add_column(sa.Column("school_year_id", sa.Integer(), nullable=True))

    op.execute(
        sa.text(
            "UPDATE school_class SET school_year_id = "
            "(SELECT school_year.id FROM school_year "
            "WHERE school_year.name = school_class.academic_year)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE fee SET school_year_id = "
            "(SELECT school_year.id FROM school_year "
            "WHERE school_year.name = fee.academic_year) "
            "WHERE fee.academic_year IS NOT NULL"
        )
    )

    with op.batch_alter_table("school_class") as batch_op:
        batch_op.drop_constraint(
            "fk_school_class_academic_year_school_year",
            type_="foreignkey",
        )
        batch_op.drop_constraint("unique_class_name_year", type_="unique")
        batch_op.alter_column("school_year_id", nullable=False)
        batch_op.create_foreign_key(
            "fk_school_class_school_year_id_school_year",
            "school_year",
            ["school_year_id"],
            ["id"],
        )
        batch_op.create_unique_constraint(
            "unique_class_name_year",
            ["name", "school_year_id"],
        )
        batch_op.create_index("ix_school_class_school_year_id", ["school_year_id"])

    with op.batch_alter_table("fee") as batch_op:
        batch_op.drop_constraint(
            "fk_fee_academic_year_school_year",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_fee_school_year_id_school_year",
            "school_year",
            ["school_year_id"],
            ["id"],
        )
        batch_op.create_index("ix_fee_school_year_id", ["school_year_id"])


def downgrade():
    with op.batch_alter_table("fee") as batch_op:
        batch_op.drop_index("ix_fee_school_year_id")
        batch_op.drop_constraint(
            "fk_fee_school_year_id_school_year",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_fee_academic_year_school_year",
            "school_year",
            ["academic_year"],
            ["name"],
        )
        batch_op.drop_column("school_year_id")

    with op.batch_alter_table("school_class") as batch_op:
        batch_op.drop_index("ix_school_class_school_year_id")
        batch_op.drop_constraint(
            "unique_class_name_year",
            type_="unique",
        )
        batch_op.drop_constraint(
            "fk_school_class_school_year_id_school_year",
            type_="foreignkey",
        )
        batch_op.create_foreign_key(
            "fk_school_class_academic_year_school_year",
            "school_year",
            ["academic_year"],
            ["name"],
        )
        batch_op.drop_column("school_year_id")
        batch_op.create_unique_constraint(
            "unique_class_name_year",
            ["name", "academic_year"],
        )
