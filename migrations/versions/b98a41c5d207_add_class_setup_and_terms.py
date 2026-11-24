"""Add class setup, timetable, terms, student photos, and staff email.

Revision ID: b98a41c5d207
Revises: f4b1d8c39a62
Create Date: 2026-11-22
"""
from alembic import op
import sqlalchemy as sa


revision = "b98a41c5d207"
down_revision = "d2a4c8f10b73"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(sa.Column("email", sa.String(length=254), nullable=True))
        batch_op.add_column(sa.Column("reset_token_hash", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("reset_requested_at", sa.DateTime(), nullable=True))
        batch_op.create_unique_constraint("uq_user_email", ["email"])
    with op.batch_alter_table("student") as batch_op:
        batch_op.add_column(sa.Column("photo_filename", sa.String(length=80), nullable=True))
    with op.batch_alter_table("school_class") as batch_op:
        batch_op.add_column(sa.Column("capacity", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("class_teacher_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_school_class_class_teacher_id_user", "user", ["class_teacher_id"], ["id"])

    op.create_table(
        "school_term",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("school_year_id", sa.Integer(), sa.ForeignKey("school_year.id"), nullable=False),
        sa.Column("name", sa.String(length=40), nullable=False),
        sa.Column("start_date", sa.Date(), nullable=False),
        sa.Column("end_date", sa.Date(), nullable=False),
        sa.UniqueConstraint("school_year_id", "name", name="unique_term_name_year"),
        sa.CheckConstraint("start_date <= end_date", name="valid_term_date_range"),
    )
    op.create_table(
        "class_subject",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("class_id", sa.Integer(), sa.ForeignKey("school_class.id"), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("teacher_id", sa.Integer(), sa.ForeignKey("user.id"), nullable=True),
        sa.UniqueConstraint("class_id", "name", name="unique_subject_per_class"),
    )
    op.create_table(
        "timetable_slot",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("class_id", sa.Integer(), sa.ForeignKey("school_class.id"), nullable=False),
        sa.Column("subject_id", sa.Integer(), sa.ForeignKey("class_subject.id"), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("start_time", sa.Time(), nullable=False),
        sa.Column("end_time", sa.Time(), nullable=False),
        sa.Column("room", sa.String(length=30), nullable=True),
        sa.CheckConstraint("weekday >= 0 AND weekday <= 6", name="valid_timetable_weekday"),
        sa.CheckConstraint("start_time < end_time", name="valid_timetable_time_range"),
    )
    with op.batch_alter_table("grade") as batch_op:
        batch_op.add_column(sa.Column("term_id", sa.Integer(), nullable=True))
        batch_op.create_foreign_key("fk_grade_term_id_school_term", "school_term", ["term_id"], ["id"])


def downgrade():
    with op.batch_alter_table("grade") as batch_op:
        batch_op.drop_constraint("fk_grade_term_id_school_term", type_="foreignkey")
        batch_op.drop_column("term_id")
    op.drop_table("timetable_slot")
    op.drop_table("class_subject")
    op.drop_table("school_term")
    with op.batch_alter_table("school_class") as batch_op:
        batch_op.drop_constraint("fk_school_class_class_teacher_id_user", type_="foreignkey")
        batch_op.drop_column("class_teacher_id")
        batch_op.drop_column("capacity")
    with op.batch_alter_table("student") as batch_op:
        batch_op.drop_column("photo_filename")
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_constraint("uq_user_email", type_="unique")
        batch_op.drop_column("reset_requested_at")
        batch_op.drop_column("reset_token_hash")
        batch_op.drop_column("email")
