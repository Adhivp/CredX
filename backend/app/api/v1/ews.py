"""EWS API: data ingestion, deterministic assessment, watchlist, customer detail, alerts,
explanation and portfolio summary.

All routes require authentication, a role permission and data scope. Scores come from
the same deterministic engine the investigation Adjudicator uses (``app.intelligence``);
the repository's ML models are not used because they are not validated.
"""

from __future__ import annotations

import io
import json
import logging

import pandas as pd
from fastapi import APIRouter, File, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.agents.budget import Budget
from app.agents.graph import start_run
from app.agents.llm import LLMGateway, unverified_numbers
from app.api.dependencies import CurrentUser, Database
from app.core.rbac import Permission
from app.intelligence.data import load_customer_snapshot
from app.intelligence.legacy import core_rule_scores
from app.intelligence.scoring import evaluate_snapshot
from app.models.ews import (
    AlertClassification,
    Customer,
    DataUpload,
    LoanFacility,
    RepaymentRecord,
    WarningAssessment,
)
from app.models.investigation import Investigation, LLMCall
from app.schemas.ews import (
    AlertCreate,
    AlertOut,
    CustomerDetailOut,
    CustomerOut,
    DataUploadResponse,
    ExplainResponse,
    LoanFacilityOut,
    RepaymentRecordOut,
    WarningAssessmentOut,
    WatchlistEntry,
    WatchlistFilters,
    WatchlistResponse,
)
from app.services import access, audit, monitoring
from app.services.ingestion import REQUIRED_COLUMNS, ingest
from app.services.policy import BudgetPolicy, get_active_policy

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ews", tags=["ews"])
P = Permission
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
CATEGORY = {
    "low": "Low",
    "moderate": "Medium",
    "high": "High",
    "severe": "Critical",
    "undetermined": "Insufficient Data",
}


# ── Upload ───────────────────────────────────────────────────────────────


def _parse_file(file: UploadFile) -> pd.DataFrame:
    content = file.file.read(MAX_UPLOAD_BYTES + 1)
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "File exceeds the 10 MB upload limit")
    name = (file.filename or "").lower()
    try:
        if name.endswith(".csv"):
            return pd.read_csv(io.BytesIO(content))
        if name.endswith((".xlsx", ".xls")):
            return pd.read_excel(io.BytesIO(content))
        if name.endswith(".json"):
            return pd.read_json(io.BytesIO(content))
    except ValueError, pd.errors.ParserError:
        raise HTTPException(400, "File could not be parsed") from None
    raise HTTPException(400, "Unsupported file format. Use CSV, Excel, or JSON.")


@router.post("/upload", response_model=DataUploadResponse)
def upload_data(
    request: Request,
    db: Database,
    user: CurrentUser,
    data_type: str = Query(
        ..., description="One of: customers, loans, repayments, exposure, cashflow"
    ),
    file: UploadFile = File(...),  # noqa: B008
):
    access.require_permission(db, user, P.UPLOAD_DATA, "upload")
    df = _parse_file(file)
    required = REQUIRED_COLUMNS.get(data_type)
    errors = []
    if required is None:
        errors = [f"Unknown data_type '{data_type}'. Use: {', '.join(REQUIRED_COLUMNS)}"]
    elif missing := required - set(df.columns):
        errors = [f"Missing required columns: {', '.join(sorted(missing))}"]
    upload = DataUpload(
        filename=(file.filename or "unknown")[:255],
        file_type=data_type[:32],
        record_count=len(df),
        status="error" if errors else "success",
        errors=json.dumps(errors) if errors else None,
    )
    db.add(upload)
    if errors:
        db.commit()
        return DataUploadResponse(
            upload_id=upload.id,
            filename=upload.filename,
            records_loaded=0,
            status="error",
            errors=errors,
        )
    rows = df.astype(object).where(pd.notna(df), None).to_dict(orient="records")
    result = ingest(db, data_type, rows, access.user_scope(db, user))
    upload.record_count = result.loaded
    upload.status = "success" if not result.skipped else "partial"
    upload.errors = json.dumps(result.skipped[:200]) if result.skipped else None
    audit.record(
        db,
        user,
        "upload_data",
        "allowed",
        "upload",
        upload.id,
        data_type=data_type,
        loaded=result.loaded,
        skipped=len(result.skipped),
    )
    reopened = []
    if data_type in {"repayments", "cashflow", "exposure"} and result.customer_ids:
        pks = set(
            db.scalars(select(Customer.id).where(Customer.customer_id.in_(result.customer_ids)))
        )
        reopened = monitoring.evaluate_customers(db, pks, f"new_{data_type}_data")
    db.commit()
    for investigation_id in reopened:
        start_run(request.app.state, investigation_id, "monitoring_reopen")
    return DataUploadResponse(
        upload_id=upload.id,
        filename=upload.filename,
        records_loaded=result.loaded,
        status=upload.status,
        errors=result.skipped[:200],
    )


