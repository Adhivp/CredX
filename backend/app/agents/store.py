"""Persistence for investigation state, events and agent results."""

from __future__ import annotations

import hashlib
import json
import time

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.contracts import AgentResult
from app.intelligence.data import CustomerSnapshot
from app.intelligence.indicators import Calculation
from app.models.investigation import (
    AgentFinding,
    AgentTask,
    CalculationRecord,
    EvidenceReference,
    Hypothesis,
    Investigation,
    InvestigationEvent,
)

TRANSITIONS: dict[str, set[str]] = {
    "created": {"investigating", "cancelled"},
    "investigating": {
        "awaiting_analyst_review",
        "awaiting_approval",
        "monitoring",
        "incomplete",
        "cancelled",
        "failed",
    },
    "awaiting_analyst_review": {"investigating", "awaiting_approval", "monitoring", "cancelled"},
    "awaiting_approval": {
        "approved",
        "rejected",
        "investigating",
        "awaiting_analyst_review",
        "cancelled",
    },
    "approved": {"reopened"},
    "rejected": {"reopened"},
    "monitoring": {"reopened"},
    "reopened": {"investigating", "cancelled"},
    "incomplete": {"investigating", "awaiting_analyst_review", "cancelled"},
    "failed": {"investigating", "cancelled"},
    "cancelled": set(),
}
MONITORED_STATUSES = {"monitoring", "approved", "rejected"}


class InvalidTransition(ValueError):
    pass


def now() -> int:
    return int(time.time())


def emit(
    db: Session,
    investigation: Investigation,
    event_type: str,
    actor_type: str,
    actor_id: str,
    payload: dict | None = None,
    task_id: str | None = None,
) -> InvestigationEvent:
    db.flush()
    seq = (
        db.scalar(
            select(func.max(InvestigationEvent.seq)).where(
                InvestigationEvent.investigation_id == investigation.id
            )
        )
        or 0
    ) + 1
    event = InvestigationEvent(
        investigation_id=investigation.id,
        seq=seq,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        round=investigation.current_round,
        task_id=task_id,
        payload=json.dumps(payload or {}, default=str),
        created_at=now(),
    )
    db.add(event)
    db.flush()
    return event


def transition(
    db: Session,
    investigation: Investigation,
    new_status: str,
    actor_type: str,
    actor_id: str,
    reason: str = "",
) -> None:
    old = investigation.status
    if new_status not in TRANSITIONS.get(old, set()):
        raise InvalidTransition(f"Cannot move investigation from {old} to {new_status}")
    investigation.status = new_status
    investigation.updated_at = now()
    emit(
        db,
        investigation,
        "status_changed",
        actor_type,
        actor_id,
        {"from": old, "to": new_status, "reason": reason},
    )


def dedup_key(agent_id: str, hypothesis_code: str, data_digest: str, extra: str = "") -> str:
    raw = f"{agent_id}|{hypothesis_code}|{data_digest}|{extra}"
    return hashlib.sha256(raw.encode()).hexdigest()[:64]


def find_duplicate(db: Session, investigation_id: str, key: str) -> AgentTask | None:
    return db.scalar(
        select(AgentTask).where(
            AgentTask.investigation_id == investigation_id,
            AgentTask.dedup_key == key,
            AgentTask.status.notin_(("rejected", "failed", "timeout", "cancelled")),
        )
    )


def hypothesis_by_code(db: Session, investigation_id: str, code: str) -> Hypothesis | None:
    return db.scalar(
        select(Hypothesis).where(
            Hypothesis.investigation_id == investigation_id, Hypothesis.code == code
        )
    )


def store_calculation(
    db: Session, investigation_id: str, calc: Calculation, cache: dict[str, str]
) -> str:
    """Persist a calculation once per investigation and input digest; return its id."""
    key = f"{calc.name}:{calc.input_digest}"
    if key in cache:
        return cache[key]
    existing = db.scalar(
        select(CalculationRecord.id).where(
            CalculationRecord.investigation_id == investigation_id,
            CalculationRecord.name == calc.name,
            CalculationRecord.input_digest == calc.input_digest,
        )
    )
    if existing is None:
        row = CalculationRecord(
            investigation_id=investigation_id,
            name=calc.name,
            version=calc.version,
            input_record_ids=json.dumps(list(calc.input_ids)),
            input_digest=calc.input_digest,
            output=json.dumps(calc.output, default=str),
            created_at=now(),
        )
        db.add(row)
        db.flush()
        existing = row.id
    cache[key] = existing
    return existing


def persist_result(
    db: Session,
    investigation: Investigation,
    task: AgentTask,
    result: AgentResult,
    calculations: dict[str, Calculation],
    snapshot: CustomerSnapshot | None,
    calc_cache: dict[str, str],
) -> tuple[list[AgentFinding], list[dict]]:
    """Store findings with validated evidence. Returns (findings, rejected evidence)."""
    valid_ids = snapshot.record_ids() if snapshot else set()
    portfolio_ids = {
        c.input_digest[:64] for c in calculations.values() if c.name == "portfolio_profile"
    }
    rejected: list[dict] = []
    stored: list[AgentFinding] = []
    for spec in result.findings:
        calc_ids = [
            store_calculation(db, investigation.id, calculations[name], calc_cache)
            for name in spec.calculation_names
            if name in calculations
        ]
        hypothesis = hypothesis_by_code(db, investigation.id, spec.hypothesis_code)
        finding = AgentFinding(
            investigation_id=investigation.id,
            task_id=task.id,
            agent_id=result.agent_id.value,
            hypothesis_id=hypothesis.id if hypothesis else None,
            round=task.round,
            code=spec.code,
            summary=spec.summary,
            direction=spec.direction,
            confidence=spec.confidence,
            uncertainty=spec.uncertainty,
            contradictions=json.dumps(spec.contradictions),
            missing_evidence=json.dumps(spec.missing_evidence),
            calculation_ids=json.dumps(calc_ids),
            metrics=json.dumps({**spec.metrics, "family": spec.family}, default=str),
            interpretation=spec.interpretation,
            interpretation_source=spec.interpretation_source,
            created_at=now(),
        )
        db.add(finding)
        db.flush()
        for ref in spec.evidence:
            if ref.source_table == "portfolio_aggregates":
                ok = ref.record_id in portfolio_ids
            else:
                ok = ref.record_id in valid_ids and snapshot.source_table(ref.record_id) == (
                    ref.source_table
                )
            if not ok:
                rejected.append({"finding": spec.code, "record_id": ref.record_id})
                continue
            db.add(
                EvidenceReference(
                    investigation_id=investigation.id,
                    finding_id=finding.id,
                    source_table=ref.source_table,
                    source_record_id=ref.record_id,
                    field=ref.field,
                    observed_value=ref.observed_value,
                    created_at=now(),
                )
            )
        stored.append(finding)
    db.flush()
    return stored, rejected
