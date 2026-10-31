import time
import uuid

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> int:
    return int(time.time())


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    customer_type: Mapped[str] = mapped_column(String(32), default="individual")
    employment_category: Mapped[str] = mapped_column(String(64), default="")
    portfolio_segment: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[int] = mapped_column(default=_now)


class LoanFacility(Base):
    __tablename__ = "loan_facilities"
    __table_args__ = (Index("ix_loan_customer", "customer_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"))
    loan_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    product_type: Mapped[str] = mapped_column(String(64), default="")
    sanctioned_amount: Mapped[float] = mapped_column(Float, default=0.0)
    outstanding_balance: Mapped[float] = mapped_column(Float, default=0.0)
    tenure_months: Mapped[int] = mapped_column(Integer, default=0)
    instalment_amount: Mapped[float] = mapped_column(Float, default=0.0)
    disbursement_date: Mapped[str] = mapped_column(String(32), default="")
    status: Mapped[str] = mapped_column(String(32), default="active")


class RepaymentRecord(Base):
    __tablename__ = "repayment_records"
    __table_args__ = (Index("ix_repayment_loan", "loan_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    loan_id: Mapped[str] = mapped_column(String(36), ForeignKey("loan_facilities.id"))
    due_date: Mapped[str] = mapped_column(String(32))
    payment_date: Mapped[str | None] = mapped_column(String(32), nullable=True)
    amount_due: Mapped[float] = mapped_column(Float, default=0.0)
    amount_paid: Mapped[float] = mapped_column(Float, default=0.0)
    dpd: Mapped[int] = mapped_column(Integer, default=0)
    repayment_status: Mapped[str] = mapped_column(String(32), default="on_time")


class CustomerExposure(Base):
    __tablename__ = "customer_exposures"
    __table_args__ = (Index("ix_exposure_customer", "customer_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"))
    facility_id: Mapped[str] = mapped_column(String(64), default="")
    facility_type: Mapped[str] = mapped_column(String(64), default="")
    sanctioned_amount: Mapped[float] = mapped_column(Float, default=0.0)
    credit_limit: Mapped[float] = mapped_column(Float, default=0.0)
    current_utilisation: Mapped[float] = mapped_column(Float, default=0.0)
    outstanding_balance: Mapped[float] = mapped_column(Float, default=0.0)
    instalment_amount: Mapped[float] = mapped_column(Float, default=0.0)
    disbursement_date: Mapped[str] = mapped_column(String(32), default="")
    snapshot_date: Mapped[str] = mapped_column(String(32), default="")


class CashFlowRecord(Base):
    __tablename__ = "cashflow_records"
    __table_args__ = (Index("ix_cashflow_customer", "customer_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"))
    month: Mapped[int] = mapped_column(Integer)
    monthly_inflow: Mapped[float] = mapped_column(Float, default=0.0)
    monthly_outflow: Mapped[float | None] = mapped_column(Float, nullable=True)
    record_date: Mapped[str] = mapped_column(String(32), default="")


class WarningAssessment(Base):
    __tablename__ = "warning_assessments"
    __table_args__ = (Index("ix_warning_customer", "customer_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"))
    warning_score: Mapped[float] = mapped_column(Float, default=0.0)
    risk_category: Mapped[str] = mapped_column(String(32), default="Low")
    rule_delay_score: Mapped[float] = mapped_column(Float, default=0.0)
    rule_miss_score: Mapped[float] = mapped_column(Float, default=0.0)
    rule_exposure_score: Mapped[float] = mapped_column(Float, default=0.0)
    rule_completeness_score: Mapped[float] = mapped_column(Float, default=0.0)
    cashflow_score: Mapped[float] = mapped_column(Float, default=0.0)
    top_indicators: Mapped[str] = mapped_column(Text, default="[]")
    assessed_at: Mapped[int] = mapped_column(default=_now)


class AlertClassification(Base):
    __tablename__ = "alert_classifications"
    __table_args__ = (Index("ix_alert_assessment", "assessment_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    assessment_id: Mapped[str] = mapped_column(String(36), ForeignKey("warning_assessments.id"))
    decision: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(Text, default="")
    analyst_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_score: Mapped[float] = mapped_column(Float, default=0.0)
    original_category: Mapped[str] = mapped_column(String(32), default="")
    preserved_evidence: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[int] = mapped_column(default=_now)


class DataUpload(Base):
    __tablename__ = "data_uploads"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    filename: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(32))
    record_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    errors: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploaded_at: Mapped[int] = mapped_column(default=_now)