# ── Assessment ───────────────────────────────────────────────────────────


@router.post("/assess")
def run_assessment(
    request: Request,
    db: Database,
    user: CurrentUser,
    customer_id: str | None = Query(default=None, max_length=64),
):
    access.require_permission(db, user, P.RUN_ASSESSMENT, "assessment")
    if customer_id:
        customers = [access.get_customer(db, user, customer_id, P.RUN_ASSESSMENT)]
    else:
        customers = db.scalars(
            select(Customer).where(access.customer_filter(access.user_scope(db, user)))
        ).all()
    if not customers:
        raise HTTPException(404, "No customers found")
    _, policy = get_active_policy(db, request.app.state.settings.institution_id)
    assessments = []
    for customer in customers:
        snapshot = load_customer_snapshot(db, customer.id)
        a = evaluate_snapshot(snapshot, policy).assessment
        rules = core_rule_scores(snapshot).output
        indicators = list(rules.get("delay_indicators", []))[:3]
        indicators.append(f"Classification: {a.classification.replace('_', ' ')}")
        indicators.extend(r for r in a.rationale if "family score" not in r)
        row = WarningAssessment(
            customer_id=customer.id,
            warning_score=a.risk_score or 0.0,
            risk_category=CATEGORY[a.severity],
            rule_delay_score=round(rules.get("delay_score") or 0.0, 2),
            rule_miss_score=round(rules.get("miss_score") or 0.0, 2),
            rule_exposure_score=a.family_scores.get("exposure", 0.0),
            rule_completeness_score=round(rules.get("completeness_score") or 0.0, 2),
            cashflow_score=a.family_scores.get("cashflow", 0.0),
            top_indicators=json.dumps(indicators[:7]),
        )
        db.add(row)
        assessments.append(
            {
                "customer_id": customer.customer_id,
                "warning_score": row.warning_score,
                "risk_category": row.risk_category,
                "classification": a.classification,
                "confidence": a.confidence,
                "rule_delay_score": row.rule_delay_score,
                "rule_miss_score": row.rule_miss_score,
                "rule_exposure_score": row.rule_exposure_score,
                "rule_completeness_score": row.rule_completeness_score,
                "cashflow_score": row.cashflow_score,
                "top_indicators": indicators[:7],
            }
        )
    audit.record(
        db,
        user,
        "run_assessment",
        "allowed",
        "assessment",
        customer_id or "scope",
        count=len(assessments),
    )
    db.commit()
    return {
        "total_assessed": len(assessments),
        "scoring_version": "1.0",
        "assessments": assessments,
    }


def _latest_assessments(db, user):
    latest = (
        select(WarningAssessment.customer_id, func.max(WarningAssessment.assessed_at).label("at"))
        .group_by(WarningAssessment.customer_id)
        .subquery()
    )
    return (
        select(WarningAssessment, Customer)
        .join(
            latest,
            (WarningAssessment.customer_id == latest.c.customer_id)
            & (WarningAssessment.assessed_at == latest.c.at),
        )
        .join(Customer, Customer.id == WarningAssessment.customer_id)
        .where(access.customer_filter(access.user_scope(db, user)))
    )


def _dedupe(rows):
    """Several assessments can share the latest second; keep one per customer."""
    seen, out = set(), []
    for wa, customer in rows:
        if customer.id not in seen:
            seen.add(customer.id)
            out.append((wa, customer))
    return out


# ── Watchlist ────────────────────────────────────────────────────────────


