"""Level 2 API: risk trends, correlation, prioritisation, recommendations, the
investigation workspace, intervention tracking and the portfolio EWS dashboard.

Guard rail: no endpoint here changes a loan, a credit limit or contacts a customer.
Interventions are records of human actions; customer-impacting ones need approval
from a second, authenticated user before they can be initiated.
"""

from __future__ import annotations

import json
import time
from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.ews import AlertClassification, Customer, LoanFacility, RepaymentRecord, WarningAssessment
from app.models.monitoring import CaseActivity, EventClassification, Intervention, InvestigationCase, RiskSnapshot
from app.schemas.monitoring import (
    ApprovalDecision,
    CaseCreate,
    CaseUpdate,
    CategoryCorrection,
    EventClassificationCreate,
    InfoRequestCreate,
    InfoRequestUpdate,
    InterventionCreate,
    InterventionUpdate,
    NoteCreate,
)
from app.services.auth import authenticate_access
from app.services.portfolio_view import (
    OPEN_CASE_STATUSES,
    OPEN_INTERVENTION_STATUSES,
    Filters,
    compact,
    facilities_for,
    portfolio_rows,
    products_for,
    segment_of,
)
from app.services.prioritisation import delinquency_status
from app.services.recommendations import PLAYBOOK, SCENARIO_LABELS
from app.services.risk_history import analyse_trend, load_history, rebuild_history
from app.services.scoring import EXCLUDING_CLASSIFICATIONS, INSUFFICIENT_DATA

router = APIRouter(prefix="/ews", tags=["ews-monitoring"])
bearer = HTTPBearer(auto_error=False)
Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]

LEVELS = ["Critical", "High", "Medium", "Low", INSUFFICIENT_DATA]
ELEVATED = {"High", "Critical"}
WARNING_LEVELS = {"Medium", "High", "Critical"}

# Allowed intervention status moves. pending_approval -> approved/cancelled only via /approve.
TRANSITIONS = {
    "planned": {"initiated", "deferred", "cancelled"},
    "pending_approval": {"cancelled"},
    "approved": {"initiated", "deferred", "cancelled"},
    "initiated": {"completed", "deferred", "escalated"},
    "deferred": {"initiated", "escalated", "cancelled"},
    "escalated": {"initiated", "completed"},
    "completed": set(),
    "cancelled": set(),
}


# ── helpers ──────────────────────────────────────────────────────────────


def _actor(request: Request, db: Session, credentials, fallback: str | None) -> str:
    """The signed-in user's name when a valid token is sent, else the stated actor."""
    if credentials and credentials.credentials:
        try:
            user = authenticate_access(db, request.app.state.settings, credentials.credentials)
            return user.full_name or user.email
        except HTTPException:
            pass
    return (fallback or "").strip() or "analyst"


def _customer(db: Session, customer_id: str) -> Customer:
    cust = db.execute(select(Customer).where(Customer.customer_id == customer_id)).scalar_one_or_none()
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")
    return cust


def _log(db: Session, customer_id: str, kind: str, body: str, actor: str, case_id: str | None = None,
         intervention_id: str | None = None, meta: dict | None = None, status: str | None = None) -> CaseActivity:
    a = CaseActivity(customer_id=customer_id, case_id=case_id, intervention_id=intervention_id, kind=kind,
                     body=body, actor=actor, meta=json.dumps(meta or {}), status=status)
    db.add(a)
    return a


def _next_ref(db: Session, model, prefix: str) -> str:
    n = (db.execute(select(func.count()).select_from(model)).scalar() or 0) + 1
    year = date.today().year
    while True:
        ref = f"{prefix}-{year}-{n:04d}"
        if not db.execute(select(model.id).where(model.reference == ref)).first():
            return ref
        n += 1


def _case_out(c: InvestigationCase) -> dict:
    return {
        "id": c.id, "reference": c.reference, "status": c.status, "disposition": c.disposition,
        "owner": c.owner, "owner_role": c.owner_role, "priority_tier": c.priority_tier,
        "score_at_open": c.score_at_open, "category_at_open": c.category_at_open,
        "corrected_category": c.corrected_category, "correction_reason": c.correction_reason,
        "summary": c.summary, "created_by": c.created_by, "created_at": c.created_at,
        "updated_at": c.updated_at, "closed_at": c.closed_at,
    }


def _iv_out(i: Intervention, ext_id: str | None = None) -> dict:
    today = date.today().isoformat()
    return {
        "id": i.id, "reference": i.reference, "case_id": i.case_id, "customer_id": ext_id,
        "facility_ref": i.facility_ref, "warning_category": i.warning_category, "scenario": i.scenario,
        "scenario_label": SCENARIO_LABELS.get(i.scenario, i.scenario),
        "contributing_indicators": json.loads(i.contributing_indicators or "[]"),
        "recommended_action": i.recommended_action, "action_code": i.action_code,
        "customer_impacting": bool(i.customer_impacting), "requires_approval": bool(i.requires_approval),
        "approved_by": i.approved_by, "approved_at": i.approved_at,
        "owner": i.owner, "owner_role": i.owner_role, "priority": i.priority, "target_date": i.target_date,
        "status": i.status, "notes": i.notes, "decision": i.decision,
        "evidence": json.loads(i.evidence or "[]"), "outcome": i.outcome,
        "created_by": i.created_by, "created_at": i.created_at, "updated_at": i.updated_at,
        "completed_at": i.completed_at,
        "overdue": bool(i.target_date and i.target_date < today and i.status in OPEN_INTERVENTION_STATUSES),
    }


