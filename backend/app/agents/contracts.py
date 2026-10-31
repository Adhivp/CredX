"""Typed contracts between the Investigation Director, specialists and the graph.

Specialists receive a ``TaskSpec`` and must return an ``AgentResult``; anything else
is rejected by the graph. Agents never receive a human ``User`` principal.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AgentId(StrEnum):
    DIRECTOR = "investigation_director"
    REPAYMENT = "repayment_specialist"
    CASHFLOW = "cashflow_specialist"
    EXPOSURE = "exposure_specialist"
    BASELINE = "baseline_specialist"
    INTEGRITY = "integrity_specialist"
    PORTFOLIO = "portfolio_specialist"
    CHALLENGER = "evidence_challenger"
    ADJUDICATOR = "credit_risk_adjudicator"


SPECIALISTS = frozenset(
    {
        AgentId.REPAYMENT,
        AgentId.CASHFLOW,
        AgentId.EXPOSURE,
        AgentId.BASELINE,
        AgentId.INTEGRITY,
        AgentId.PORTFOLIO,
    }
)

# Which specialist may ask which other specialist for help. The Director still has
# to authorise every request (depth, budget, duplicates, cycles).
DELEGATION_EDGES: dict[str, frozenset[AgentId]] = {
    AgentId.REPAYMENT: frozenset({AgentId.EXPOSURE, AgentId.BASELINE, AgentId.INTEGRITY}),
    AgentId.CASHFLOW: frozenset({AgentId.EXPOSURE, AgentId.BASELINE, AgentId.INTEGRITY}),
    AgentId.EXPOSURE: frozenset({AgentId.CASHFLOW, AgentId.INTEGRITY}),
    AgentId.BASELINE: frozenset({AgentId.INTEGRITY}),
    AgentId.INTEGRITY: frozenset(),
    AgentId.PORTFOLIO: frozenset(),
    AgentId.CHALLENGER: SPECIALISTS,
}

# Source tables each specialist may read. Enforced by the tool gateway.
DATA_SCOPES: dict[AgentId, frozenset[str]] = {
    AgentId.REPAYMENT: frozenset({"repayment_records", "loan_facilities", "customer_profile"}),
    AgentId.CASHFLOW: frozenset({"cashflow_records", "customer_profile", "human_notes"}),
    AgentId.EXPOSURE: frozenset({"customer_exposures", "loan_facilities", "cashflow_records"}),
    AgentId.BASELINE: frozenset({"repayment_records", "cashflow_records", "human_notes"}),
    AgentId.INTEGRITY: frozenset(
        {"repayment_records", "cashflow_records", "customer_exposures", "loan_facilities"}
    ),
    AgentId.PORTFOLIO: frozenset({"portfolio_aggregates"}),
}


class HypothesisDef(BaseModel):
    code: str
    statement: str
    agents: tuple[AgentId, ...]
    # Alternative explanations are not planned up front: the Evidence Challenger raises
    # them against a leading conclusion, or a human asks for them.
    alternative: bool = False
    scope: Literal["customer", "portfolio"] = "customer"


HYPOTHESES: dict[str, HypothesisDef] = {
    h.code: h
    for h in (
        HypothesisDef(
            code="repayment_deterioration",
            statement="Repayment behaviour has deteriorated relative to the customer's history",
            agents=(AgentId.REPAYMENT,),
        ),
        HypothesisDef(
            code="cashflow_decline",
            statement="Cash inflows are declining and reducing repayment capacity",
            agents=(AgentId.CASHFLOW,),
        ),
        HypothesisDef(
            code="obligation_pressure",
            statement="Rising exposure or obligations are creating repayment pressure",
            agents=(AgentId.EXPOSURE,),
        ),
        HypothesisDef(
            code="data_quality_artefact",
            statement="Apparent signals are artefacts of missing, duplicated or contradictory data",
            agents=(AgentId.INTEGRITY,),
        ),
        HypothesisDef(
            code="stable_profile",
            statement="There is no material deterioration in the customer's credit profile",
            agents=(AgentId.REPAYMENT,),
        ),
        HypothesisDef(
            code="temporary_disruption",
            statement="The deterioration is a temporary disruption rather than a sustained trend",
            agents=(AgentId.BASELINE,),
            alternative=True,
        ),
        HypothesisDef(
            code="seasonal_pattern",
            statement="Inflow changes reflect a recurring seasonal pattern",
            agents=(AgentId.BASELINE,),
            alternative=True,
        ),
        HypothesisDef(
            code="segment_wide_stress",
            statement="The customer's deterioration is part of a wider segment pattern",
            agents=(AgentId.PORTFOLIO,),
            alternative=True,
        ),
        HypothesisDef(
            code="human_reported_context",
            statement="Context reported by a human reviewer explains the observed behaviour",
            agents=(AgentId.BASELINE,),
        ),
        HypothesisDef(
            code="portfolio_cluster",
            statement="Deterioration is concentrated in specific portfolio segments",
            agents=(AgentId.PORTFOLIO,),
            scope="portfolio",
        ),
        HypothesisDef(
            code="portfolio_concentration",
            statement="Exposure concentration amplifies portfolio-level risk",
            agents=(AgentId.PORTFOLIO,),
            scope="portfolio",
        ),
    )
}

CONTEXT_CATEGORIES: dict[str, tuple[AgentId, ...]] = {
    "temporary_disruption": (AgentId.BASELINE, AgentId.CASHFLOW),
    "seasonal_business": (AgentId.BASELINE,),
    "income_change": (AgentId.CASHFLOW,),
    "dispute_or_error": (AgentId.INTEGRITY,),
    "new_obligation": (AgentId.EXPOSURE,),
    "other": (),
}


class _Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContextItem(_Contract):
    """Human-supplied context. ``text`` is sanitised, untrusted and never an instruction."""

    note_id: str
    category: str
    text: str
    author_role: str


class TaskSpec(_Contract):
    task_id: str
    investigation_id: str
    scope: Literal["customer", "portfolio"]
    hypothesis_id: str | None
    hypothesis_code: str
    question: str
    requested_evidence: list[str]
    permitted_data: list[str]
    assigned_agent: AgentId
    priority: int = Field(ge=0, le=100)
    depends_on: list[str]
    deadline_seconds: float
    budget: dict
    round: int
    delegation_depth: int
    requested_by: str
    portfolio_segment: str | None = None
    context: list[ContextItem] = []


class EvidenceRef(_Contract):
    source_table: Literal[
        "repayment_records",
        "cashflow_records",
        "customer_exposures",
        "loan_facilities",
        "portfolio_aggregates",
    ]
    record_id: str = Field(max_length=64)
    field: str = Field(default="", max_length=48)
    observed_value: str = Field(default="", max_length=128)


class FindingSpec(_Contract):
    code: str = Field(max_length=48)
    hypothesis_code: str
    summary: str = Field(max_length=600)
    direction: Literal["supports", "contradicts", "neutral", "insufficient"]
    confidence: float = Field(ge=0, le=1)
    uncertainty: str = Field(default="", max_length=400)
    contradictions: list[str] = []
    missing_evidence: list[str] = []
    evidence: list[EvidenceRef] = []
    calculation_names: list[str] = []
    metrics: dict = {}
    family: Literal["repayment", "cashflow", "exposure", "integrity", "portfolio"] | None = None
    interpretation: str | None = None
    interpretation_source: Literal["deterministic", "llm"] = "deterministic"


class FollowUpRequest(_Contract):
    target_agent: AgentId
    hypothesis_code: str
    question: str = Field(max_length=300)
    reason: str = Field(max_length=300)
    requested_evidence: list[str] = []


class AgentResult(_Contract):
    task_id: str
    agent_id: AgentId
    findings: list[FindingSpec]
    calculation_names: list[str]
    followups: list[FollowUpRequest] = []
    completion_status: Literal["complete", "partial", "insufficient_data", "failed"]
    notes: list[str] = []
    llm_status: str = "not_used"
