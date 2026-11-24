"""Replace fee balances with dated payment records.

Revision ID: 5a7c3e9d1b42
Revises: 4d8b2f6a1c93
Create Date: 2026-11-22

"""
from alembic import op
import sqlalchemy as sa


revision = "5a7c3e9d1b42"
down_revision = "4d8b2f6a1c93"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "payment",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("fee_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("payment_method", sa.String(length=40), nullable=True),
        sa.Column("reference", sa.String(length=120), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.CheckConstraint("amount > 0", name="positive_payment_amount"),
        sa.ForeignKeyConstraint(["fee_id"], ["fee.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_payment_fee_id", "payment", ["fee_id"])
    op.execute(
        sa.text(
            "INSERT INTO payment "
            "(fee_id, amount, payment_date, notes) "
            "SELECT id, paid_amount, NULL, "
            "'Opening balance imported from legacy fee balance' "
            "FROM fee WHERE paid_amount > 0"
        )
    )
    with op.batch_alter_table("fee") as batch_op:
        batch_op.drop_column("paid_amount")


def downgrade():
    with op.batch_alter_table("fee") as batch_op:
        batch_op.add_column(
            sa.Column(
                "paid_amount",
                sa.Numeric(12, 2),
                server_default="0",
                nullable=False,
            )
        )
    op.execute(
        sa.text(
            "UPDATE fee SET paid_amount = "
            "(SELECT COALESCE(SUM(payment.amount), 0) "
            "FROM payment WHERE payment.fee_id = fee.id)"
        )
    )
    op.drop_index("ix_payment_fee_id", table_name="payment")
    op.drop_table("payment")
