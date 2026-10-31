"""Continuous monitoring: decide whether new evidence should reopen an investigation.

Runs the deterministic evaluation on the customer's current data and compares it with
the last adjudication. Reopen rules come from the investigation's policy version plus
any monitoring instructions a human set on the case.
"""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import store
from app.intelligence.data import load_customer_snapshot
from app.intelligence.scoring import evaluate_snapshot
from app.models.investigation import CalculationRecord, Investigation, MonitoringEvent
from app.services.investigations import latest_adjudication
from app.services.policy import get_policy_version


def evaluate_investigation(db: Session, inv: Investigation, trigger: str) -> str:
    """Return 'reopen', 'no_change' or 'skipped' and record a monitoring event."""
    if inv.scope_type != "customer" or inv.status not in store.MONITORED_STATUSES:
        return "skipped"
    adj = latest_adjudication(db, inv)
    policy = get_policy_version(db, inv.policy_version_id)
    instructions = json.loads(inv.monitoring or "{}")
    evaluation = evaluate_snapshot(load_customer_snapshot(db, inv.customer_id), policy)
    current = evaluation.assessment
    rep = evaluation.calculations["repayment_profile"].output
    reasons = []
    baseline = {}
    if adj is not None:
        inputs_row = db.get(CalculationRecord, adj.calculation_id)
        baseline = json.loads(inputs_row.output) if inputs_row else {}
        delta_limit = instructions.get("reopen_score_delta", policy.monitoring.reopen_score_delta)
        if adj.risk_score is not None and current.risk_score is not None:
            delta = current.risk_score - adj.risk_score
            if abs(delta) >= delta_limit:
                reasons.append(f"Risk score moved {delta:+.1f} (threshold {delta_limit})")
        if (
            policy.monitoring.reopen_on_classification_change
            and current.classification != adj.classification
        ):
            reasons.append(
                f"Classification would change from {adj.classification} to {current.classification}"
            )
        previous_missed = baseline.get("repayment_missed_count")
        if (
            policy.monitoring.reopen_on_new_missed_payment
            and previous_missed is not None
            and (rep.get("missed_count") or 0) > previous_missed
        ):
            reasons.append(
                f"{rep['missed_count'] - previous_missed} new missed payment(s) since adjudication"
            )
    dpd_limit = instructions.get("reopen_if_dpd_over")
    if dpd_limit is not None and (rep.get("recent_max_dpd") or 0) > dpd_limit:
        reasons.append(f"Recent DPD {rep['recent_max_dpd']} exceeds instructed limit {dpd_limit}")
    new_period = baseline.get("latest_repayment_month") != (rep.get("months") or [None])[-1]
    decision = "reopen" if reasons else "no_change"
    db.add(
        MonitoringEvent(
            investigation_id=inv.id,
            customer_id=inv.customer_id,
            trigger=trigger,
            baseline_adjudication_id=adj.id if adj else None,
            evaluation=json.dumps(
                {
                    "classification": current.classification,
                    "severity": current.severity,
                    "risk_score": current.risk_score,
                    "new_period": new_period,
                    "reasons": reasons,
                    "instructions": instructions,
                }
            ),
            decision=decision,
            created_at=store.now(),
        )
    )
    inv.last_monitored_at = store.now()
    store.emit(
        db,
        inv,
        "monitoring_evaluated",
        "system",
        "monitoring",
        {"decision": decision, "reasons": reasons, "trigger": trigger},
    )
    if decision == "reopen":
        store.transition(db, inv, "reopened", "system", "monitoring", "; ".join(reasons))
        inv.reopen_count += 1
        inv.trigger = "monitoring_reopen"
    return decision


def evaluate_customers(db: Session, customer_pks: set[str], trigger: str) -> list[str]:
    """Evaluate every monitored investigation for these customers; return reopened ids."""
    if not customer_pks:
        return []
    reopened = []
    for inv in db.scalars(
        select(Investigation).where(
            Investigation.customer_id.in_(customer_pks),
            Investigation.status.in_(store.MONITORED_STATUSES),
        )
    ):
        if evaluate_investigation(db, inv, trigger) == "reopen":
            reopened.append(inv.id)
    return reopened
