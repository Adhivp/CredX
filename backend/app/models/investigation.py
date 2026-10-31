"""Investigation evidence graph persisted in the relational database.

Two kinds of tables:

* Working state (investigations, hypotheses, agent_tasks, evidence_challenges,
  review_items): their current status changes; every change is also written to
  ``investigation_events``.
* Records (findings, evidence references, calculations, adjudications, human
  decisions, notes, events, LLM calls, monitoring events): append-only. Database
  triggers (see APPEND_ONLY_TABLES) reject UPDATE and DELETE, so a human override can
  never silently rewrite an agent's original output.
"""

import time
import uuid

from sqlalchemy import Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.append_only import make_append_only
from app.db.session import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> int:
    return int(time.time())


class Investigation(Base):
    __tablename__ = "investigations"
    __table_args__ = (
        Index("ix_investigations_customer", "customer_id"),
        Index("ix_investigations_status", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    scope_type: Mapped[str] = mapped_column(String(16), default="customer")
    customer_id: Mapped[str | None] = mapped_column(ForeignKey("customers.id"), nullable=True)
    portfolio_segment: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="created")
    trigger: Mapped[str] = mapped_column(String(32), default="analyst_request")
    question: Mapped[str] = mapped_column(Text, default="")
    initiated_by: Mapped[str] = mapped_column(String(64))
    policy_version_id: Mapped[str] = mapped_column(ForeignKey("institution_policies.id"))
    current_round: Mapped[int] = mapped_column(Integer, default=0)
    pathway: Mapped[str | None] = mapped_column(String(8), nullable=True)
    assigned_analyst_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    run_state: Mapped[str] = mapped_column(String(16), default="idle")
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    usage: Mapped[str] = mapped_column(Text, default="{}")
    monitoring: Mapped[str] = mapped_column(Text, default="{}")
    last_monitored_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reopen_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[int] = mapped_column(default=_now)
    updated_at: Mapped[int] = mapped_column(default=_now)


class Hypothesis(Base):
    __tablename__ = "investigation_hypotheses"
    __table_args__ = (Index("ix_hypotheses_investigation", "investigation_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"))
    code: Mapped[str] = mapped_column(String(48))
    statement: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open")
    priority: Mapped[int] = mapped_column(Integer, default=50)
    source: Mapped[str] = mapped_column(String(64), default="director")
    rationale: Mapped[str] = mapped_column(Text, default="")
    round_created: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[int] = mapped_column(default=_now)
    updated_at: Mapped[int] = mapped_column(default=_now)


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    __table_args__ = (
        Index("ix_tasks_investigation", "investigation_id"),
        Index("ix_tasks_dedup", "investigation_id", "dedup_key"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"))
    hypothesis_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent_id: Mapped[str] = mapped_column(String(32))
    question: Mapped[str] = mapped_column(Text)
    requested_evidence: Mapped[str] = mapped_column(Text, default="[]")
    data_scope: Mapped[str] = mapped_column(Text, default="{}")
    priority: Mapped[int] = mapped_column(Integer, default=50)
    depends_on: Mapped[str] = mapped_column(Text, default="[]")
    requested_by: Mapped[str] = mapped_column(String(64))
    parent_task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    delegation_depth: Mapped[int] = mapped_column(Integer, default=0)
    round: Mapped[int] = mapped_column(Integer, default=1)
    wave: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    dedup_key: Mapped[str] = mapped_column(String(128))
    deadline_seconds: Mapped[float] = mapped_column(Float, default=20.0)
    budget: Mapped[str] = mapped_column(Text, default="{}")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    completion_status: Mapped[str | None] = mapped_column(String(24), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[int] = mapped_column(default=_now)
    started_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class AgentFinding(Base):
    __tablename__ = "agent_findings"
    __table_args__ = (Index("ix_findings_investigation", "investigation_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("agent_tasks.id"))
    agent_id: Mapped[str] = mapped_column(String(32))
    hypothesis_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    round: Mapped[int] = mapped_column(Integer, default=1)
    code: Mapped[str] = mapped_column(String(48))
    summary: Mapped[str] = mapped_column(Text)
    direction: Mapped[str] = mapped_column(String(16))
    confidence: Mapped[float] = mapped_column(Float)
    uncertainty: Mapped[str] = mapped_column(Text, default="")
    contradictions: Mapped[str] = mapped_column(Text, default="[]")
    missing_evidence: Mapped[str] = mapped_column(Text, default="[]")
    calculation_ids: Mapped[str] = mapped_column(Text, default="[]")
    metrics: Mapped[str] = mapped_column(Text, default="{}")
    interpretation: Mapped[str | None] = mapped_column(Text, nullable=True)
    interpretation_source: Mapped[str] = mapped_column(String(16), default="deterministic")
    created_at: Mapped[int] = mapped_column(default=_now)


class EvidenceReference(Base):
    __tablename__ = "evidence_references"
    __table_args__ = (Index("ix_evidence_finding", "finding_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"))
    finding_id: Mapped[str] = mapped_column(ForeignKey("agent_findings.id"))
    source_table: Mapped[str] = mapped_column(String(48))
    source_record_id: Mapped[str] = mapped_column(String(64))
    field: Mapped[str] = mapped_column(String(48), default="")
    observed_value: Mapped[str] = mapped_column(String(128), default="")
    created_at: Mapped[int] = mapped_column(default=_now)


class CalculationRecord(Base):
    __tablename__ = "calculation_records"
    __table_args__ = (Index("ix_calculations_investigation", "investigation_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"))
    name: Mapped[str] = mapped_column(String(64))
    version: Mapped[str] = mapped_column(String(16))
    input_record_ids: Mapped[str] = mapped_column(Text, default="[]")
    input_digest: Mapped[str] = mapped_column(String(64))
    output: Mapped[str] = mapped_column(Text)
    created_at: Mapped[int] = mapped_column(default=_now)


class EvidenceChallenge(Base):
    __tablename__ = "evidence_challenges"
    __table_args__ = (Index("ix_challenges_investigation", "investigation_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"))
    round: Mapped[int] = mapped_column(Integer, default=1)
    target_type: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(36))
    challenge_type: Mapped[str] = mapped_column(String(48))
    statement: Mapped[str] = mapped_column(Text)
    materiality: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="open")
    raised_by: Mapped[str] = mapped_column(String(64))
    followup_task_ids: Mapped[str] = mapped_column(Text, default="[]")
    resolution: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(default=_now)
    resolved_at: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Adjudication(Base):
    __tablename__ = "adjudications"
    __table_args__ = (Index("ix_adjudications_investigation", "investigation_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"))
    round: Mapped[int] = mapped_column(Integer)
    classification: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    risk_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    confidence: Mapped[float] = mapped_column(Float)
    recommendation: Mapped[str] = mapped_column(String(48))
    proposed_interventions: Mapped[str] = mapped_column(Text, default="[]")
    rationale: Mapped[str] = mapped_column(Text, default="[]")
    pathway: Mapped[str] = mapped_column(String(8))
    escalation_reasons: Mapped[str] = mapped_column(Text, default="[]")
    completeness: Mapped[str] = mapped_column(String(16))
    exposure_at_risk: Mapped[float] = mapped_column(Float, default=0.0)
    scoring_version: Mapped[str] = mapped_column(String(16))
    policy_version_id: Mapped[str] = mapped_column(String(36))
    calculation_id: Mapped[str] = mapped_column(String(36))
    finding_ids: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[int] = mapped_column(default=_now)


class ReviewItem(Base):
    __tablename__ = "review_items"
    __table_args__ = (Index("ix_review_items_queue", "queue", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"))
    adjudication_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    queue: Mapped[str] = mapped_column(String(32))
    required_role: Mapped[str] = mapped_column(String(32))
    stage: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16), default="open")
    reasons: Mapped[str] = mapped_column(Text, default="[]")
    assigned_to: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[int] = mapped_column(default=_now)
    completed_at: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completed_by: Mapped[str | None] = mapped_column(String(36), nullable=True)


class HumanDecision(Base):
    __tablename__ = "human_decisions"
    __table_args__ = (Index("ix_decisions_investigation", "investigation_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"))
    customer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    review_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # A real authenticated user is mandatory: agents cannot record decisions.
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(32))
    action: Mapped[str] = mapped_column(String(48))
    target_type: Mapped[str] = mapped_column(String(24), default="investigation")
    target_id: Mapped[str] = mapped_column(String(36), default="")
    previous_state: Mapped[str] = mapped_column(Text, default="{}")
    new_state: Mapped[str] = mapped_column(Text, default="{}")
    reason: Mapped[str] = mapped_column(Text, default="")
    evidence_refs: Mapped[str] = mapped_column(Text, default="[]")
    policy_version_id: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[int] = mapped_column(default=_now)


class HumanNote(Base):
    """Context or supporting material added by a human. Stored and handled as untrusted text."""

    __tablename__ = "human_notes"
    __table_args__ = (Index("ix_notes_investigation", "investigation_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"))
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(32))
    kind: Mapped[str] = mapped_column(String(16))
    # Structured context category (see agents.contracts.CONTEXT_CATEGORIES); the Director
    # acts on this, never on the free text.
    category: Mapped[str | None] = mapped_column(String(32), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    document_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[int] = mapped_column(default=_now)


class InvestigationEvent(Base):
    __tablename__ = "investigation_events"
    __table_args__ = (UniqueConstraint("investigation_id", "seq", name="uq_event_seq"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"))
    seq: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(48))
    actor_type: Mapped[str] = mapped_column(String(16))
    actor_id: Mapped[str] = mapped_column(String(64))
    round: Mapped[int] = mapped_column(Integer, default=0)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    payload: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[int] = mapped_column(default=_now)


class LLMCall(Base):
    __tablename__ = "llm_calls"
    __table_args__ = (Index("ix_llm_calls_investigation", "investigation_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    task_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent_id: Mapped[str] = mapped_column(String(32))
    purpose: Mapped[str] = mapped_column(String(48))
    model: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(24))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[int] = mapped_column(default=_now)


class MonitoringEvent(Base):
    __tablename__ = "monitoring_events"
    __table_args__ = (Index("ix_monitoring_investigation", "investigation_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    investigation_id: Mapped[str] = mapped_column(ForeignKey("investigations.id"))
    customer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    trigger: Mapped[str] = mapped_column(String(32))
    baseline_adjudication_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    evaluation: Mapped[str] = mapped_column(Text, default="{}")
    decision: Mapped[str] = mapped_column(String(16))
    created_at: Mapped[int] = mapped_column(default=_now)


APPEND_ONLY_TABLES = (
    "agent_findings",
    "evidence_references",
    "calculation_records",
    "adjudications",
    "human_decisions",
    "human_notes",
    "investigation_events",
    "llm_calls",
    "monitoring_events",
    "audit_logs",
)

for _model in (
    AgentFinding,
    EvidenceReference,
    CalculationRecord,
    Adjudication,
    HumanDecision,
    HumanNote,
    InvestigationEvent,
    LLMCall,
    MonitoringEvent,
):
    make_append_only(_model.__table__)
