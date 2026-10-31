import json

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── Customer ──────────────────────────────────────────────────────────

class CustomerCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str = Field(min_length=1, max_length=64)
    customer_type: str = Field(default="individual", max_length=32)
    employment_category: str = Field(default="", max_length=64)
    portfolio_segment: str = Field(default="", max_length=64)


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    customer_id: str
    customer_type: str
    employment_category: str
    portfolio_segment: str
    created_at: int


# ── Loan Facility ─────────────────────────────────────────────────────

class LoanFacilityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str
    loan_id: str = Field(min_length=1, max_length=64)
    product_type: str = Field(default="", max_length=64)
    sanctioned_amount: float = Field(ge=0)
    outstanding_balance: float = Field(ge=0)
    tenure_months: int = Field(ge=0)
    instalment_amount: float = Field(ge=0)
    disbursement_date: str = ""
    status: str = "active"


class LoanFacilityOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    customer_id: str
    loan_id: str
    product_type: str
    sanctioned_amount: float
    outstanding_balance: float
    tenure_months: int
    instalment_amount: float
    disbursement_date: str
    status: str


# ── Repayment ─────────────────────────────────────────────────────────

class RepaymentRecordOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    loan_id: str
    due_date: str
    payment_date: str | None
    amount_due: float
    amount_paid: float
    dpd: int
    repayment_status: str


# ── Warning Assessment ────────────────────────────────────────────────

class WarningAssessmentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    customer_id: str
    warning_score: float
    risk_category: str
    rule_delay_score: float
    rule_miss_score: float
    rule_exposure_score: float
    rule_completeness_score: float
    cashflow_score: float
    top_indicators: list[str]
    assessed_at: int

    @field_validator("top_indicators", mode="before")
    @classmethod
    def parse_indicators(cls, v: str | list) -> list[str]:
        if isinstance(v, str):
            return json.loads(v)
        return v


# ── Watchlist ─────────────────────────────────────────────────────────

class WatchlistEntry(BaseModel):
    customer_id: str
    product_type: str = ""
    outstanding_exposure: float = 0.0
    warning_score: float
    risk_category: str
    top_indicators: list[str]
    active_signals: int
    monitoring_status: str


class WatchlistFilters(BaseModel):
    product_type: str | None = None
    customer_segment: str | None = None
    risk_level: str | None = None
    min_score: float = 0.0
    max_results: int = Field(default=100, ge=1, le=1000)


class WatchlistResponse(BaseModel):
    items: list[WatchlistEntry]
    total: int
    filters: WatchlistFilters


# ── Alert Classification ─────────────────────────────────────────────

VALID_DECISIONS = {
    "Continue Monitoring",
    "Requires Investigation",
    "False Positive",
    "Escalate for Review",
}


class AlertCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: str
    reason: str = Field(min_length=1, max_length=2000)
    analyst_id: str | None = None

    @field_validator("decision")
    @classmethod
    def validate_decision(cls, v: str) -> str:
        if v not in VALID_DECISIONS:
            raise ValueError(f"decision must be one of: {VALID_DECISIONS}")
        return v


class AlertOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    assessment_id: str
    decision: str
    reason: str
    analyst_id: str | None
    original_score: float
    original_category: str
    preserved_evidence: dict
    created_at: int

    @field_validator("preserved_evidence", mode="before")
    @classmethod
    def parse_evidence(cls, v: str | dict) -> dict:
        if isinstance(v, str):
            return json.loads(v)
        return v


# ── Data Upload ───────────────────────────────────────────────────────

class DataUploadResponse(BaseModel):
    upload_id: str
    filename: str
    records_loaded: int
    status: str
    errors: list[str]

    @field_validator("errors", mode="before")
    @classmethod
    def parse_errors(cls, v: str | list | None) -> list[str]:
        if v is None:
            return []
        if isinstance(v, str):
            return json.loads(v)
        return v


# ── Customer Detail ───────────────────────────────────────────────────

class CustomerDetailOut(BaseModel):
    customer: CustomerOut
    loans: list[LoanFacilityOut]
    repayments: list[RepaymentRecordOut]
    assessments: list[WarningAssessmentOut]
    alerts: list[AlertOut]


# ── AI Explainability ─────────────────────────────────────────────────

class ExplainRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    customer_id: str


class ExplainResponse(BaseModel):
    customer_id: str
    explanation: str
    contributing_factors: list[dict]
    risk_factors: list[str] = []
    recommended_actions: list[str] = []
    data_status: str = "assessable"  # "assessable" | "insufficient"
    source: str = "fallback"  # "ai" | "fallback"
    model: str | None = None
