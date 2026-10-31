"""Serialisation of investigation state for the workspace and review queue.

Everything here is read from persisted backend state; nothing is synthesised for
display. Text written by humans or derived from uploaded data is returned as data and
must be escaped by clients.
"""

from __future__ import annotations

import json

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.agents.view import load_view
from app.models.ews import Customer, LoanFacility
from app.models.governance import InstitutionPolicy
from app.models.investigation import (
    CalculationRecord,
    EvidenceReference,
    Investigation,
    InvestigationEvent,
    LLMCall,
    MonitoringEvent,
    ReviewItem,
)
from app.models.user import User

J = json.loads


def _user_names(db: Session, ids: set[str]) -> dict[str, dict]:
    ids = {i for i in ids if i}
    if not ids:
        return {}
    return {
        u.id: {"name": u.full_name, "role": u.role}
        for u in db.scalars(select(User).where(User.id.in_(ids)))
    }


def effective_classification(adjudications, decisions) -> dict | None:
    """Latest adjudication, with any later human override applied on top (never in place)."""
    if not adjudications:
        return None
    adj = adjudications[-1]
    result = {
        "classification": adj.classification,
        "severity": adj.severity,
        "source": "adjudicator",
        "adjudication_id": adj.id,
    }
    for d in decisions:
        if d.target_id == adj.id and d.action in {
            "override_classification",
            "override_portfolio_assessment",
        }:
            new = J(d.new_state)
            result = {
                "classification": new.get("classification"),
                "severity": new.get("severity", adj.severity),
                "source": "human_override",
                "adjudication_id": adj.id,
                "decision_id": d.id,
            }
        elif d.target_id == adj.id and d.action in {
            "accept_classification",
            "accept_portfolio_assessment",
        }:
            result = result | {"source": "human_accepted", "decision_id": d.id}
    return result


def summary(db: Session, inv: Investigation) -> dict:
    customer = db.get(Customer, inv.customer_id) if inv.customer_id else None
    view = load_view(db, inv)
    return {
        "id": inv.id,
        "scope": inv.scope_type,
        "customer_id": customer.customer_id if customer else None,
        "segment": customer.portfolio_segment if customer else inv.portfolio_segment,
        "status": inv.status,
        "run_state": inv.run_state,
        "round": inv.current_round,
        "pathway": inv.pathway,
        "outcome": inv.outcome,
        "trigger": inv.trigger,
        "assigned_analyst_id": inv.assigned_analyst_id,
        "effective_classification": effective_classification(view.adjudications, view.decisions),
        "created_at": inv.created_at,
        "updated_at": inv.updated_at,
    }


