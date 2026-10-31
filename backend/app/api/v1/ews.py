"""EWS API — data ingestion, assessment, watchlist, customer detail, alerts, explainability."""

from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.models.ews import (
    AlertClassification,
    CashFlowRecord,
    Customer,
    CustomerExposure,
    DataUpload,
    LoanFacility,
    RepaymentRecord,
    WarningAssessment,
)
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
from app.services.explainability import explain_assessment

# Make the core package importable
_project_root = Path(__file__).resolve().parents[4]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ews", tags=["ews"])

REQUIRED_COLUMNS: dict[str, set[str]] = {
    "customers": {"customer_id", "customer_type"},
    "loans": {"customer_id", "loan_id", "product_type", "sanctioned_amount", "outstanding_balance"},
    "repayments": {"customer_id", "loan_id", "due_date", "amount_due", "amount_paid", "repayment_status"},
    "exposure": {"customer_id", "facility_type", "outstanding_balance"},
    "cashflow": {"customer_id", "month", "monthly_inflow"},
}


# ── Upload ───────────────────────────────────────────────────────────────


def _parse_file(file: UploadFile) -> pd.DataFrame:
    content = file.file.read()
    name = (file.filename or "").lower()
    if name.endswith(".csv"):
        return pd.read_csv(io.BytesIO(content))
    if name.endswith((".xlsx", ".xls")):
        return pd.read_excel(io.BytesIO(content))
    if name.endswith(".json"):
        return pd.read_json(io.BytesIO(content))
    raise HTTPException(status_code=400, detail="Unsupported file format. Use CSV, Excel, or JSON.")


def _validate_columns(df: pd.DataFrame, data_type: str) -> list[str]:
    required = REQUIRED_COLUMNS.get(data_type)
    if required is None:
        return [f"Unknown data_type '{data_type}'. Use: {', '.join(REQUIRED_COLUMNS)}"]
    missing = required - set(df.columns)
    if missing:
        return [f"Missing required columns: {', '.join(sorted(missing))}"]
    return []