def _activity_out(a: CaseActivity) -> dict:
    return {
        "id": a.id, "case_id": a.case_id, "intervention_id": a.intervention_id, "kind": a.kind,
        "body": a.body, "actor": a.actor, "meta": json.loads(a.meta or "{}"), "status": a.status,
        "created_at": a.created_at,
    }


def _open_case(db: Session, cust: Customer) -> InvestigationCase | None:
    return db.execute(
        select(InvestigationCase)
        .where(InvestigationCase.customer_id == cust.id, InvestigationCase.status.in_(OPEN_CASE_STATUSES))
        .order_by(InvestigationCase.created_at.desc()).limit(1)
    ).scalar_one_or_none()


def _analysis(db: Session, cust: Customer, case: InvestigationCase | None = None) -> dict | None:
    """Tiers are portfolio-relative, so the customer is analysed within the portfolio."""
    return next((r for r in portfolio_rows(db) if r["internal_id"] == cust.id), None)


def _filters(product, segment, portfolio, period) -> Filters:
    return Filters(product=product or None, segment=segment or None, portfolio=portfolio or None, period=period or None)


# ── History ──────────────────────────────────────────────────────────────


@router.post("/monitoring/history/rebuild")
def rebuild(customer_id: str | None = Query(default=None), db: Session = Depends(get_db)):
    return rebuild_history(db, [customer_id] if customer_id else None)


@router.get("/monitoring/customer/{customer_id}/trend")
def customer_trend(customer_id: str, db: Session = Depends(get_db)):
    cust = _customer(db, customer_id)
    a = _analysis(db, cust)
    if not a:
        raise HTTPException(status_code=404, detail="No risk history for this customer; run an assessment")
    return {"customer_id": customer_id, "trend": a["trend"], "correlation": a["correlation"], "history": a["history"]}


# ── Prioritisation ───────────────────────────────────────────────────────

SORTS = {
    "priority": lambda r: -r["priority_score"],
    "warning_score": lambda r: -r["effective_score"],
    "exposure": lambda r: -r["ead"],
    "expected_loss": lambda r: -r["expected_loss"],
    "probability": lambda r: -(r["probability_of_deterioration"] or 0),
    "delinquency": lambda r: -r["delinquency"]["dpd"],
    "trend": lambda r: -(r["delta_3m"] or 0),
    "segment": lambda r: (r["segment"], -r["priority_score"]),
}