def workspace(db: Session, inv: Investigation) -> dict:
    view = load_view(db, inv)
    customer = db.get(Customer, inv.customer_id) if inv.customer_id else None
    rejected = view.rejected_finding_ids
    finding_reviews: dict[str, dict] = {}
    for d in view.decisions:
        if d.target_type == "finding":
            finding_reviews[d.target_id] = {
                "action": d.action,
                "reason": d.reason,
                "user_id": d.user_id,
                "decision_id": d.id,
            }
    evidence = list(
        db.scalars(select(EvidenceReference).where(EvidenceReference.investigation_id == inv.id))
    )
    by_finding: dict[str, list] = {}
    for e in evidence:
        by_finding.setdefault(e.finding_id, []).append(
            {
                "source_table": e.source_table,
                "record_id": e.source_record_id,
                "field": e.field,
                "observed_value": e.observed_value,
            }
        )
    calcs = {
        c.id: {
            "id": c.id,
            "name": c.name,
            "version": c.version,
            "input_digest": c.input_digest,
            "output": J(c.output),
            "input_record_count": len(J(c.input_record_ids)),
        }
        for c in db.scalars(
            select(CalculationRecord).where(CalculationRecord.investigation_id == inv.id)
        )
    }
    events = list(
        db.scalars(
            select(InvestigationEvent)
            .where(InvestigationEvent.investigation_id == inv.id)
            .order_by(InvestigationEvent.seq)
        )
    )
    reviews = list(
        db.scalars(
            select(ReviewItem)
            .where(ReviewItem.investigation_id == inv.id)
            .order_by(text("review_items.rowid"))
        )
    )
    llm = db.execute(
        select(
            LLMCall.status,
            func.count(),
            func.sum(LLMCall.prompt_tokens),
            func.sum(LLMCall.completion_tokens),
            func.sum(LLMCall.cost_usd),
        )
        .where(LLMCall.investigation_id == inv.id)
        .group_by(LLMCall.status)
    ).all()
    monitoring = list(
        db.scalars(
            select(MonitoringEvent)
            .where(MonitoringEvent.investigation_id == inv.id)
            .order_by(text("monitoring_events.rowid"))
        )
    )
    policy = db.get(InstitutionPolicy, inv.policy_version_id)
    users = _user_names(
        db,
        {d.user_id for d in view.decisions}
        | {n.user_id for n in view.notes}
        | {inv.assigned_analyst_id, inv.initiated_by},
    )
    loans = (
        db.scalars(select(LoanFacility).where(LoanFacility.customer_id == customer.id)).all()
        if customer
        else []
    )
    return {
        **summary(db, inv),
        "question": inv.question,
        "initiated_by": inv.initiated_by,
        "policy": {"id": policy.id, "version": policy.version} if policy else None,
        "monitoring_instructions": J(inv.monitoring or "{}"),
        "usage": J(inv.usage or "{}"),
        "customer": None
        if customer is None
        else {
            "customer_id": customer.customer_id,
            "customer_type": customer.customer_type,
            "segment": customer.portfolio_segment,
            "employment_category": customer.employment_category,
            "facilities": [
                {
                    "loan_id": loan.loan_id,
                    "product_type": loan.product_type,
                    "sanctioned_amount": loan.sanctioned_amount,
                    "outstanding_balance": loan.outstanding_balance,
                    "instalment_amount": loan.instalment_amount,
                    "status": loan.status,
                }
                for loan in loans
            ],
        },
        "hypotheses": [
            {
                "id": h.id,
                "code": h.code,
                "statement": h.statement,
                "status": h.status,
                "priority": h.priority,
                "source": h.source,
                "rationale": h.rationale,
                "round_created": h.round_created,
            }
            for h in view.hypotheses
        ],
        "tasks": [
            {
                "id": t.id,
                "agent": t.agent_id,
                "hypothesis_id": t.hypothesis_id,
                "question": t.question,
                "status": t.status,
                "requested_by": t.requested_by,
                "parent_task_id": t.parent_task_id,
                "delegation_depth": t.delegation_depth,
                "round": t.round,
                "wave": t.wave,
                "attempts": t.attempts,
                "completion_status": t.completion_status,
                "error": t.error,
                "started_at": t.started_at,
                "completed_at": t.completed_at,
            }
            for t in view.tasks
        ],
        "findings": [
            {
                "id": f.id,
                "task_id": f.task_id,
                "agent": f.agent_id,
                "hypothesis_id": f.hypothesis_id,
                "round": f.round,
                "code": f.code,
                "summary": f.summary,
                "direction": f.direction,
                "confidence": f.confidence,
                "uncertainty": f.uncertainty,
                "contradictions": J(f.contradictions),
                "missing_evidence": J(f.missing_evidence),
                "metrics": J(f.metrics),
                "calculations": [calcs[c] for c in J(f.calculation_ids) if c in calcs],
                "evidence": by_finding.get(f.id, []),
                "interpretation": f.interpretation,
                "interpretation_source": f.interpretation_source,
                "human_review": finding_reviews.get(f.id),
                "excluded_from_adjudication": f.id in rejected,
            }
            for f in view.findings
        ],
        "challenges": [
            {
                "id": c.id,
                "round": c.round,
                "type": c.challenge_type,
                "target_type": c.target_type,
                "target_id": c.target_id,
                "statement": c.statement,
                "materiality": c.materiality,
                "status": c.status,
                "raised_by": c.raised_by,
                "followup_task_ids": J(c.followup_task_ids),
                "resolution": c.resolution,
            }
            for c in view.challenges
        ],
        "adjudications": [
            {
                "id": a.id,
                "round": a.round,
                "classification": a.classification,
                "severity": a.severity,
                "risk_score": a.risk_score,
                "confidence": a.confidence,
                "recommendation": a.recommendation,
                "proposed_interventions": J(a.proposed_interventions),
                "rationale": J(a.rationale),
                "pathway": a.pathway,
                "escalation_reasons": J(a.escalation_reasons),
                "completeness": a.completeness,
                "exposure_at_risk": a.exposure_at_risk,
                "scoring_version": a.scoring_version,
                "calculation": calcs.get(a.calculation_id),
                "created_at": a.created_at,
            }
            for a in view.adjudications
        ],
        "reviews": [
            {
                "id": r.id,
                "queue": r.queue,
                "required_role": r.required_role,
                "stage": r.stage,
                "status": r.status,
                "reasons": J(r.reasons),
                "assigned_to": r.assigned_to,
                "adjudication_id": r.adjudication_id,
                "created_at": r.created_at,
                "completed_at": r.completed_at,
                "completed_by": r.completed_by,
            }
            for r in reviews
        ],
        "decisions": [
            {
                "id": d.id,
                "user": users.get(d.user_id, {"name": None}) | {"id": d.user_id},
                "role": d.role,
                "action": d.action,
                "target_type": d.target_type,
                "target_id": d.target_id,
                "previous_state": J(d.previous_state),
                "new_state": J(d.new_state),
                "reason": d.reason,
                "evidence_refs": J(d.evidence_refs),
                "policy_version_id": d.policy_version_id,
                "created_at": d.created_at,
            }
            for d in view.decisions
        ],
        "notes": [
            {
                "id": n.id,
                "kind": n.kind,
                "category": n.category,
                "content": n.content,
                "document_name": n.document_name,
                "role": n.role,
                "user": users.get(n.user_id, {}).get("name"),
                "created_at": n.created_at,
            }
            for n in view.notes
        ],
        "events": [
            {
                "seq": e.seq,
                "type": e.event_type,
                "actor_type": e.actor_type,
                "actor_id": e.actor_id,
                "round": e.round,
                "task_id": e.task_id,
                "payload": J(e.payload),
                "created_at": e.created_at,
            }
            for e in events
        ],
        "llm_usage": [
            {
                "status": s,
                "calls": n,
                "prompt_tokens": p or 0,
                "completion_tokens": c or 0,
                "cost_usd": round(cost or 0, 6),
            }
            for s, n, p, c, cost in llm
        ],
        "monitoring_events": [
            {
                "id": m.id,
                "trigger": m.trigger,
                "decision": m.decision,
                "evaluation": J(m.evaluation),
                "created_at": m.created_at,
            }
            for m in monitoring
        ],
    }
