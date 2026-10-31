"""One analysed row per customer: latest snapshot, trend, correlation, priority and
recommendations, plus case / intervention state. Shared by the dashboard, the priority
queue and the investigation workspace so all three agree."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ews import Customer, CustomerExposure, LoanFacility
from app.models.monitoring import EventClassification, Intervention, InvestigationCase
from app.services.correlation import correlate
from app.services.prioritisation import assign_tiers, prioritise
from app.services.recommendations import recommend
from app.services.risk_history import analyse_trend, load_history

OPEN_CASE_STATUSES = {"open", "under_investigation", "awaiting_information", "action_in_progress", "monitoring"}
OPEN_INTERVENTION_STATUSES = {"proposed", "pending_approval", "approved", "initiated", "deferred", "escalated"}


@dataclass
class Filters:
    product: str | None = None
    segment: str | None = None
    portfolio: str | None = None
    period: str | None = None  # YYYY-MM: analyse as of this month end


def segment_of(c: Customer) -> str:
    """Customer segment = customer type + employment (e.g. 'individual · salaried')."""
    parts = [p for p in (c.customer_type, c.employment_category) if p and p != "None"]
    return " · ".join(parts) or "unspecified"


def facilities_for(db: Session, internal_ids: list[str]) -> dict[str, list[dict]]:
    """Latest exposure snapshot rows per customer; loans when there is no snapshot.
    Loans and exposure describe the same facilities, so only one source is used."""
    out: dict[str, list[dict]] = {}
    latest = (
        select(CustomerExposure.customer_id, func.max(CustomerExposure.snapshot_date).label("snap"))
        .where(CustomerExposure.customer_id.in_(internal_ids))
        .group_by(CustomerExposure.customer_id).subquery()
    )
    rows = db.execute(
        select(CustomerExposure).join(latest, (CustomerExposure.customer_id == latest.c.customer_id)
                                      & (CustomerExposure.snapshot_date == latest.c.snap))
    ).scalars().all()
    seen = set()
    for r in rows:
        key = (r.customer_id, r.facility_type, r.disbursement_date, r.sanctioned_amount)
        if key in seen:
            continue
        seen.add(key)
        out.setdefault(r.customer_id, []).append({
            "facility_type": r.facility_type, "sanctioned_amount": r.sanctioned_amount,
            "credit_limit": r.credit_limit, "current_utilisation": r.current_utilisation,
            "outstanding_balance": r.outstanding_balance, "instalment_amount": r.instalment_amount,
            "disbursement_date": r.disbursement_date, "snapshot_date": r.snapshot_date, "source": "exposure",
        })
    for loan in db.execute(select(LoanFacility).where(LoanFacility.customer_id.in_(internal_ids))).scalars():
        if loan.customer_id in out and any(f["source"] == "exposure" for f in out[loan.customer_id]):
            continue
        out.setdefault(loan.customer_id, []).append({
            "facility_type": loan.product_type, "sanctioned_amount": loan.sanctioned_amount,
            "credit_limit": 0.0, "outstanding_balance": loan.outstanding_balance,
            "instalment_amount": loan.instalment_amount, "loan_id": loan.loan_id, "source": "loan",
        })
    return out


def products_for(db: Session) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for cid, p in db.execute(select(LoanFacility.customer_id, LoanFacility.product_type)).all():
        if p:
            out.setdefault(cid, set()).add(p)
    for cid, p in db.execute(select(CustomerExposure.customer_id, CustomerExposure.facility_type)).all():
        if p:
            out.setdefault(cid, set()).add(p)
    return out


def analyse_customer(history: list[dict], facilities: list[dict], case: InvestigationCase | None,
                     excluded_events: int = 0) -> dict:
    latest = history[-1]
    trend = analyse_trend(history)
    corr = correlate(history, trend)
    corrected = case.corrected_category if case and case.status in OPEN_CASE_STATUSES else None
    prio = prioritise(latest, trend, corr, facilities, latest.get("default_probability"), corrected)
    return {"latest": latest, "trend": trend, "correlation": corr, "priority": prio, "recommendations": [],
            "_excluded": excluded_events}


def finish(rows: list[dict]) -> None:
    """Portfolio-relative steps: natural-break tiers, then recommendations."""
    assign_tiers([r["priority"] for r in rows])
    for r in rows:
        r["recommendations"] = recommend(r["history"], r["trend"], r["correlation"], r["priority"], r.pop("_excluded", 0))


def portfolio_rows(db: Session, filters: Filters | None = None) -> list[dict]:
    filters = filters or Filters()
    customers = db.execute(select(Customer)).scalars().all()
    products = products_for(db)
    selected = []
    for c in customers:
        if filters.portfolio and (c.portfolio_segment or "") != filters.portfolio:
            continue
        if filters.segment and segment_of(c) != filters.segment:
            continue
        if filters.product and filters.product not in products.get(c.id, set()):
            continue
        selected.append(c)
    if not selected:
        return []
    ids = [c.id for c in selected]
    histories = load_history(db, ids, filters.period)
    facilities = facilities_for(db, ids)

    cases: dict[str, InvestigationCase] = {}
    for case in db.execute(
        select(InvestigationCase).where(InvestigationCase.customer_id.in_(ids)).order_by(InvestigationCase.created_at)
    ).scalars():
        if case.status in OPEN_CASE_STATUSES or case.customer_id not in cases:
            cases[case.customer_id] = case
    open_iv: dict[str, list[Intervention]] = {}
    for iv in db.execute(
        select(Intervention).where(Intervention.customer_id.in_(ids), Intervention.status.in_(OPEN_INTERVENTION_STATUSES))
    ).scalars():
        open_iv.setdefault(iv.customer_id, []).append(iv)
    excluded = dict(db.execute(
        select(EventClassification.customer_id, func.count())
        .where(EventClassification.excluded_from_scoring == 1, EventClassification.customer_id.in_(ids))
        .group_by(EventClassification.customer_id)
    ).all())

    today = date.today().isoformat()
    rows = []
    for c in selected:
        hist = histories.get(c.id)
        if not hist:
            continue
        case = cases.get(c.id)
        a = analyse_customer(hist, facilities.get(c.id, []), case, excluded.get(c.id, 0))
        ivs = open_iv.get(c.id, [])
        rows.append({
            "history": hist,
            "customer_id": c.customer_id,
            "internal_id": c.id,
            "customer_type": c.customer_type,
            "segment": segment_of(c),
            "portfolio": c.portfolio_segment or "unassigned",
            "products": sorted(products.get(c.id, set())),
            **a,
            "case": {
                "id": case.id, "reference": case.reference, "status": case.status, "owner": case.owner,
                "disposition": case.disposition, "corrected_category": case.corrected_category,
            } if case else None,
            "open_interventions": len(ivs),
            "overdue_interventions": sum(1 for i in ivs if i.target_date and i.target_date < today),
        })
    finish(rows)
    return rows


def compact(row: dict) -> dict:
    """List-view shape (no full history)."""
    latest, trend, prio, corr = row["latest"], row["trend"], row["priority"], row["correlation"]
    return {
        "customer_id": row["customer_id"],
        "segment": row["segment"],
        "portfolio": row["portfolio"],
        "products": row["products"],
        "period": latest["period"],
        "warning_score": latest["warning_score"],
        "model_category": latest["risk_category"],
        "warning_level": prio["warning_level"],
        "effective_score": prio["effective_score"],
        "correlation_uplift": corr["uplift"],
        "co_moving_signals": corr["co_moving"],
        "trend": trend["direction"],
        "delta_3m": trend.get("delta_3m"),
        "priority_score": prio["priority_score"],
        "tier": prio["tier"],
        "tier_label": prio["tier_label"],
        "quadrant": prio["quadrant"],
        "probability_of_deterioration": prio["probability_of_deterioration"],
        "probability_lift": prio["probability_lift"],
        "outstanding": prio["exposure"]["outstanding"],
        "ead": prio["exposure"]["ead"],
        "expected_loss": prio["expected_loss"],
        "loss_if_default": prio["loss_if_default"],
        "delinquency": prio["delinquency"],
        "top_recommendation": row["recommendations"][0]["action"] if row["recommendations"] else None,
        "recommendation_count": len(row["recommendations"]),
        "case": row["case"],
        "open_interventions": row["open_interventions"],
        "overdue_interventions": row["overdue_interventions"],
        "summary": trend.get("summary", ""),
    }


def dumps(v) -> str:
    return json.dumps(v)