@router.get("/monitoring/priority")
def priority_queue(
    sort: str = Query(default="priority"),
    product: str | None = None,
    segment: str | None = None,
    portfolio: str | None = None,
    period: str | None = None,
    trend: str | None = Query(default=None, description="deteriorating | stable | improving"),
    delinquency: str | None = Query(default=None, description="delinquent | pre_delinquent | current"),
    level: str | None = None,
    tier: str | None = None,
    quadrant: str | None = None,
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    if sort not in SORTS:
        raise HTTPException(status_code=422, detail=f"sort must be one of: {', '.join(SORTS)}")
    items = [compact(r) for r in portfolio_rows(db, _filters(product, segment, portfolio, period))]
    if trend:
        items = [i for i in items if i["trend"] == trend]
    if delinquency == "delinquent":
        items = [i for i in items if i["delinquency"]["delinquent"]]
    elif delinquency == "pre_delinquent":
        items = [i for i in items if not i["delinquency"]["delinquent"]
                 and (i["warning_level"] in WARNING_LEVELS or i["trend"] == "deteriorating")]
    elif delinquency == "current":
        items = [i for i in items if not i["delinquency"]["delinquent"]]
    if level:
        items = [i for i in items if i["warning_level"] == level]
    if tier:
        items = [i for i in items if i["tier"] == tier]
    if quadrant:
        items = [i for i in items if i["quadrant"]["key"] == quadrant]
    items.sort(key=SORTS[sort])

    quadrants: dict[str, dict] = {}
    for i in items:
        q = quadrants.setdefault(i["quadrant"]["key"], {**i["quadrant"], "count": 0, "ead": 0.0, "expected_loss": 0.0})
        q["count"] += 1
        q["ead"] += i["ead"]
        q["expected_loss"] += i["expected_loss"]
    tiers = {t: sum(1 for i in items if i["tier"] == t) for t in ("P1", "P2", "P3", "P4")}
    return {"items": items[:limit], "total": len(items), "sort": sort, "tiers": tiers,
            "quadrants": list(quadrants.values())}


# ── Workspace ────────────────────────────────────────────────────────────


@router.get("/workspace/{customer_id}")
def workspace(customer_id: str, db: Session = Depends(get_db)):
    cust = _customer(db, customer_id)
    case = _open_case(db, cust)
    all_cases = db.execute(
        select(InvestigationCase).where(InvestigationCase.customer_id == cust.id)
        .order_by(InvestigationCase.created_at.desc())
    ).scalars().all()
    if not case and all_cases:
        case = all_cases[0]
    analysis = _analysis(db, cust, case)

    loans = db.execute(select(LoanFacility).where(LoanFacility.customer_id == cust.id)).scalars().all()
    loan_ref = {l.id: l.loan_id for l in loans}
    classifications = {
        e.repayment_id: e for e in db.execute(
            select(EventClassification).where(EventClassification.customer_id == cust.id)
        ).scalars()
    }
    repayments = []
    if loans:
        for r in db.execute(
            select(RepaymentRecord).where(RepaymentRecord.loan_id.in_(list(loan_ref)))
            .order_by(RepaymentRecord.due_date.desc())
        ).scalars():
            ec = classifications.get(r.id)
            repayments.append({
                "id": r.id, "loan_id": loan_ref[r.loan_id], "due_date": r.due_date, "payment_date": r.payment_date,
                "amount_due": r.amount_due, "amount_paid": r.amount_paid, "dpd": r.dpd,
                "repayment_status": r.repayment_status,
                "classification": {
                    "classification": ec.classification, "excluded_from_scoring": bool(ec.excluded_from_scoring),
                    "reason": ec.reason, "actor": ec.actor, "score_before": ec.score_before,
                    "score_after": ec.score_after, "created_at": ec.created_at,
                } if ec else None,
            })

    latest_assessment = db.execute(
        select(WarningAssessment).where(WarningAssessment.customer_id == cust.id)
        .order_by(WarningAssessment.assessed_at.desc()).limit(1)
    ).scalar_one_or_none()
    alerts = []
    if latest_assessment:
        alerts = [
            {"decision": a.decision, "reason": a.reason, "analyst_id": a.analyst_id, "created_at": a.created_at,
             "original_score": a.original_score, "original_category": a.original_category}
            for a in db.execute(
                select(AlertClassification).join(WarningAssessment, WarningAssessment.id == AlertClassification.assessment_id)
                .where(WarningAssessment.customer_id == cust.id).order_by(AlertClassification.created_at.desc())
            ).scalars()
        ]
    interventions = db.execute(
        select(Intervention).where(Intervention.customer_id == cust.id).order_by(Intervention.created_at.desc())
    ).scalars().all()
    activities = db.execute(
        select(CaseActivity).where(CaseActivity.customer_id == cust.id).order_by(CaseActivity.created_at.desc())
    ).scalars().all()

    return {
        "customer": {
            "customer_id": cust.customer_id, "customer_type": cust.customer_type,
            "employment_category": cust.employment_category, "portfolio": cust.portfolio_segment,
            "segment": segment_of(cust), "products": sorted(products_for(db).get(cust.id, set())),
        },
        "facilities": facilities_for(db, [cust.id]).get(cust.id, []),
        "loans": [{"loan_id": l.loan_id, "product_type": l.product_type, "sanctioned_amount": l.sanctioned_amount,
                   "outstanding_balance": l.outstanding_balance, "instalment_amount": l.instalment_amount,
                   "tenure_months": l.tenure_months, "status": l.status} for l in loans],
        "assessment": {
            "id": latest_assessment.id, "warning_score": latest_assessment.warning_score,
            "risk_category": latest_assessment.risk_category, "assessed_at": latest_assessment.assessed_at,
            "top_indicators": json.loads(latest_assessment.top_indicators or "[]"),
            "scores": {"delay": latest_assessment.rule_delay_score, "miss": latest_assessment.rule_miss_score,
                       "exposure": latest_assessment.rule_exposure_score,
                       "completeness": latest_assessment.rule_completeness_score,
                       "cashflow": latest_assessment.cashflow_score},
        } if latest_assessment else None,
        "analysis": analysis,
        "case": _case_out(case) if case else None,
        "cases": [_case_out(c) for c in all_cases],
        "interventions": [_iv_out(i, cust.customer_id) for i in interventions],
        "activities": [_activity_out(a) for a in activities],
        "info_requests": [_activity_out(a) for a in activities if a.kind == "info_request"],
        "repayments": repayments,
        "alerts": alerts,
        "playbook": {k: {**v, "label": SCENARIO_LABELS[k]} for k, v in PLAYBOOK.items()},
    }


@router.post("/workspace/{customer_id}/briefing")
def case_briefing(customer_id: str, request: Request, db: Session = Depends(get_db)):
    """LLM case briefing grounded in the computed facts (numbers verified; template fallback)."""
    from app.services.case_briefing import brief
    a = _analysis(db, _customer(db, customer_id))
    if not a:
        raise HTTPException(status_code=404, detail="No risk history for this customer; run an assessment")
    return brief(customer_id, a, request.app.state.settings)


# ── Cases ────────────────────────────────────────────────────────────────


@router.post("/cases", status_code=201)
def open_case(body: CaseCreate, request: Request, credentials: Credentials, db: Session = Depends(get_db)):
    cust = _customer(db, body.customer_id)
    existing = _open_case(db, cust)
    if existing:
        raise HTTPException(status_code=409, detail=f"Case {existing.reference} is already open for this customer")
    actor = _actor(request, db, credentials, body.actor)
    a = _analysis(db, cust)
    prio = a["priority"] if a else {}
    case = InvestigationCase(
        reference=_next_ref(db, InvestigationCase, "CASE"), customer_id=cust.id,
        owner=body.owner or actor, owner_role=body.owner_role, summary=body.summary, created_by=actor,
        priority_tier=prio.get("tier", ""), score_at_open=prio.get("effective_score", 0.0),
        category_at_open=prio.get("warning_level", ""),
    )
    db.add(case)
    db.flush()
    _log(db, cust.id, "case_opened", body.summary or f"Case opened ({case.category_at_open}, {case.priority_tier})",
         actor, case.id, meta={"owner": case.owner, "owner_role": case.owner_role})
    db.commit()
    return _case_out(case)


@router.get("/cases")
def list_cases(status: str | None = None, owner: str | None = None, db: Session = Depends(get_db)):
    q = select(InvestigationCase, Customer.customer_id).join(Customer, Customer.id == InvestigationCase.customer_id)
    if status == "open":
        q = q.where(InvestigationCase.status.in_(OPEN_CASE_STATUSES))
    elif status:
        q = q.where(InvestigationCase.status == status)
    if owner:
        q = q.where(InvestigationCase.owner == owner)
    rows = db.execute(q.order_by(InvestigationCase.updated_at.desc())).all()
    return {"items": [{**_case_out(c), "customer_id": cid} for c, cid in rows], "total": len(rows)}


def _get_case(db: Session, case_id: str) -> InvestigationCase:
    case = db.get(InvestigationCase, case_id)
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    return case


@router.patch("/cases/{case_id}")
def update_case(case_id: str, body: CaseUpdate, request: Request, credentials: Credentials,
                db: Session = Depends(get_db)):
    case = _get_case(db, case_id)
    actor = _actor(request, db, credentials, body.actor)
    changes = []
    if body.status and body.status != case.status:
        if body.status == "closed" and not (body.note or "").strip():
            raise HTTPException(status_code=422, detail="Closing a case needs a closing note")
        open_ivs = db.execute(select(func.count()).select_from(Intervention).where(
            Intervention.case_id == case.id, Intervention.status.in_(OPEN_INTERVENTION_STATUSES))).scalar() or 0
        if body.status == "closed" and open_ivs:
            raise HTTPException(status_code=409, detail=f"{open_ivs} intervention(s) are still open on this case")
        changes.append(("status_change", f"Status {case.status} → {body.status}", {"from": case.status, "to": body.status}))
        case.status = body.status
        case.closed_at = int(time.time()) if body.status == "closed" else None
    if body.disposition and body.disposition != case.disposition:
        changes.append(("disposition", f"Warning {body.disposition.replace('_', ' ')}",
                        {"from": case.disposition, "to": body.disposition}))
        case.disposition = body.disposition
    if (body.owner is not None and body.owner != case.owner) or (body.owner_role and body.owner_role != case.owner_role):
        new_owner = body.owner if body.owner is not None else case.owner
        new_role = body.owner_role or case.owner_role
        changes.append(("assignment", f"Assigned to {new_owner} ({new_role.replace('_', ' ')})",
                        {"from": [case.owner, case.owner_role], "to": [new_owner, new_role]}))
        case.owner, case.owner_role = new_owner, new_role
    if not changes and not body.note:
        raise HTTPException(status_code=422, detail="Nothing to update")
    for kind, text, meta in changes:
        _log(db, case.customer_id, kind, text + (f": {body.note}" if body.note else ""), actor, case.id, meta=meta)
    if body.note and not changes:
        _log(db, case.customer_id, "note", body.note, actor, case.id)
    case.updated_at = int(time.time())
    db.commit()
    return _case_out(case)


@router.post("/cases/{case_id}/notes", status_code=201)
def add_note(case_id: str, body: NoteCreate, request: Request, credentials: Credentials, db: Session = Depends(get_db)):
    case = _get_case(db, case_id)
    a = _log(db, case.customer_id, body.kind, body.body, _actor(request, db, credentials, body.actor), case.id)
    case.updated_at = int(time.time())
    db.commit()
    return _activity_out(a)


@router.post("/cases/{case_id}/info-requests", status_code=201)
def request_info(case_id: str, body: InfoRequestCreate, request: Request, credentials: Credentials,
                 db: Session = Depends(get_db)):
    case = _get_case(db, case_id)
    actor = _actor(request, db, credentials, body.actor)
    a = _log(db, case.customer_id, "info_request",
             body.message or "Requested: " + "; ".join(body.items), actor, case.id,
             meta={"items": body.items, "requested_from": body.requested_from, "due_date": body.due_date},
             status="open")
    if case.status in ("open", "under_investigation"):
        _log(db, case.customer_id, "status_change", f"Status {case.status} → awaiting_information", actor, case.id,
             meta={"from": case.status, "to": "awaiting_information"})
        case.status = "awaiting_information"
    case.updated_at = int(time.time())
    db.commit()
    return _activity_out(a)


@router.patch("/info-requests/{activity_id}")
def update_info_request(activity_id: str, body: InfoRequestUpdate, request: Request, credentials: Credentials,
                        db: Session = Depends(get_db)):
    a = db.get(CaseActivity, activity_id)
    if not a or a.kind != "info_request":
        raise HTTPException(status_code=404, detail="Information request not found")
    actor = _actor(request, db, credentials, body.actor)
    meta = json.loads(a.meta or "{}")
    meta.update({"response": body.response, "resolved_by": actor, "resolved_at": int(time.time())})
    a.meta, a.status = json.dumps(meta), body.status
    _log(db, a.customer_id, "info_received" if body.status == "fulfilled" else "note",
         body.response or f"Information request {body.status}", actor, a.case_id, meta={"request_id": a.id})
    db.commit()
    return _activity_out(a)


@router.post("/cases/{case_id}/classification")
def correct_category(case_id: str, body: CategoryCorrection, request: Request, credentials: Credentials,
                     db: Session = Depends(get_db)):
    """Analyst correction of the warning level. The model score and its evidence stay
    on record; the corrected level is what the queue and dashboard show while the
    case is open."""
    case = _get_case(db, case_id)
    actor = _actor(request, db, credentials, body.actor)
    before = case.corrected_category
    case.corrected_category, case.correction_reason = body.corrected_category, body.reason
    case.updated_at = int(time.time())
    _log(db, case.customer_id, "classification",
         (f"Warning level corrected to {body.corrected_category}" if body.corrected_category
          else "Warning level correction removed") + f": {body.reason}",
         actor, case.id, meta={"from": before or case.category_at_open, "to": body.corrected_category,
                               "model_category_at_open": case.category_at_open})
    db.commit()
    return _case_out(case)


# ── Repayment event classification (+ recalculation) ─────────────────────


def _recalculate(db: Session, cust: Customer) -> float | None:
    from app.api.v1.ews import run_assessment
    result = run_assessment(customer_id=cust.customer_id, db=db)
    rows = result.get("assessments") or []
    return rows[0]["warning_score"] if rows else None


def _latest_score(db: Session, cust: Customer) -> float | None:
    return db.execute(
        select(WarningAssessment.warning_score).where(WarningAssessment.customer_id == cust.id)
        .order_by(WarningAssessment.assessed_at.desc()).limit(1)
    ).scalar_one_or_none()


@router.post("/repayments/{repayment_id}/classification")
def classify_event(repayment_id: str, body: EventClassificationCreate, request: Request, credentials: Credentials,
                   db: Session = Depends(get_db)):
    rec = db.get(RepaymentRecord, repayment_id)
    if not rec:
        raise HTTPException(status_code=404, detail="Repayment record not found")
    loan = db.get(LoanFacility, rec.loan_id)
    cust = db.get(Customer, loan.customer_id)
    actor = _actor(request, db, credentials, body.actor)
    before = _latest_score(db, cust)

    ec = db.execute(select(EventClassification).where(EventClassification.repayment_id == rec.id)).scalar_one_or_none()
    if not ec:
        ec = EventClassification(repayment_id=rec.id, customer_id=cust.id)
        db.add(ec)
    ec.classification = body.classification
    ec.excluded_from_scoring = int(body.classification in EXCLUDING_CLASSIFICATIONS)
    ec.reason, ec.actor, ec.score_before = body.reason, actor, before
    ec.original_values = json.dumps({
        "payment_date": rec.payment_date, "amount_paid": rec.amount_paid, "dpd": rec.dpd,
        "repayment_status": rec.repayment_status,
    })
    db.commit()

    after = _recalculate(db, cust)
    ec.score_after = after
    case = db.get(InvestigationCase, body.case_id) if body.case_id else _open_case(db, cust)
    label = body.classification.replace("_", " ")
    _log(db, cust.id, "event_classification",
         f"Instalment {loan.loan_id} due {rec.due_date} ({rec.dpd} DPD, {rec.repayment_status}) classified as "
         f"{label}: {body.reason}", actor, case.id if case else None,
         meta={"repayment_id": rec.id, "classification": body.classification,
               "excluded_from_scoring": bool(ec.excluded_from_scoring)})
    _log(db, cust.id, "recalculation",
         f"Risk reassessed after event classification: {before if before is not None else '–'} → "
         f"{after if after is not None else '–'}", actor, case.id if case else None,
         meta={"score_before": before, "score_after": after})
    db.commit()
    return {"repayment_id": rec.id, "classification": body.classification,
            "excluded_from_scoring": bool(ec.excluded_from_scoring), "score_before": before, "score_after": after}


@router.delete("/repayments/{repayment_id}/classification")
def remove_event_classification(repayment_id: str, request: Request, credentials: Credentials,
                                actor: str | None = None, db: Session = Depends(get_db)):
    ec = db.execute(select(EventClassification).where(EventClassification.repayment_id == repayment_id)).scalar_one_or_none()
    if not ec:
        raise HTTPException(status_code=404, detail="No classification on this event")
    cust = db.get(Customer, ec.customer_id)
    who = _actor(request, db, credentials, actor)
    before = _latest_score(db, cust)
    db.delete(ec)
    db.commit()
    after = _recalculate(db, cust)
    case = _open_case(db, cust)
    _log(db, cust.id, "recalculation", f"Event classification removed; risk reassessed: {before} → {after}", who,
         case.id if case else None, meta={"repayment_id": repayment_id, "score_before": before, "score_after": after})
    db.commit()
    return {"repayment_id": repayment_id, "score_before": before, "score_after": after}


# ── Interventions ────────────────────────────────────────────────────────


@router.post("/interventions", status_code=201)
def create_intervention(body: InterventionCreate, request: Request, credentials: Credentials,
                        db: Session = Depends(get_db)):
    cust = _customer(db, body.customer_id)
    actor = _actor(request, db, credentials, body.actor)
    case = db.get(InvestigationCase, body.case_id) if body.case_id else _open_case(db, cust)
    analysis = _analysis(db, cust, case)
    rec = next((r for r in (analysis or {}).get("recommendations", []) if r["scenario"] == body.scenario), None)
    play = PLAYBOOK[body.scenario]
    prio = (analysis or {}).get("priority", {})
    if not case:
        case = InvestigationCase(
            reference=_next_ref(db, InvestigationCase, "CASE"), customer_id=cust.id, owner=body.owner or actor,
            owner_role=body.owner_role or play["owner_role"], created_by=actor, status="action_in_progress",
            priority_tier=prio.get("tier", ""), score_at_open=prio.get("effective_score", 0.0),
            category_at_open=prio.get("warning_level", ""), summary="Opened when an intervention was accepted",
        )
        db.add(case)
        db.flush()
        _log(db, cust.id, "case_opened", case.summary, actor, case.id)
    # Accepting an intervention means the warning was accepted as requiring action.
    if case.disposition != "accepted":
        _log(db, cust.id, "disposition", "Warning accepted as requiring action", actor, case.id,
             meta={"from": case.disposition, "to": "accepted"})
        case.disposition = "accepted"
    if case.status in ("open", "under_investigation"):
        case.status = "action_in_progress"

    indicators = list(rec["evidence"]) if rec else ["Analyst-initiated: not among the current recommendations"]
    corr = (analysis or {}).get("correlation", {})
    if corr.get("co_moving"):
        indicators.append("Correlated signals: " + ", ".join(corr["co_moving"]))
    priority = body.priority or (rec["priority"] if rec else prio.get("tier", "P3"))
    target = body.target_date or (date.today() + timedelta(days=play["target_days"])).isoformat()
    loans = db.execute(select(LoanFacility.loan_id).where(LoanFacility.customer_id == cust.id)
                       .order_by(LoanFacility.outstanding_balance.desc())).scalars().all()
    iv = Intervention(
        reference=_next_ref(db, Intervention, "INT"), case_id=case.id, customer_id=cust.id,
        facility_ref=body.facility_ref or (loans[0] if loans else ""),
        warning_category=prio.get("warning_level", ""), scenario=body.scenario,
        contributing_indicators=json.dumps(indicators), recommended_action=play["action"], action_code=body.scenario,
        customer_impacting=int(play["customer_impacting"]), requires_approval=int(play["requires_approval"]),
        owner=body.owner or actor, owner_role=body.owner_role or play["owner_role"], priority=priority,
        target_date=target, status="pending_approval" if play["requires_approval"] else "planned",
        notes=body.notes, evidence=json.dumps([rec["rationale"]] if rec else []), created_by=actor,
    )
    db.add(iv)
    db.flush()
    _log(db, cust.id, "intervention_created",
         f"{iv.reference}: {play['action']} ({iv.status.replace('_', ' ')}, due {target})", actor, case.id, iv.id,
         meta={"scenario": body.scenario, "requires_approval": bool(iv.requires_approval)})
    case.updated_at = int(time.time())
    db.commit()
    return _iv_out(iv, cust.customer_id)


@router.get("/interventions")
def list_interventions(
    status: str | None = Query(default=None, description="a status, or 'open'"),
    owner: str | None = None,
    customer_id: str | None = None,
    overdue: bool = False,
    db: Session = Depends(get_db),
):
    q = select(Intervention, Customer.customer_id).join(Customer, Customer.id == Intervention.customer_id)
    if status == "open":
        q = q.where(Intervention.status.in_(OPEN_INTERVENTION_STATUSES))
    elif status:
        q = q.where(Intervention.status == status)
    if owner:
        q = q.where(Intervention.owner == owner)
    if customer_id:
        q = q.where(Customer.customer_id == customer_id)
    items = [_iv_out(i, cid) for i, cid in db.execute(q.order_by(Intervention.target_date)).all()]
    if overdue:
        items = [i for i in items if i["overdue"]]
    summary = {}
    for i in items:
        summary[i["status"]] = summary.get(i["status"], 0) + 1
    return {"items": items, "total": len(items), "by_status": summary,
            "overdue": sum(1 for i in items if i["overdue"])}


def _get_iv(db: Session, iv_id: str) -> Intervention:
    iv = db.get(Intervention, iv_id)
    if not iv:
        raise HTTPException(status_code=404, detail="Intervention not found")
    return iv


@router.patch("/interventions/{iv_id}")
def update_intervention(iv_id: str, body: InterventionUpdate, request: Request, credentials: Credentials,
                        db: Session = Depends(get_db)):
    iv = _get_iv(db, iv_id)
    actor = _actor(request, db, credentials, body.actor)
    logs = []
    if body.status and body.status != iv.status:
        if body.status not in TRANSITIONS[iv.status]:
            hint = " Customer-impacting actions need approval first (POST /approve)." if iv.status == "pending_approval" else ""
            raise HTTPException(status_code=409, detail=f"Cannot move from {iv.status} to {body.status}.{hint}")
        if body.status == "completed" and not (body.outcome or iv.outcome).strip():
            raise HTTPException(status_code=422, detail="Record the outcome to complete an intervention")
        if body.status in ("deferred", "cancelled", "escalated") and not (body.decision or body.notes or "").strip():
            raise HTTPException(status_code=422, detail=f"Give a reason (decision or notes) to mark it {body.status}")
        logs.append(("intervention_status", f"{iv.reference}: {iv.status} → {body.status}",
                     {"from": iv.status, "to": body.status}))
        iv.status = body.status
        iv.completed_at = int(time.time()) if body.status == "completed" else None
    for fld in ("owner", "owner_role", "priority", "target_date"):
        v = getattr(body, fld)
        if v is not None and v != getattr(iv, fld):
            logs.append(("intervention_update", f"{iv.reference}: {fld.replace('_', ' ')} {getattr(iv, fld) or '–'} → {v}",
                         {"field": fld, "from": getattr(iv, fld), "to": v}))
            setattr(iv, fld, v)
    for fld, kind in (("notes", "intervention_note"), ("decision", "intervention_decision"),
                      ("outcome", "intervention_outcome")):
        v = getattr(body, fld)
        if v is not None and v.strip() and v != getattr(iv, fld):
            setattr(iv, fld, v)
            logs.append((kind, f"{iv.reference} {fld}: {v}", {}))
    if body.evidence and body.evidence.strip():
        ev = json.loads(iv.evidence or "[]")
        ev.append(body.evidence.strip())
        iv.evidence = json.dumps(ev)
        logs.append(("intervention_evidence", f"{iv.reference} evidence added: {body.evidence.strip()}", {}))
    if not logs:
        raise HTTPException(status_code=422, detail="Nothing to update")
    for kind, text, meta in logs:
        _log(db, iv.customer_id, kind, text, actor, iv.case_id, iv.id, meta=meta)
    iv.updated_at = int(time.time())
    db.commit()
    return _iv_out(iv, db.get(Customer, iv.customer_id).customer_id)


@router.post("/interventions/{iv_id}/approve")
def approve_intervention(iv_id: str, body: ApprovalDecision, request: Request, credentials: Credentials,
                         db: Session = Depends(get_db)):
    """Authorised approval for customer-impacting actions: needs a signed-in user who
    is not the person who proposed the intervention (four-eyes)."""
    if not credentials or not credentials.credentials:
        raise HTTPException(status_code=401, detail="Sign in to approve customer-impacting actions")
    user = authenticate_access(db, request.app.state.settings, credentials.credentials)
    approver = user.full_name or user.email
    iv = _get_iv(db, iv_id)
    if iv.status != "pending_approval":
        raise HTTPException(status_code=409, detail=f"Intervention is {iv.status}, not awaiting approval")
    if approver.strip().lower() in {iv.created_by.strip().lower(), user.email.lower()}:
        raise HTTPException(status_code=403, detail="The proposer cannot approve their own intervention")
    iv.status = "approved" if body.approve else "cancelled"
    iv.approved_by, iv.approved_at = approver, int(time.time())
    iv.decision = f"{'Approved' if body.approve else 'Rejected'} by {approver}: {body.comment}"
    iv.updated_at = int(time.time())
    _log(db, iv.customer_id, "approval", f"{iv.reference} {'approved' if body.approve else 'rejected'}: {body.comment}",
         approver, iv.case_id, iv.id, meta={"approved": body.approve})
    db.commit()
    return _iv_out(iv, db.get(Customer, iv.customer_id).customer_id)


# ── Portfolio dashboard ──────────────────────────────────────────────────


@router.get("/monitoring/dashboard")
def dashboard(
    product: str | None = None,
    segment: str | None = None,
    portfolio: str | None = None,
    period: str | None = Query(default=None, description="As-of month YYYY-MM (default: latest)"),
    window: int = Query(default=6, ge=2, le=12, description="Months shown in the trend charts"),
    db: Session = Depends(get_db),
):
    customers = db.execute(select(Customer)).scalars().all()
    products = products_for(db)
    all_periods = sorted({p for (p,) in db.execute(select(func.distinct(RiskSnapshot.period))).all()})
    options = {
        "products": sorted({p for s in products.values() for p in s}),
        "segments": sorted({segment_of(c) for c in customers}),
        "portfolios": sorted({c.portfolio_segment for c in customers if c.portfolio_segment}),
        "periods": all_periods,
    }
    filters = _filters(product, segment, portfolio, period)
    rows = portfolio_rows(db, filters)
    as_of = period or (all_periods[-1] if all_periods else None)
    if not rows:
        return {"as_of": as_of, "filters": options, "applied": filters.__dict__, "customers": 0,
                "message": "No risk history for this selection. Run an assessment to build it."}

    items = [compact(r) for r in rows]
    delinquent = [i for i in items if i["delinquency"]["delinquent"]]
    early = [i for i in items if not i["delinquency"]["delinquent"]
             and (i["warning_level"] in WARNING_LEVELS or i["trend"] == "deteriorating")]
    early_ids = {i["customer_id"] for i in early}
    elevated = [i for i in items if i["warning_level"] in ELEVATED]

    def total(xs, key="outstanding"):
        return round(sum(x[key] for x in xs), 2)

    by_level = []
    for lvl in LEVELS:
        xs = [i for i in items if i["warning_level"] == lvl]
        by_level.append({
            "level": lvl, "customers": len(xs), "exposure": total(xs), "expected_loss": total(xs, "expected_loss"),
            "delinquent": sum(1 for x in xs if x["delinquency"]["delinquent"]),
            "early_warning": sum(1 for x in xs if x["customer_id"] in early_ids),
        })

    # Trend series over the window, from each customer's snapshots.
    periods = [p for p in all_periods if not as_of or p <= as_of][-window:]
    series = []
    for p in periods:
        point = {"period": p, "customers": 0, "avg_score": None, "elevated": 0, "elevated_exposure": 0.0,
                 "deteriorating": 0, "buckets": {"Standard": 0, "SMA-0": 0, "SMA-1": 0, "SMA-2": 0, "NPA": 0}}
        scores = []
        for r in rows:
            hist = [h for h in r["history"] if h["period"] <= p]
            if not hist or hist[-1]["period"] != p:
                continue
            snap = hist[-1]
            point["customers"] += 1
            point["buckets"][delinquency_status(snap["metrics"])["bucket"]] += 1
            if snap["risk_category"] != INSUFFICIENT_DATA:
                scores.append(snap["warning_score"])
            if snap["risk_category"] in ELEVATED:
                point["elevated"] += 1
                point["elevated_exposure"] += snap["metrics"].get("total_outstanding") or 0
            if analyse_trend(hist)["direction"] == "deteriorating":
                point["deteriorating"] += 1
        point["avg_score"] = round(sum(scores) / len(scores), 2) if scores else None
        point["elevated_exposure"] = round(point["elevated_exposure"], 2)
        point["delinquent"] = point["customers"] - point["buckets"]["Standard"]
        series.append(point)

    # Newly delinquent: current at the start of the window, past due now.
    newly = []
    if len(periods) >= 2:
        for r in rows:
            start = [h for h in r["history"] if h["period"] <= periods[0]]
            if start and not delinquency_status(start[-1]["metrics"])["delinquent"] \
                    and r["priority"]["delinquency"]["delinquent"]:
                newly.append(r["customer_id"])

    worsening = sorted([i for i in items if i["trend"] == "deteriorating"], key=lambda i: -(i["delta_3m"] or 0))
    ivs = list_interventions(status="open", db=db)["items"]
    selected = {i["customer_id"] for i in items}
    ivs = [i for i in ivs if i["customer_id"] in selected]

    breakdown = {}
    for dim in ("portfolio", "segment"):
        groups: dict[str, dict] = {}
        for i in items:
            g = groups.setdefault(i[dim], {"name": i[dim], "customers": 0, "elevated": 0, "delinquent": 0,
                                           "early_warning": 0, "exposure": 0.0, "elevated_exposure": 0.0})
            g["customers"] += 1
            g["exposure"] += i["outstanding"]
            if i["warning_level"] in ELEVATED:
                g["elevated"] += 1
                g["elevated_exposure"] += i["outstanding"]
            g["delinquent"] += int(i["delinquency"]["delinquent"])
            g["early_warning"] += int(i["customer_id"] in early_ids)
        breakdown[dim] = sorted(groups.values(), key=lambda g: -g["exposure"])
    by_product: dict[str, dict] = {}
    for i in items:
        for p in i["products"]:
            g = by_product.setdefault(p, {"name": p, "customers": 0, "elevated": 0, "delinquent": 0, "early_warning": 0})
            g["customers"] += 1
            g["elevated"] += int(i["warning_level"] in ELEVATED)
            g["delinquent"] += int(i["delinquency"]["delinquent"])
            g["early_warning"] += int(i["customer_id"] in early_ids)
    breakdown["product"] = sorted(by_product.values(), key=lambda g: -g["customers"])

    return {
        "as_of": as_of,
        "filters": options,
        "applied": filters.__dict__ | {"window": window},
        "customers": len(items),
        "kpis": {
            "total_exposure": total(items),
            "elevated_customers": len(elevated),
            "elevated_exposure": total(elevated),
            "delinquent_customers": len(delinquent),
            "delinquent_exposure": total(delinquent),
            "early_warning_customers": len(early),
            "early_warning_exposure": total(early),
            "deteriorating_customers": len(worsening),
            "improving_customers": sum(1 for i in items if i["trend"] == "improving"),
            "expected_loss": total(items, "expected_loss"),
            "open_interventions": len(ivs),
            "overdue_interventions": sum(1 for i in ivs if i["overdue"]),
            "pending_approval": sum(1 for i in ivs if i["status"] == "pending_approval"),
            "open_cases": sum(1 for i in items if i["case"] and i["case"]["status"] in OPEN_CASE_STATUSES),
            "correlated_escalations": sum(1 for i in items if i["correlation_uplift"] > 0),
        },
        "by_level": by_level,
        "delinquency_split": {
            "delinquent": {"customers": len(delinquent), "exposure": total(delinquent),
                           "items": sorted(delinquent, key=lambda i: -i["priority_score"])[:15]},
            "early_warning": {"customers": len(early), "exposure": total(early),
                              "items": sorted(early, key=lambda i: -i["priority_score"])[:15]},
        },
        "trend_series": series,
        "newly_delinquent": newly,
        "worsening": worsening[:10],
        "interventions": sorted(ivs, key=lambda i: (not i["overdue"], i["target_date"]))[:15],
        "breakdown": breakdown,
    }