@router.get("/watchlist", response_model=WatchlistResponse)
def get_watchlist(
    db: Database,
    user: CurrentUser,
    risk_level: str | None = Query(default=None, max_length=32),
    product_type: str | None = Query(default=None, max_length=64),
    customer_segment: str | None = Query(default=None, max_length=64),
    min_score: float = Query(default=0.0),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
):
    access.require_permission(db, user, P.VIEW_CUSTOMER, "watchlist")
    query = _latest_assessments(db, user).where(WarningAssessment.warning_score >= min_score)
    if risk_level:
        query = query.where(WarningAssessment.risk_category == risk_level)
    if customer_segment:
        query = query.where(Customer.portfolio_segment == customer_segment)
    if product_type:
        query = query.where(
            Customer.id.in_(
                select(LoanFacility.customer_id).where(LoanFacility.product_type == product_type)
            )
        )
    rows = _dedupe(db.execute(query.order_by(WarningAssessment.warning_score.desc())).all())
    status_by_customer = dict(
        db.execute(
            select(Investigation.customer_id, Investigation.status)
            .where(Investigation.customer_id.in_([c.id for _, c in rows]))
            .order_by(Investigation.updated_at)
        ).all()
    )
    items = []
    for wa, customer in rows[offset : offset + limit]:
        loan = db.scalar(
            select(LoanFacility)
            .where(LoanFacility.customer_id == customer.id)
            .order_by(LoanFacility.outstanding_balance.desc())
            .limit(1)
        )
        items.append(
            WatchlistEntry(
                customer_id=customer.customer_id,
                product_type=loan.product_type if loan else "",
                outstanding_exposure=loan.outstanding_balance if loan else 0.0,
                warning_score=wa.warning_score,
                risk_category=wa.risk_category,
                top_indicators=json.loads(wa.top_indicators or "[]")[:3],
                active_signals=sum(
                    1
                    for s in (
                        wa.rule_delay_score,
                        wa.rule_miss_score,
                        wa.rule_exposure_score,
                        wa.cashflow_score,
                    )
                    if s > 20
                ),
                monitoring_status=status_by_customer.get(customer.id, "not_investigated"),
            )
        )
    return WatchlistResponse(
        items=items,
        total=len(rows),
        filters=WatchlistFilters(
            product_type=product_type,
            customer_segment=customer_segment,
            risk_level=risk_level,
            min_score=min_score,
            max_results=limit,
        ),
    )


# ── Customer detail ──────────────────────────────────────────────────────


@router.get("/customer/{customer_id}", response_model=CustomerDetailOut)
def get_customer_detail(customer_id: str, db: Database, user: CurrentUser):
    customer = access.get_customer(db, user, customer_id, P.VIEW_CUSTOMER)
    loans = db.scalars(select(LoanFacility).where(LoanFacility.customer_id == customer.id)).all()
    repayments = (
        db.scalars(
            select(RepaymentRecord)
            .where(RepaymentRecord.loan_id.in_([loan.id for loan in loans]))
            .order_by(RepaymentRecord.due_date)
        ).all()
        if loans
        else []
    )
    assessments = db.scalars(
        select(WarningAssessment)
        .where(WarningAssessment.customer_id == customer.id)
        .order_by(WarningAssessment.assessed_at.desc())
    ).all()
    alerts = (
        db.scalars(
            select(AlertClassification)
            .where(AlertClassification.assessment_id.in_([a.id for a in assessments]))
            .order_by(AlertClassification.created_at.desc())
        ).all()
        if assessments
        else []
    )
    return CustomerDetailOut(
        customer=CustomerOut.model_validate(customer),
        loans=[LoanFacilityOut.model_validate(loan) for loan in loans],
        repayments=[RepaymentRecordOut.model_validate(r) for r in repayments],
        assessments=[WarningAssessmentOut.model_validate(a) for a in assessments],
        alerts=[AlertOut.model_validate(a) for a in alerts],
    )


# ── Alert classification ─────────────────────────────────────────────────


@router.post("/alert/{assessment_id}", response_model=AlertOut)
def classify_alert(assessment_id: str, body: AlertCreate, db: Database, user: CurrentUser):
    access.require_permission(db, user, P.COMPLETE_ANALYST_REVIEW, "assessment", assessment_id)
    assessment = db.get(WarningAssessment, assessment_id)
    customer = db.get(Customer, assessment.customer_id) if assessment else None
    if customer is None:
        raise HTTPException(404, "Assessment not found")
    access.get_customer(db, user, customer.customer_id, P.COMPLETE_ANALYST_REVIEW)
    alert = AlertClassification(
        assessment_id=assessment.id,
        decision=body.decision,
        reason=body.reason,
        # The analyst is the authenticated caller, never a client-supplied value.
        analyst_id=user.id,
        original_score=assessment.warning_score,
        original_category=assessment.risk_category,
        preserved_evidence=json.dumps(
            {
                "top_indicators": json.loads(assessment.top_indicators or "[]"),
                "rule_scores": {
                    "delay": assessment.rule_delay_score,
                    "miss": assessment.rule_miss_score,
                    "exposure": assessment.rule_exposure_score,
                    "completeness": assessment.rule_completeness_score,
                    "cashflow": assessment.cashflow_score,
                },
            }
        ),
    )
    db.add(alert)
    audit.record(
        db, user, "classify_alert", "allowed", "assessment", assessment.id, decision=body.decision
    )
    db.commit()
    return AlertOut.model_validate(alert)


