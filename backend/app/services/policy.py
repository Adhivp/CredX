"""Versioned institutional policy.

Escalation thresholds, budgets and scoring parameters are read from the active policy
version rather than hardcoded. The values in ``DEFAULT_POLICY`` are an illustrative
starting configuration that is seeded as version 1 when an institution has no policy;
they are not calibrated thresholds and must be reviewed by the institution.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models.governance import InstitutionPolicy

Severity = Literal["low", "moderate", "high", "severe"]
SEVERITY_ORDER = {"low": 0, "moderate": 1, "high": 2, "severe": 3}
INTERVENTIONS = (
    "continue_monitoring",
    "increase_monitoring_frequency",
    "request_additional_data",
    "relationship_contact_review",
    "restructuring_assessment",
    "credit_limit_review",
    "collections_referral_review",
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EscalationPolicy(_Strict):
    analyst_review_min_severity: Severity = "moderate"
    approval_min_severity: Severity = "high"
    material_exposure_amount: float = Field(default=1_000_000, ge=0)
    material_exposure_min_severity: Severity = "moderate"
    min_confidence_for_autonomy: float = Field(default=0.70, ge=0, le=1)
    classification_change_requires_review: bool = True
    insufficient_evidence_requires_review: bool = True
    unresolved_material_challenge_requires_review: bool = True
    approval_required_interventions: list[str] = [
        "restructuring_assessment",
        "credit_limit_review",
        "collections_referral_review",
    ]
    four_eyes: bool = True

    @model_validator(mode="after")
    def known_interventions(self):
        unknown = set(self.approval_required_interventions) - set(INTERVENTIONS)
        if unknown:
            raise ValueError(f"Unknown interventions: {sorted(unknown)}")
        return self


class BudgetPolicy(_Strict):
    max_rounds: int = Field(default=4, ge=1, le=10)
    max_delegation_depth: int = Field(default=2, ge=0, le=5)
    max_tasks: int = Field(default=30, ge=1, le=200)
    max_llm_calls: int = Field(default=24, ge=0, le=500)
    max_tokens: int = Field(default=80_000, ge=0)
    max_cost_usd: float = Field(default=1.0, ge=0)
    task_timeout_seconds: float = Field(default=30.0, gt=0, le=300)
    max_task_retries: int = Field(default=1, ge=0, le=3)
    max_wall_seconds: float = Field(default=300.0, gt=0, le=3600)
    max_challenge_cycles: int = Field(default=2, ge=0, le=5)
    max_parallel_tasks: int = Field(default=4, ge=1, le=16)


class ScoringPolicy(_Strict):
    # Noisy-OR weights per independent signal family; correlated signals are combined
    # inside a family first so they are not double counted.
    family_weights: dict[str, float] = {"repayment": 0.9, "cashflow": 0.7, "exposure": 0.6}
    elevated_family_score: float = Field(default=30, ge=0, le=100)
    severity_thresholds: dict[str, float] = {"moderate": 25, "high": 45, "severe": 65}
    sustained_min_periods: int = Field(default=3, ge=2, le=12)
    recovery_clean_periods: int = Field(default=2, ge=1, le=6)
    cashflow_decline_threshold: float = Field(default=0.10, gt=0, lt=1)

    @model_validator(mode="after")
    def ordered_thresholds(self):
        t = self.severity_thresholds
        if set(t) != {"moderate", "high", "severe"}:
            raise ValueError("severity_thresholds needs moderate, high and severe")
        if not t["moderate"] < t["high"] < t["severe"]:
            raise ValueError("severity thresholds must increase")
        if not set(self.family_weights) <= {"repayment", "cashflow", "exposure"}:
            raise ValueError("Unknown scoring family")
        if any(not 0 <= w <= 1 for w in self.family_weights.values()):
            raise ValueError("family weights must be between 0 and 1")
        return self


class DataPolicy(_Strict):
    min_repayment_records: int = Field(default=6, ge=1)
    min_cashflow_months: int = Field(default=6, ge=1)
    stale_after_days: int = Field(default=120, ge=1)
    portfolio_min_cell_size: int = Field(default=3, ge=1)


class MonitoringPolicy(_Strict):
    reopen_score_delta: float = Field(default=10, ge=0)
    reopen_on_classification_change: bool = True
    reopen_on_new_missed_payment: bool = True


class PolicyConfig(_Strict):
    name: str = "Default policy (illustrative; calibrate before institutional use)"
    escalation: EscalationPolicy = EscalationPolicy()
    budgets: BudgetPolicy = BudgetPolicy()
    scoring: ScoringPolicy = ScoringPolicy()
    data: DataPolicy = DataPolicy()
    monitoring: MonitoringPolicy = MonitoringPolicy()


DEFAULT_POLICY = PolicyConfig()


def severity_at_least(severity: str, minimum: str) -> bool:
    return SEVERITY_ORDER[severity] >= SEVERITY_ORDER[minimum]


def get_active_policy(db: Session, institution_id: str) -> tuple[InstitutionPolicy, PolicyConfig]:
    row = db.scalar(
        select(InstitutionPolicy).where(
            InstitutionPolicy.institution_id == institution_id,
            InstitutionPolicy.is_active.is_(True),
        )
    )
    if row is None:
        row = _seed_default(db, institution_id)
    return row, PolicyConfig.model_validate(json.loads(row.config))


def get_policy_version(db: Session, policy_id: str) -> PolicyConfig:
    row = db.get(InstitutionPolicy, policy_id)
    if row is None:
        raise LookupError("Unknown policy version")
    return PolicyConfig.model_validate(json.loads(row.config))


def _seed_default(db: Session, institution_id: str) -> InstitutionPolicy:
    row = InstitutionPolicy(
        institution_id=institution_id,
        version=1,
        config=DEFAULT_POLICY.model_dump_json(),
        is_active=True,
        change_reason="Seeded default policy",
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError:
        # Another request seeded it concurrently.
        db.rollback()
        row = db.scalar(
            select(InstitutionPolicy).where(
                InstitutionPolicy.institution_id == institution_id,
                InstitutionPolicy.is_active.is_(True),
            )
        )
    return row


def publish_policy(
    db: Session, institution_id: str, config: PolicyConfig, user_id: str, reason: str
) -> InstitutionPolicy:
    """Create and activate a new version; earlier versions remain for decisions that cite them."""
    current, _ = get_active_policy(db, institution_id)
    latest = db.scalar(
        select(InstitutionPolicy.version)
        .where(InstitutionPolicy.institution_id == institution_id)
        .order_by(InstitutionPolicy.version.desc())
        .limit(1)
    )
    db.execute(
        update(InstitutionPolicy).where(InstitutionPolicy.id == current.id).values(is_active=False)
    )
    row = InstitutionPolicy(
        institution_id=institution_id,
        version=(latest or 0) + 1,
        config=config.model_dump_json(),
        is_active=True,
        created_by=user_id,
        change_reason=reason,
    )
    db.add(row)
    return row
