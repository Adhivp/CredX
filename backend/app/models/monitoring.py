"""Level 2 tables: risk history, analyst event classifications, investigation cases,
interventions and their audit trail.

All are new tables (Base.metadata.create_all adds them to an existing database);
no Level 1 table is altered.
"""

import time
import uuid

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> int:
    return int(time.time())


class RiskSnapshot(Base):
    """Point-in-time assessment of one customer at one month end.

    Built by replaying the Level 1 rules and models on the data that was known at
    that date (app/services/risk_history.py), so the history shows how the warning
    score and its indicators changed over time.
    """

    __tablename__ = "risk_snapshots"
    __table_args__ = (
        UniqueConstraint("customer_id", "period", name="uq_snapshot_customer_period"),
        Index("ix_snapshot_period", "period"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), index=True)
    period: Mapped[str] = mapped_column(String(7))  # YYYY-MM
    period_end: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    warning_score: Mapped[float] = mapped_column(Float, default=0.0)
    risk_category: Mapped[str] = mapped_column(String(32), default="Low")
    default_probability: Mapped[float | None] = mapped_column(Float, nullable=True)
    rule_delay_score: Mapped[float] = mapped_column(Float, default=0.0)
    rule_miss_score: Mapped[float] = mapped_column(Float, default=0.0)
    rule_exposure_score: Mapped[float] = mapped_column(Float, default=0.0)
    rule_completeness_score: Mapped[float] = mapped_column(Float, default=0.0)
    cashflow_score: Mapped[float] = mapped_column(Float, default=0.0)
    metrics: Mapped[str] = mapped_column(Text, default="{}")  # JSON: raw indicators at period end
    model_contributors: Mapped[str] = mapped_column(Text, default="[]")  # JSON: SHAP top features
    indicators: Mapped[str] = mapped_column(Text, default="[]")
    data_notes: Mapped[str] = mapped_column(Text, default="[]")  # JSON: reconstruction caveats
    computed_at: Mapped[int] = mapped_column(default=_now)


class EventClassification(Base):
    """Analyst classification of one repayment event (e.g. a miss caused by a
    processing failure). The raw repayment record is never modified: scoring applies
    the classification on top of it, and deleting the row restores the original."""

    __tablename__ = "event_classifications"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    repayment_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("repayment_records.id"), unique=True, index=True,
    )
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"), index=True)
    classification: Mapped[str] = mapped_column(String(32))
    excluded_from_scoring: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(128), default="")
    original_values: Mapped[str] = mapped_column(Text, default="{}")
    score_before: Mapped[float | None] = mapped_column(Float, nullable=True)
    score_after: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[int] = mapped_column(default=_now)


class InvestigationCase(Base):
    __tablename__ = "investigation_cases"
    __table_args__ = (Index("ix_case_customer", "customer_id"), Index("ix_case_status", "status"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    reference: Mapped[str] = mapped_column(String(32), unique=True)
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"))
    status: Mapped[str] = mapped_column(String(32), default="open")
    disposition: Mapped[str] = mapped_column(String(32), default="pending")  # pending|accepted|false_positive
    owner: Mapped[str] = mapped_column(String(128), default="")
    owner_role: Mapped[str] = mapped_column(String(32), default="")
    priority_tier: Mapped[str] = mapped_column(String(8), default="")
    score_at_open: Mapped[float] = mapped_column(Float, default=0.0)
    category_at_open: Mapped[str] = mapped_column(String(32), default="")
    corrected_category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    correction_reason: Mapped[str] = mapped_column(Text, default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[int] = mapped_column(default=_now)
    updated_at: Mapped[int] = mapped_column(default=_now)
    closed_at: Mapped[int | None] = mapped_column(nullable=True)


class Intervention(Base):
    __tablename__ = "interventions"
    __table_args__ = (Index("ix_intervention_customer", "customer_id"), Index("ix_intervention_status", "status"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    reference: Mapped[str] = mapped_column(String(32), unique=True)
    case_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("investigation_cases.id"), nullable=True)
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"))
    facility_ref: Mapped[str] = mapped_column(String(64), default="")
    warning_category: Mapped[str] = mapped_column(String(32), default="")
    scenario: Mapped[str] = mapped_column(String(48), default="")
    contributing_indicators: Mapped[str] = mapped_column(Text, default="[]")
    recommended_action: Mapped[str] = mapped_column(Text, default="")
    action_code: Mapped[str] = mapped_column(String(48), default="")
    customer_impacting: Mapped[int] = mapped_column(Integer, default=0)
    requires_approval: Mapped[int] = mapped_column(Integer, default=0)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[int | None] = mapped_column(nullable=True)
    owner: Mapped[str] = mapped_column(String(128), default="")
    owner_role: Mapped[str] = mapped_column(String(32), default="")
    priority: Mapped[str] = mapped_column(String(8), default="P3")
    target_date: Mapped[str] = mapped_column(String(10), default="")
    status: Mapped[str] = mapped_column(String(32), default="proposed")
    notes: Mapped[str] = mapped_column(Text, default="")
    decision: Mapped[str] = mapped_column(Text, default="")
    evidence: Mapped[str] = mapped_column(Text, default="[]")
    outcome: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[int] = mapped_column(default=_now)
    updated_at: Mapped[int] = mapped_column(default=_now)
    completed_at: Mapped[int | None] = mapped_column(nullable=True)


class CaseActivity(Base):
    """Append-only audit trail for cases and interventions: notes, information
    requests, status changes, assignments, classification corrections, recalculations."""

    __tablename__ = "case_activities"
    __table_args__ = (Index("ix_activity_case", "case_id"), Index("ix_activity_customer", "customer_id"))

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(String(36), ForeignKey("customers.id"))
    case_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("investigation_cases.id"), nullable=True)
    intervention_id: Mapped[str | None] = mapped_column(String(36), ForeignKey("interventions.id"), nullable=True)
    kind: Mapped[str] = mapped_column(String(32))
    body: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(128), default="")
    meta: Mapped[str] = mapped_column(Text, default="{}")
    status: Mapped[str | None] = mapped_column(String(32), nullable=True)  # info requests: open|fulfilled
    created_at: Mapped[int] = mapped_column(default=_now)
