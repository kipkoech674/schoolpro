"""Add school year lifecycle and year-based classes and fees.

Revision ID: 8d4a2c7f1e63
Revises: 6c2f9a4d8e17
Create Date: 2026-11-22

"""
from alembic import op
import sqlalchemy as sa


revision = "8d4a2c7f1e63"
down_revision = "6c2f9a4d8e17"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "school_year",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=20), nullable=False),
        sa.Column(
            "archived",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.execute(
        sa.text(
            "INSERT INTO school_year (name, archived) "
            "SELECT DISTINCT academic_year, false FROM school_class"
        )
    )
    op.drop_constraint("school_class_name_key", "school_class", type_="unique")
    op.create_unique_constraint(
        "unique_class_name_year",
        "school_class",
        ["name", "academic_year"],
    )
    op.create_foreign_key(
        "fk_school_class_academic_year_school_year",
        "school_class",
        "school_year",
        ["academic_year"],
        ["name"],
    )
    with op.batch_alter_table("fee") as batch_op:
        batch_op.add_column(sa.Column("academic_year", sa.String(length=20)))
        batch_op.create_foreign_key(
            "fk_fee_academic_year_school_year",
            "school_year",
            ["academic_year"],
            ["name"],
        )


def downgrade():
    with op.batch_alter_table("fee") as batch_op:
        batch_op.drop_constraint("fk_fee_academic_year_school_year", type_="foreignkey")
        batch_op.drop_column("academic_year")
    op.drop_constraint(
        "fk_school_class_academic_year_school_year",
        "school_class",
        type_="foreignkey",
    )
    op.drop_constraint("unique_class_name_year", "school_class", type_="unique")
    op.create_unique_constraint("school_class_name_key", "school_class", ["name"])
    op.drop_table("school_year")