# ── Explanation ──────────────────────────────────────────────────────────


class _Explanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    explanation: str = Field(max_length=900)


@router.post("/explain/{customer_id}", response_model=ExplainResponse)
def explain_customer(customer_id: str, request: Request, db: Database, user: CurrentUser):
    customer = access.get_customer(db, user, customer_id, P.VIEW_CUSTOMER)
    settings = request.app.state.settings
    _, policy = get_active_policy(db, settings.institution_id)
    a = evaluate_snapshot(load_customer_snapshot(db, customer.id), policy).assessment
    facts = {
        "classification": a.classification,
        "severity": a.severity,
        "risk_score": a.risk_score,
        "confidence": a.confidence,
        "family_scores": a.family_scores,
        "rationale": a.rationale,
        "recommendation": a.recommendation,
    }
    deterministic = (
        f"Deterministic assessment: {a.classification.replace('_', ' ')} "
        f"(severity {a.severity}, confidence {a.confidence:.2f}). " + " ".join(a.rationale)
    )
    gateway = LLMGateway(
        settings,
        Budget(BudgetPolicy(max_llm_calls=2)),
        client=getattr(request.app.state, "llm_client", None),
    )
    # The customer is referred to by a pseudonym; no identifiers leave the service.
    result = gateway.complete_json(
        agent_id="explainer",
        purpose="explain_assessment",
        instructions="Explain this deterministic credit assessment for a relationship manager "
        "in at most four sentences. Do not add figures.",
        data={"subject": "SUBJECT", **facts},
        schema=_Explanation,
    )
    for r in gateway.drain()[0]:
        db.add(
            LLMCall(
                agent_id=r.agent_id,
                purpose=r.purpose,
                model=r.model,
                status=r.status,
                prompt_tokens=r.prompt_tokens,
                completion_tokens=r.completion_tokens,
                cost_usd=r.cost_usd,
                latency_ms=r.latency_ms,
                error=r.error,
            )
        )
    db.commit()
    if result is not None and not unverified_numbers(result.explanation, facts):
        explanation = f"{result.explanation} (AI explanation; figures verified.)"
    else:
        explanation = deterministic + " (AI explanation unavailable.)"
    return ExplainResponse(
        customer_id=customer.customer_id,
        explanation=explanation,
        contributing_factors=[{"signal": k, "score": v} for k, v in a.family_scores.items()],
    )


# ── Portfolio summary ────────────────────────────────────────────────────


@router.get("/portfolio")
def portfolio_summary(db: Database, user: CurrentUser):
    access.require_permission(db, user, P.VIEW_CUSTOMER, "portfolio")
    condition = access.customer_filter(access.user_scope(db, user))
    total_customers = db.scalar(select(func.count(Customer.id)).where(condition)) or 0
    latest = _dedupe(db.execute(_latest_assessments(db, user)).all())
    distribution = {"Low": 0, "Medium": 0, "High": 0, "Critical": 0, "Insufficient Data": 0}
    for wa, _ in latest:
        distribution[wa.risk_category] = distribution.get(wa.risk_category, 0) + 1
    scored = [wa.warning_score for wa, _ in latest if wa.risk_category != "Insufficient Data"]
    top = sorted(latest, key=lambda row: -row[0].warning_score)[:5]
    return {
        "total_customers": total_customers,
        "total_assessed": len(latest),
        "average_warning_score": round(sum(scored) / len(scored), 2) if scored else 0.0,
        "risk_distribution": distribution,
        "top_risky_customers": [
            {
                "customer_id": c.customer_id,
                "warning_score": wa.warning_score,
                "risk_category": wa.risk_category,
                "top_indicators": json.loads(wa.top_indicators or "[]")[:3],
            }
            for wa, c in top
        ],
    }
