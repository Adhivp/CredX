"""Request bodies for investigation, review and governance endpoints."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Specialist = Literal[
    "repayment_specialist",
    "cashflow_specialist",
    "exposure_specialist",
    "baseline_specialist",
    "integrity_specialist",
    "portfolio_specialist",
]
ContextCategory = Literal[
    "temporary_disruption",
    "seasonal_business",
    "income_change",
    "dispute_or_error",
    "new_obligation",
    "other",
]
Classification = Literal[
    "low_risk",
    "emerging_deterioration",
    "sustained_deterioration",
    "accelerating_deterioration",
    "recovery",
    "insufficient_evidence",
]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InitiateInvestigation(_Body):
    customer_id: str | None = Field(default=None, min_length=1, max_length=64)
    portfolio_segment: str | None = Field(default=None, max_length=64)
    scope: Literal["customer", "portfolio"] = "customer"
    question: str = Field(default="", max_length=1000)
    context: str | None = Field(default=None, max_length=4000)
    context_category: ContextCategory | None = None
    requested_specialists: list[Specialist] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def scope_target(self):
        if self.scope == "customer" and not self.customer_id:
            raise ValueError("customer_id is required for a customer investigation")
        if self.scope == "portfolio" and self.customer_id:
            raise ValueError("customer_id is not allowed for a portfolio investigation")
        return self


class NoteIn(_Body):
    kind: Literal["context", "observation", "document", "correction"] = "observation"
    content: str = Field(min_length=1, max_length=4000)
    category: ContextCategory | None = None
    document_name: str | None = Field(default=None, max_length=255)


class TaskRequestIn(_Body):
    specialist: Specialist
    question: str = Field(default="", max_length=600)
    hypothesis_code: str | None = Field(default=None, max_length=48)


class ChallengeIn(_Body):
    statement: str = Field(min_length=5, max_length=600)
    request_specialist: Specialist | None = None


class FindingReviewIn(_Body):
    action: Literal["accept_finding", "reject_finding", "flag_unsupported"]
    reason: str = Field(default="", max_length=2000)


class ChallengeResolutionIn(_Body):
    action: Literal["dismiss", "uphold"]
    reason: str = Field(min_length=5, max_length=2000)


class RecordReviewIn(_Body):
    action: Literal["dispute_evidence", "confirm_evidence"]
    reason: str = Field(min_length=5, max_length=2000)


class EvidenceValidationIn(_Body):
    outcome: Literal["complete", "incomplete"]
    reason: str = Field(default="", max_length=2000)
    flagged_finding_ids: list[str] = Field(default_factory=list, max_length=50)


class EvidenceRequestIn(_Body):
    description: str = Field(min_length=5, max_length=1000)


class AssignIn(_Body):
    analyst_user_id: str = Field(min_length=1, max_length=36)
    reason: str = Field(default="", max_length=2000)


class ReasonIn(_Body):
    reason: str = Field(min_length=5, max_length=2000)


class CompleteReviewIn(_Body):
    reason: str = Field(min_length=5, max_length=2000)
    recommended_classification: Classification | None = None


class ClassificationDecisionIn(_Body):
    action: Literal["accept_classification", "override_classification"]
    reason: str = Field(min_length=5, max_length=2000)
    classification: Classification | None = None
    severity: Literal["low", "moderate", "high", "severe"] | None = None


class InterventionDecisionIn(_Body):
    action: Literal["approve_intervention", "reject_intervention"]
    reason: str = Field(min_length=5, max_length=2000)


class PortfolioDecisionIn(_Body):
    action: Literal["accept_portfolio_assessment", "override_portfolio_assessment"]
    reason: str = Field(min_length=5, max_length=2000)
    classification: str | None = Field(default=None, max_length=32)


class MonitoringIn(_Body):
    reopen_score_delta: float | None = Field(default=None, ge=0, le=100)
    reopen_if_dpd_over: int | None = Field(default=None, ge=0, le=365)
    review_frequency_days: int | None = Field(default=None, ge=1, le=365)
    note: str = Field(default="", max_length=1000)
    reason: str = Field(default="", max_length=2000)


class RoleAssignmentIn(_Body):
    role: Literal[
        "unassigned",
        "relationship_officer",
        "credit_analyst",
        "credit_manager",
        "portfolio_manager",
        "risk_admin",
        "auditor",
    ]
    approval_limit: float | None = Field(default=None, ge=0)
    reason: str = Field(min_length=5, max_length=2000)


class ScopeIn(_Body):
    scope_type: Literal["all", "segment", "customer"]
    scope_value: str = Field(default="", max_length=64)
    reason: str = Field(min_length=5, max_length=2000)


class PolicyIn(_Body):
    config: dict
    reason: str = Field(min_length=5, max_length=2000)
