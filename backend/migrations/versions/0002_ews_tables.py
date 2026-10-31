"""Bring the EWS tables, previously created only by create_all at startup, under Alembic.

Databases bootstrapped by the old startup ``create_all`` already contain these tables;
each table is created only when absent so such databases upgrade cleanly.

Revision ID: 0002
Revises: 0001
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def _tables(bind) -> set[str]:
    return set(sa.inspect(bind).get_table_names())


def upgrade() -> None:
    existing = _tables(op.get_bind())

    if "customers" not in existing:
        op.create_table(
            "customers",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("customer_id", sa.String(64), nullable=False),
            sa.Column("customer_type", sa.String(32), nullable=False),
            sa.Column("employment_category", sa.String(64), nullable=False),
            sa.Column("portfolio_segment", sa.String(64), nullable=False),
            sa.Column("created_at", sa.Integer(), nullable=False),
        )
        op.create_index("ix_customers_customer_id", "customers", ["customer_id"], unique=True)

    if "loan_facilities" not in existing:
        op.create_table(
            "loan_facilities",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
            sa.Column("loan_id", sa.String(64), nullable=False),
            sa.Column("product_type", sa.String(64), nullable=False),
            sa.Column("sanctioned_amount", sa.Float(), nullable=False),
            sa.Column("outstanding_balance", sa.Float(), nullable=False),
            sa.Column("tenure_months", sa.Integer(), nullable=False),
            sa.Column("instalment_amount", sa.Float(), nullable=False),
            sa.Column("disbursement_date", sa.String(32), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
        )
        op.create_index("ix_loan_customer", "loan_facilities", ["customer_id"])
        op.create_index("ix_loan_facilities_loan_id", "loan_facilities", ["loan_id"], unique=True)

    if "repayment_records" not in existing:
        op.create_table(
            "repayment_records",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("loan_id", sa.String(36), sa.ForeignKey("loan_facilities.id"), nullable=False),
            sa.Column("due_date", sa.String(32), nullable=False),
            sa.Column("payment_date", sa.String(32), nullable=True),
            sa.Column("amount_due", sa.Float(), nullable=False),
            sa.Column("amount_paid", sa.Float(), nullable=False),
            sa.Column("dpd", sa.Integer(), nullable=False),
            sa.Column("repayment_status", sa.String(32), nullable=False),
        )
        op.create_index("ix_repayment_loan", "repayment_records", ["loan_id"])

    if "customer_exposures" not in existing:
        op.create_table(
            "customer_exposures",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
            sa.Column("facility_id", sa.String(64), nullable=False),
            sa.Column("facility_type", sa.String(64), nullable=False),
            sa.Column("sanctioned_amount", sa.Float(), nullable=False),
            sa.Column("credit_limit", sa.Float(), nullable=False),
            sa.Column("current_utilisation", sa.Float(), nullable=False),
            sa.Column("outstanding_balance", sa.Float(), nullable=False),
            sa.Column("instalment_amount", sa.Float(), nullable=False),
            sa.Column("disbursement_date", sa.String(32), nullable=False),
            sa.Column("snapshot_date", sa.String(32), nullable=False),
        )
        op.create_index("ix_exposure_customer", "customer_exposures", ["customer_id"])

    if "cashflow_records" not in existing:
        op.create_table(
            "cashflow_records",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
            sa.Column("month", sa.Integer(), nullable=False),
            sa.Column("monthly_inflow", sa.Float(), nullable=False),
            sa.Column("monthly_outflow", sa.Float(), nullable=True),
            sa.Column("record_date", sa.String(32), nullable=False),
        )
        op.create_index("ix_cashflow_customer", "cashflow_records", ["customer_id"])

    if "warning_assessments" not in existing:
        op.create_table(
            "warning_assessments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
            sa.Column("warning_score", sa.Float(), nullable=False),
            sa.Column("risk_category", sa.String(32), nullable=False),
            sa.Column("rule_delay_score", sa.Float(), nullable=False),
            sa.Column("rule_miss_score", sa.Float(), nullable=False),
            sa.Column("rule_exposure_score", sa.Float(), nullable=False),
            sa.Column("rule_completeness_score", sa.Float(), nullable=False),
            sa.Column("cashflow_score", sa.Float(), nullable=False),
            sa.Column("top_indicators", sa.Text(), nullable=False),
            sa.Column("assessed_at", sa.Integer(), nullable=False),
        )
        op.create_index("ix_warning_customer", "warning_assessments", ["customer_id"])

    if "alert_classifications" not in existing:
        op.create_table(
            "alert_classifications",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column(
                "assessment_id",
                sa.String(36),
                sa.ForeignKey("warning_assessments.id"),
                nullable=False,
            ),
            sa.Column("decision", sa.String(64), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("analyst_id", sa.String(64), nullable=True),
            sa.Column("original_score", sa.Float(), nullable=False),
            sa.Column("original_category", sa.String(32), nullable=False),
            sa.Column("preserved_evidence", sa.Text(), nullable=False),
            sa.Column("created_at", sa.Integer(), nullable=False),
        )
        op.create_index("ix_alert_assessment", "alert_classifications", ["assessment_id"])

    if "data_uploads" not in existing:
        op.create_table(
            "data_uploads",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("filename", sa.String(255), nullable=False),
            sa.Column("file_type", sa.String(32), nullable=False),
            sa.Column("record_count", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("errors", sa.Text(), nullable=True),
            sa.Column("uploaded_at", sa.Integer(), nullable=False),
        )


def downgrade() -> None:
    for table in (
        "data_uploads",
        "alert_classifications",
        "warning_assessments",
        "cashflow_records",
        "customer_exposures",
        "repayment_records",
        "loan_facilities",
        "customers",
    ):
        op.drop_table(table)