@router.post("/upload", response_model=DataUploadResponse)
def upload_data(
    data_type: str = Query(..., description="One of: customers, loans, repayments, exposure, cashflow"),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    df = _parse_file(file)
    errors = _validate_columns(df, data_type)

    upload = DataUpload(
        filename=file.filename or "unknown",
        file_type=data_type,
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

    loaded = _insert_records(db, df, data_type)
    upload.record_count = loaded
    db.commit()

    return DataUploadResponse(
        upload_id=upload.id,
        filename=upload.filename,
        records_loaded=loaded,
        status="success",
        errors=[],
    )


def _insert_records(db: Session, df: pd.DataFrame, data_type: str) -> int:
    df = df.where(pd.notna(df), None)
    count = 0

    if data_type == "customers":
        for _, row in df.iterrows():
            cid = str(row["customer_id"])
            existing = db.execute(select(Customer).where(Customer.customer_id == cid)).scalar_one_or_none()
            if existing:
                existing.customer_type = str(row.get("customer_type", "individual"))
                existing.employment_category = str(row.get("employment_category", ""))
                existing.portfolio_segment = str(row.get("portfolio_segment", ""))
            else:
                db.add(Customer(
                    customer_id=cid,
                    customer_type=str(row.get("customer_type", "individual")),
                    employment_category=str(row.get("employment_category", "")),
                    portfolio_segment=str(row.get("portfolio_segment", "")),
                ))
            count += 1

    elif data_type == "loans":
        for _, row in df.iterrows():
            cust = db.execute(
                select(Customer).where(Customer.customer_id == str(row["customer_id"]))
            ).scalar_one_or_none()
            if not cust:
                cust = Customer(customer_id=str(row["customer_id"]))
                db.add(cust)
                db.flush()
            existing = db.execute(
                select(LoanFacility).where(LoanFacility.loan_id == str(row["loan_id"]))
            ).scalar_one_or_none()
            if not existing:
                db.add(LoanFacility(
                    customer_id=cust.id,
                    loan_id=str(row["loan_id"]),
                    product_type=str(row.get("product_type", "")),
                    sanctioned_amount=float(row.get("sanctioned_amount", 0)),
                    outstanding_balance=float(row.get("outstanding_balance", 0)),
                    tenure_months=int(row.get("tenure_months", 0)),
                    instalment_amount=float(row.get("instalment_amount", 0)),
                    disbursement_date=str(row.get("disbursement_date", "")),
                    status=str(row.get("status", "active")),
                ))
                count += 1

    elif data_type == "repayments":
        for _, row in df.iterrows():
            loan = db.execute(
                select(LoanFacility).where(LoanFacility.loan_id == str(row["loan_id"]))
            ).scalar_one_or_none()
            if not loan:
                continue
            db.add(RepaymentRecord(
                loan_id=loan.id,
                due_date=str(row["due_date"]),
                payment_date=str(row["payment_date"]) if pd.notna(row.get("payment_date")) else None,
                amount_due=float(row.get("amount_due", 0)),
                amount_paid=float(row.get("amount_paid", 0)),
                dpd=int(row.get("dpd", 0)),
                repayment_status=str(row.get("repayment_status", "on_time")),
            ))
            count += 1

    elif data_type == "exposure":
        for _, row in df.iterrows():
            cust = db.execute(
                select(Customer).where(Customer.customer_id == str(row["customer_id"]))
            ).scalar_one_or_none()
            if not cust:
                continue
            db.add(CustomerExposure(
                customer_id=cust.id,
                facility_type=str(row.get("facility_type", "")),
                sanctioned_amount=float(row.get("sanctioned_amount", 0)),
                credit_limit=float(row.get("credit_limit", 0)),
                current_utilisation=float(row.get("current_utilisation", 0)),
                outstanding_balance=float(row.get("outstanding_balance", 0)),
                instalment_amount=float(row.get("instalment_amount", 0)),
                disbursement_date=str(row.get("disbursement_date", "")),
                snapshot_date=str(row.get("snapshot_date", "")),
            ))
            count += 1

    elif data_type == "cashflow":
        for _, row in df.iterrows():
            cust = db.execute(
                select(Customer).where(Customer.customer_id == str(row["customer_id"]))
            ).scalar_one_or_none()
            if not cust:
                continue
            db.add(CashFlowRecord(
                customer_id=cust.id,
                month=int(row["month"]),
                monthly_inflow=float(row["monthly_inflow"]),
                monthly_outflow=float(row["monthly_outflow"]) if pd.notna(row.get("monthly_outflow")) else None,
                record_date=str(row.get("record_date", "")),
            ))
            count += 1

    return count


# ── Assessment ───────────────────────────────────────────────────────────


def _build_repayment_df(db: Session, customer_ids: list[str] | None = None) -> pd.DataFrame:
    """Load repayment records from DB and shape them for the core rules."""
    query = (
        select(
            Customer.customer_id,
            LoanFacility.loan_id,
            RepaymentRecord.due_date,
            RepaymentRecord.payment_date,
            RepaymentRecord.amount_due,
            RepaymentRecord.amount_paid,
            RepaymentRecord.dpd,
            RepaymentRecord.repayment_status,
        )
        .join(LoanFacility, LoanFacility.customer_id == Customer.id)
        .join(RepaymentRecord, RepaymentRecord.loan_id == LoanFacility.id)
    )
    if customer_ids:
        query = query.where(Customer.customer_id.in_(customer_ids))

    rows = db.execute(query).all()
    if not rows:
        return pd.DataFrame(columns=[
            "customer_id", "loan_id", "due_date", "payment_date",
            "amount_due", "amount_paid", "dpd", "repayment_status",
        ])
    return pd.DataFrame(rows, columns=[
        "customer_id", "loan_id", "due_date", "payment_date",
        "amount_due", "amount_paid", "dpd", "repayment_status",
    ])


def _build_customer_profiles_df(db: Session, customer_ids: list[str] | None = None) -> pd.DataFrame:
    query = select(Customer.customer_id, Customer.customer_type, Customer.created_at)
    if customer_ids:
        query = query.where(Customer.customer_id.in_(customer_ids))
    rows = db.execute(query).all()
    if not rows:
        return pd.DataFrame(columns=["customer_id", "customer_type"])
    return pd.DataFrame(rows, columns=["customer_id", "customer_type", "created_at"])


def _score_to_category(score: float) -> str:
    if score < 20:
        return "Low"
    if score < 45:
        return "Medium"
    if score < 70:
        return "High"
    return "Critical"


@router.post("/assess")
def run_assessment(
    customer_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    filter_ids = [customer_id] if customer_id else None
    repayment_df = _build_repayment_df(db, filter_ids)
    profiles_df = _build_customer_profiles_df(db, filter_ids)

    if profiles_df.empty:
        raise HTTPException(status_code=404, detail="No customers found")

    # Run core rules
    delay_signals = {}
    miss_signals = {}
    try:
        from core.rules.repayment_delays import detect_repayment_delays
        from core.rules.temporary_vs_sustained import classify_misses

        if not repayment_df.empty:
            delay_signals = detect_repayment_delays(repayment_df)
            miss_signals = classify_misses(repayment_df)
    except Exception:
        logger.exception("Core rule execution failed")

    history_signals = {}
    try:
        from core.rules.missing_history import detect_missing_history
        history_signals = detect_missing_history(profiles_df, repayment_df)
    except Exception:
        logger.exception("Missing history rule failed")

    # Build and store assessments
    assessments = []
    all_cids = set(profiles_df["customer_id"].astype(str).unique())
    for cid in all_cids:
        delay = delay_signals.get(cid)
        miss = miss_signals.get(cid)
        hist = history_signals.get(cid)

        delay_score = delay.score if delay else 0.0
        miss_score = miss.score if miss else 0.0
        completeness_score = hist.completeness_score if hist else 100.0

        # Weighted composite
        warning_score = delay_score * 0.35 + miss_score * 0.25 + completeness_score * 0.0 + 0.0
        # Normalise: completeness is inverted (high = good), we want risk score
        incompleteness_penalty = max(0, (50 - completeness_score)) * 0.3
        warning_score = delay_score * 0.35 + miss_score * 0.25 + incompleteness_penalty
        warning_score = min(warning_score, 100.0)

        indicators = []
        if delay:
            indicators.extend(delay.indicators[:3])
        if miss:
            indicators.extend(miss.indicators[:2])
        if hist and hist.indicators:
            indicators.extend(hist.indicators[:2])

        risk_category = _score_to_category(warning_score)

        cust = db.execute(select(Customer).where(Customer.customer_id == cid)).scalar_one_or_none()
        if not cust:
            continue

        assessment = WarningAssessment(
            customer_id=cust.id,
            warning_score=round(warning_score, 2),
            risk_category=risk_category,
            rule_delay_score=round(delay_score, 2),
            rule_miss_score=round(miss_score, 2),
            rule_exposure_score=0.0,
            rule_completeness_score=round(completeness_score, 2),
            cashflow_score=0.0,
            top_indicators=json.dumps(indicators),
        )
        db.add(assessment)
        assessments.append({
            "customer_id": cid,
            "warning_score": round(warning_score, 2),
            "risk_category": risk_category,
            "rule_delay_score": round(delay_score, 2),
            "rule_miss_score": round(miss_score, 2),
            "rule_exposure_score": 0.0,
            "rule_completeness_score": round(completeness_score, 2),
            "cashflow_score": 0.0,
            "top_indicators": indicators,
        })

    db.commit()
    return {"total_assessed": len(assessments), "assessments": assessments}


# ── Watchlist ────────────────────────────────────────────────────────────


@router.get("/watchlist", response_model=WatchlistResponse)
def get_watchlist(
    risk_level: str | None = Query(default=None),
    product_type: str | None = Query(default=None),
    customer_segment: str | None = Query(default=None),
    min_score: float = Query(default=0.0),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
):
    # Subquery: latest assessment per customer
    latest_sq = (
        select(
            WarningAssessment.customer_id,
            func.max(WarningAssessment.assessed_at).label("max_at"),
        )
        .group_by(WarningAssessment.customer_id)
        .subquery()
    )

    query = (
        select(WarningAssessment, Customer.customer_id.label("ext_customer_id"))
        .join(latest_sq, (
            (WarningAssessment.customer_id == latest_sq.c.customer_id)
            & (WarningAssessment.assessed_at == latest_sq.c.max_at)
        ))
        .join(Customer, Customer.id == WarningAssessment.customer_id)
        .where(WarningAssessment.warning_score >= min_score)
    )
    if risk_level:
        query = query.where(WarningAssessment.risk_category == risk_level)
    if customer_segment:
        query = query.where(Customer.portfolio_segment == customer_segment)

    query = query.order_by(WarningAssessment.warning_score.desc())

    total_q = select(func.count()).select_from(query.subquery())
    total = db.execute(total_q).scalar() or 0

    rows = db.execute(query.offset(offset).limit(limit)).all()

    items = []
    for wa, ext_cid in rows:
        indicators = json.loads(wa.top_indicators) if isinstance(wa.top_indicators, str) else wa.top_indicators
        active_signals = sum(1 for s in [wa.rule_delay_score, wa.rule_miss_score, wa.rule_exposure_score, wa.cashflow_score] if s > 20)

        # Get product type and outstanding exposure from loans
        loan = db.execute(
            select(LoanFacility)
            .where(LoanFacility.customer_id == wa.customer_id)
            .order_by(LoanFacility.outstanding_balance.desc())
            .limit(1)
        ).scalar_one_or_none()

        items.append(WatchlistEntry(
            customer_id=ext_cid,
            product_type=loan.product_type if loan else "",
            outstanding_exposure=loan.outstanding_balance if loan else 0.0,
            warning_score=wa.warning_score,
            risk_category=wa.risk_category,
            top_indicators=indicators[:3],
            active_signals=active_signals,
            monitoring_status="Active",
        ))

    return WatchlistResponse(
        items=items,
        total=total,
        filters=WatchlistFilters(
            product_type=product_type,
            customer_segment=customer_segment,
            risk_level=risk_level,
            min_score=min_score,
            max_results=limit,
        ),
    )


# ── Customer Detail ──────────────────────────────────────────────────────


@router.get("/customer/{customer_id}", response_model=CustomerDetailOut)
def get_customer_detail(customer_id: str, db: Session = Depends(get_db)):
    cust = db.execute(select(Customer).where(Customer.customer_id == customer_id)).scalar_one_or_none()
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")

    loans = db.execute(select(LoanFacility).where(LoanFacility.customer_id == cust.id)).scalars().all()
    loan_ids = [l.id for l in loans]

    repayments = []
    if loan_ids:
        repayments = db.execute(
            select(RepaymentRecord).where(RepaymentRecord.loan_id.in_(loan_ids)).order_by(RepaymentRecord.due_date)
        ).scalars().all()

    assessments = db.execute(
        select(WarningAssessment)
        .where(WarningAssessment.customer_id == cust.id)
        .order_by(WarningAssessment.assessed_at.desc())
    ).scalars().all()

    assessment_ids = [a.id for a in assessments]
    alerts = []
    if assessment_ids:
        alerts = db.execute(
            select(AlertClassification)
            .where(AlertClassification.assessment_id.in_(assessment_ids))
            .order_by(AlertClassification.created_at.desc())
        ).scalars().all()

    return CustomerDetailOut(
        customer=CustomerOut.model_validate(cust),
        loans=[LoanFacilityOut.model_validate(l) for l in loans],
        repayments=[RepaymentRecordOut.model_validate(r) for r in repayments],
        assessments=[WarningAssessmentOut.model_validate(a) for a in assessments],
        alerts=[AlertOut.model_validate(a) for a in alerts],
    )


# ── Alert Classification ─────────────────────────────────────────────────


@router.post("/alert/{assessment_id}", response_model=AlertOut)
def classify_alert(assessment_id: str, body: AlertCreate, db: Session = Depends(get_db)):
    assessment = db.execute(
        select(WarningAssessment).where(WarningAssessment.id == assessment_id)
    ).scalar_one_or_none()
    if not assessment:
        raise HTTPException(status_code=404, detail="Assessment not found")

    indicators = json.loads(assessment.top_indicators) if isinstance(assessment.top_indicators, str) else assessment.top_indicators

    alert = AlertClassification(
        assessment_id=assessment.id,
        decision=body.decision,
        reason=body.reason,
        analyst_id=body.analyst_id,
        original_score=assessment.warning_score,
        original_category=assessment.risk_category,
        preserved_evidence=json.dumps({
            "top_indicators": indicators,
            "rule_scores": {
                "delay": assessment.rule_delay_score,
                "miss": assessment.rule_miss_score,
                "exposure": assessment.rule_exposure_score,
                "completeness": assessment.rule_completeness_score,
                "cashflow": assessment.cashflow_score,
            },
        }),
    )
    db.add(alert)
    db.commit()

    return AlertOut.model_validate(alert)


# ── AI Explainability ────────────────────────────────────────────────────


@router.post("/explain/{customer_id}", response_model=ExplainResponse)
def explain_customer(customer_id: str, db: Session = Depends(get_db)):
    from fastapi import Request

    cust = db.execute(select(Customer).where(Customer.customer_id == customer_id)).scalar_one_or_none()
    if not cust:
        raise HTTPException(status_code=404, detail="Customer not found")

    assessment = db.execute(
        select(WarningAssessment)
        .where(WarningAssessment.customer_id == cust.id)
        .order_by(WarningAssessment.assessed_at.desc())
        .limit(1)
    ).scalar_one_or_none()
    if not assessment:
        raise HTTPException(status_code=404, detail="No assessment found for customer")

    indicators = json.loads(assessment.top_indicators) if isinstance(assessment.top_indicators, str) else assessment.top_indicators

    rule_scores = {
        "repayment_delay": assessment.rule_delay_score,
        "miss_classification": assessment.rule_miss_score,
        "exposure": assessment.rule_exposure_score,
        "data_completeness": assessment.rule_completeness_score,
        "cashflow": assessment.cashflow_score,
    }

    # Get settings from app state — use a fallback approach
    from app.core.config import Settings
    try:
        settings = Settings()
    except Exception:
        settings = type("S", (), {"nebius_api_key": "", "nebius_base_url": "", "nebius_model": ""})()

    result = explain_assessment(
        customer_id=customer_id,
        warning_score=assessment.warning_score,
        risk_category=assessment.risk_category,
        rule_scores=rule_scores,
        top_indicators=indicators,
        settings=settings,
    )

    return ExplainResponse(**result)


# ── Portfolio Summary ────────────────────────────────────────────────────


@router.get("/portfolio")
def portfolio_summary(db: Session = Depends(get_db)):
    total_customers = db.execute(select(func.count(Customer.id))).scalar() or 0

    # Latest assessment per customer
    latest_sq = (
        select(
            WarningAssessment.customer_id,
            func.max(WarningAssessment.assessed_at).label("max_at"),
        )
        .group_by(WarningAssessment.customer_id)
        .subquery()
    )
    latest_assessments = db.execute(
        select(WarningAssessment)
        .join(latest_sq, (
            (WarningAssessment.customer_id == latest_sq.c.customer_id)
            & (WarningAssessment.assessed_at == latest_sq.c.max_at)
        ))
    ).scalars().all()

    total_assessed = len(latest_assessments)
    avg_score = sum(a.warning_score for a in latest_assessments) / total_assessed if total_assessed else 0.0

    distribution: dict[str, int] = {"Low": 0, "Medium": 0, "High": 0, "Critical": 0}
    for a in latest_assessments:
        distribution[a.risk_category] = distribution.get(a.risk_category, 0) + 1

    sorted_assessments = sorted(latest_assessments, key=lambda a: -a.warning_score)[:5]
    top_risky = []
    for a in sorted_assessments:
        cust = db.execute(select(Customer).where(Customer.id == a.customer_id)).scalar_one_or_none()
        indicators = json.loads(a.top_indicators) if isinstance(a.top_indicators, str) else a.top_indicators
        top_risky.append({
            "customer_id": cust.customer_id if cust else "unknown",
            "warning_score": a.warning_score,
            "risk_category": a.risk_category,
            "top_indicators": indicators[:3],
        })

    return {
        "total_customers": total_customers,
        "total_assessed": total_assessed,
        "average_warning_score": round(avg_score, 2),
        "risk_distribution": distribution,
        "top_risky_customers": top_risky,
    }
