"""EWS API — data ingestion, assessment, watchlist, customer detail, alerts, explainability."""

from __future__ import annotations

import io
import json
import logging
import sys
from pathlib import Path

import pandas as pd
from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
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
            values = dict(
                payment_date=str(row["payment_date"]) if pd.notna(row.get("payment_date")) else None,
                amount_due=float(row.get("amount_due", 0)),
                amount_paid=float(row.get("amount_paid", 0)),
                dpd=int(row.get("dpd", 0)),
                repayment_status=str(row.get("repayment_status", "on_time")),
            )
            # One instalment per loan + due date: re-uploading a file updates, never duplicates.
            existing = db.execute(select(RepaymentRecord).where(
                RepaymentRecord.loan_id == loan.id, RepaymentRecord.due_date == str(row["due_date"]),
            ).limit(1)).scalar_one_or_none()
            if existing:
                for k, v in values.items():
                    setattr(existing, k, v)
            else:
                db.add(RepaymentRecord(loan_id=loan.id, due_date=str(row["due_date"]), **values))
            db.flush()
            count += 1

    elif data_type == "exposure":
        for _, row in df.iterrows():
            cust = db.execute(
                select(Customer).where(Customer.customer_id == str(row["customer_id"]))
            ).scalar_one_or_none()
            if not cust:
                continue
            key = dict(
                customer_id=cust.id,
                facility_type=str(row.get("facility_type", "")),
                disbursement_date=str(row.get("disbursement_date", "")),
                snapshot_date=str(row.get("snapshot_date", "")),
            )
            values = dict(
                sanctioned_amount=float(row.get("sanctioned_amount", 0)),
                credit_limit=float(row.get("credit_limit", 0)),
                current_utilisation=float(row.get("current_utilisation", 0)),
                outstanding_balance=float(row.get("outstanding_balance", 0)),
                instalment_amount=float(row.get("instalment_amount", 0)),
            )
            # One row per facility per snapshot: re-uploading updates, never duplicates.
            existing = db.execute(select(CustomerExposure).filter_by(**key).limit(1)).scalar_one_or_none()
            if existing:
                for k, v in values.items():
                    setattr(existing, k, v)
            else:
                db.add(CustomerExposure(**key, **values))
            db.flush()
            count += 1

    elif data_type == "cashflow":
        for _, row in df.iterrows():
            cust = db.execute(
                select(Customer).where(Customer.customer_id == str(row["customer_id"]))
            ).scalar_one_or_none()
            if not cust:
                continue
            values = dict(
                monthly_inflow=float(row["monthly_inflow"]),
                monthly_outflow=float(row["monthly_outflow"]) if pd.notna(row.get("monthly_outflow")) else None,
                record_date=str(row.get("record_date", "")),
            )
            # One row per customer + month: re-uploading updates, never duplicates.
            existing = db.execute(select(CashFlowRecord).where(
                CashFlowRecord.customer_id == cust.id, CashFlowRecord.month == int(row["month"]),
            ).limit(1)).scalar_one_or_none()
            if existing:
                for k, v in values.items():
                    setattr(existing, k, v)
            else:
                db.add(CashFlowRecord(customer_id=cust.id, month=int(row["month"]), **values))
            db.flush()
            count += 1

    return count


# ── Assessment ───────────────────────────────────────────────────────────


def _build_repayment_df(db: Session, customer_ids: list[str] | None = None) -> pd.DataFrame:
    """Load repayment records from DB and shape them for the core rules."""
    cols = [
        "customer_id", "loan_id", "due_date", "payment_date",
        "amount_due", "amount_paid", "dpd", "repayment_status",
    ]
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
    df = pd.DataFrame(db.execute(query).all(), columns=cols)
    # Re-uploading a file appends duplicate instalments; one row per loan + due date.
    return df.drop_duplicates(subset=["loan_id", "due_date"], keep="last")


def _build_customer_profiles_df(db: Session, customer_ids: list[str] | None = None) -> pd.DataFrame:
    query = select(Customer.customer_id, Customer.customer_type, Customer.created_at)
    if customer_ids:
        query = query.where(Customer.customer_id.in_(customer_ids))
    rows = db.execute(query).all()
    if not rows:
        return pd.DataFrame(columns=["customer_id", "customer_type"])
    return pd.DataFrame(rows, columns=["customer_id", "customer_type", "created_at"])


def _build_loans_df(db: Session, customer_ids: list[str] | None = None) -> pd.DataFrame:
    """Loans per customer, with credit limit / utilisation from the exposure snapshot."""
    cols = ["customer_id", "loan_id", "sanctioned_amount", "outstanding_balance"]
    query = select(
        Customer.customer_id, LoanFacility.loan_id,
        LoanFacility.sanctioned_amount, LoanFacility.outstanding_balance,
    ).join(LoanFacility, LoanFacility.customer_id == Customer.id)
    if customer_ids:
        query = query.where(Customer.customer_id.in_(customer_ids))
    loans = pd.DataFrame(db.execute(query).all(), columns=cols)

    exp_query = select(
        Customer.customer_id, CustomerExposure.credit_limit, CustomerExposure.current_utilisation,
    ).join(CustomerExposure, CustomerExposure.customer_id == Customer.id)
    if customer_ids:
        exp_query = exp_query.where(Customer.customer_id.in_(customer_ids))
    exposure = pd.DataFrame(
        db.execute(exp_query).all(), columns=["customer_id", "credit_limit", "current_utilisation"]
    )
    if loans.empty or exposure.empty:
        return loans
    exposure = exposure.groupby("customer_id", as_index=False).max()
    return loans.merge(exposure, on="customer_id", how="left")


def _build_income_df(db: Session, customer_ids: list[str] | None = None) -> pd.DataFrame:
    """Monthly cash-flow inflow is the only income evidence the platform ingests."""
    query = select(
        Customer.customer_id, CashFlowRecord.monthly_inflow, CashFlowRecord.record_date,
    ).join(CashFlowRecord, CashFlowRecord.customer_id == Customer.id)
    if customer_ids:
        query = query.where(Customer.customer_id.in_(customer_ids))
    rows = [(cid, inflow, date or None) for cid, inflow, date in db.execute(query).all()]
    return pd.DataFrame(rows, columns=["customer_id", "monthly_income", "income_date"])


def _build_exposure_df(db: Session, customer_ids: list[str] | None = None) -> pd.DataFrame:
    """Exposure snapshots in the shape core.rules.increasing_exposure expects."""
    cols = [
        "customer_id", "facility_type", "sanctioned_amount", "credit_limit", "current_utilisation",
        "outstanding_balance", "instalment_amount", "disbursement_date", "snapshot_date",
    ]
    query = select(
        Customer.customer_id, CustomerExposure.facility_type, CustomerExposure.sanctioned_amount,
        CustomerExposure.credit_limit, CustomerExposure.current_utilisation,
        CustomerExposure.outstanding_balance, CustomerExposure.instalment_amount,
        CustomerExposure.disbursement_date, CustomerExposure.snapshot_date,
    ).join(CustomerExposure, CustomerExposure.customer_id == Customer.id)
    if customer_ids:
        query = query.where(Customer.customer_id.in_(customer_ids))
    df = pd.DataFrame(db.execute(query).all(), columns=cols).drop_duplicates()
    for col in ("disbursement_date", "snapshot_date"):
        df[col] = pd.to_datetime(df[col].replace("", None), errors="coerce")
    return df.dropna(subset=["snapshot_date"])


def _build_cashflow_df(db: Session, customer_ids: list[str] | None = None) -> pd.DataFrame:
    query = select(
        Customer.customer_id, CashFlowRecord.month, CashFlowRecord.monthly_inflow,
    ).join(CashFlowRecord, CashFlowRecord.customer_id == Customer.id)
    if customer_ids:
        query = query.where(Customer.customer_id.in_(customer_ids))
    df = pd.DataFrame(db.execute(query).all(), columns=["customer_id", "month", "monthly_inflow"])
    return df.drop_duplicates(subset=["customer_id", "month"], keep="last")


# Customers whose data is too thin to score. Their provisional score is kept for
# audit but must not be read as a risk rating (see core/rules/rules.md, Rule 4).
INSUFFICIENT_DATA = "Insufficient Data"


def _score_to_category(score: float) -> str:
    if score < 20:
        return "Low"
    if score < 45:
        return "Medium"
    if score < 70:
        return "High"
    return "Critical"


# Fallback only, when the model files are absent: same weights as core/ews_engine.py.
SIGNAL_WEIGHTS = {"delay": 0.30, "miss": 0.20, "exposure": 0.25, "cashflow": 0.25}

# Final score = warning-score model (XGBoost trained on Home Credit only, see
# core/models/warning_score_model.py), blended with the cash-flow model when it ran:
# Home Credit has no cash-flow data, so that signal cannot be learned by the model and
# keeps its core/ews_engine.py design weight.
CASHFLOW_BLEND_WEIGHT = 0.25


def _run_signals(name: str, fn, *args) -> dict:
    """Run one rule/model; a failure in one must not silently zero the others."""
    try:
        return fn(*args) or {}
    except Exception:
        logger.exception("%s failed", name)
        return {}


@router.post("/assess")
def run_assessment(
    customer_id: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    from core.rules.increasing_exposure import detect_increasing_exposure
    from core.rules.missing_history import detect_missing_history
    from core.rules.repayment_delays import detect_repayment_delays
    from core.rules.temporary_vs_sustained import classify_misses

    filter_ids = [customer_id] if customer_id else None
    repayment_df = _build_repayment_df(db, filter_ids)
    profiles_df = _build_customer_profiles_df(db, filter_ids)
    if profiles_df.empty:
        raise HTTPException(status_code=404, detail="No customers found")

    exposure_df = _build_exposure_df(db, filter_ids)
    income_df = _build_income_df(db, filter_ids)
    cashflow_df = _build_cashflow_df(db, filter_ids)

    # Rules 1-4
    delay_signals = miss_signals = {}
    if not repayment_df.empty:
        delay_signals = _run_signals("Repayment delay rule", detect_repayment_delays, repayment_df)
        miss_signals = _run_signals("Miss classification rule", classify_misses, repayment_df)
    exposure_signals = {}
    if not exposure_df.empty:
        # No separate balance history is ingested; the snapshots double as history.
        exposure_signals = _run_signals(
            "Exposure rule", detect_increasing_exposure, exposure_df, exposure_df, income_df,
        )
    history_signals = _run_signals(
        "Missing history rule", detect_missing_history,
        profiles_df, repayment_df, _build_loans_df(db, filter_ids), income_df,
    )

    # The models (and their gitignored .joblib files) are optional: the Docker image
    # ships core/rules only, so a missing model degrades to the remaining signals.
    def _load_cashflow():
        from core.models.cashflow_model import load_cashflow_model, predict_cashflow_deterioration
        return {"model": load_cashflow_model()[0], "predict": predict_cashflow_deterioration}

    def _load_warning():
        from core.models import warning_score_model as wsm
        return {"model": wsm.load_warning_score_model()[0], "module": wsm}

    # Model 1: cash-flow deterioration (needs >= 3 months per customer)
    cashflow_signals = {}
    cf = _run_signals("Cash-flow model load", _load_cashflow)
    if cf and not cashflow_df.empty:
        cashflow_signals = _run_signals("Cash-flow model", cf["predict"], cashflow_df, cf["model"])

    # Model 2: final warning-score model on the same features it was trained on
    income_by_cid = (
        cashflow_df.sort_values("month").groupby("customer_id")["monthly_inflow"]
        .apply(lambda s: s.tail(3).mean()).to_dict()
        if not cashflow_df.empty else {}
    )
    model_results = {}
    ws = _run_signals("Warning-score model load", _load_warning)
    if ws:
        latest_exposure = exposure_df
        if not exposure_df.empty:
            last_snap = exposure_df.groupby("customer_id")["snapshot_date"].transform("max")
            latest_exposure = exposure_df[exposure_df["snapshot_date"] == last_snap]
        model_results = _run_signals(
            "Warning-score model",
            lambda: ws["module"].predict_warning_scores(
                ws["module"].platform_features(repayment_df, latest_exposure, income_by_cid), ws["model"],
            ),
        )

    assessments = []
    for cid in sorted(set(profiles_df["customer_id"].astype(str))):
        delay, miss = delay_signals.get(cid), miss_signals.get(cid)
        exposure, hist, cashflow = exposure_signals.get(cid), history_signals.get(cid), cashflow_signals.get(cid)

        rule_scores = {
            "delay": delay.score if delay else 0.0,
            "miss": miss.score if miss else 0.0,
            "exposure": exposure.score if exposure else 0.0,
            "completeness": hist.completeness_score if hist else 100.0,
            "cashflow": cashflow.score if cashflow else 0.0,
        }

        available = {
            name: rule_scores[name]
            for name, signal in (("delay", delay), ("miss", miss), ("exposure", exposure), ("cashflow", cashflow))
            if signal is not None
        }
        model = model_results.get(cid)
        if model is not None:
            warning_score = model.warning_score
            if cashflow is not None:
                warning_score = (
                    (1 - CASHFLOW_BLEND_WEIGHT) * model.warning_score + CASHFLOW_BLEND_WEIGHT * cashflow.score
                )
            score_source = "warning_score_model"
        else:
            # Model files absent (e.g. the Docker image): rule composite over the signals that ran.
            total_weight = sum(SIGNAL_WEIGHTS[n] for n in available)
            warning_score = (
                sum(SIGNAL_WEIGHTS[n] * v for n, v in available.items()) / total_weight if total_weight else 0.0
            )
            score_source = "rule_composite"

        # Data gaps are handled by the Insufficient Data tag, not by adding risk.
        insufficient = hist is not None and not hist.risk_assessable

        indicators = []
        if model is not None and not insufficient:
            indicators.extend(model.indicators)
        if insufficient:
            # Lead with the data gaps: they, not delinquency, explain this customer.
            indicators.extend(hist.indicators)
        if delay:
            indicators.extend(delay.indicators[:3])
        if miss:
            indicators.extend(miss.indicators[:2])
        if exposure:
            indicators.extend(exposure.indicators[:2])
        if cashflow:
            indicators.extend(cashflow.indicators[:2])
        if hist and hist.indicators and not insufficient:
            indicators.extend(hist.indicators[:2])

        risk_category = INSUFFICIENT_DATA if insufficient else _score_to_category(warning_score)

        cust = db.execute(select(Customer).where(Customer.customer_id == cid)).scalar_one_or_none()
        if not cust:
            continue

        row = {
            "warning_score": round(warning_score, 2),
            "risk_category": risk_category,
            "rule_delay_score": round(rule_scores["delay"], 2),
            "rule_miss_score": round(rule_scores["miss"], 2),
            "rule_exposure_score": round(rule_scores["exposure"], 2),
            "rule_completeness_score": round(rule_scores["completeness"], 2),
            "cashflow_score": round(rule_scores["cashflow"], 2),
        }
        db.add(WarningAssessment(customer_id=cust.id, top_indicators=json.dumps(indicators), **row))
        assessments.append({
            "customer_id": cid,
            **row,
            "top_indicators": indicators,
            "signals_used": sorted(available),
            "score_source": score_source,
            "model_warning_score": model.warning_score if model else None,
            "model_default_probability": model.default_probability if model else None,
            "model_top_contributors": model.top_contributors if model else [],
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
            monitoring_status=INSUFFICIENT_DATA if wa.risk_category == INSUFFICIENT_DATA else "Active",
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
def explain_customer(customer_id: str, request: Request, db: Session = Depends(get_db)):
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

    # One instalment per loan + due date (re-uploads append duplicate rows).
    repayments = list({
        (loan_id, due): (due, dpd)
        for loan_id, due, dpd in db.execute(
            select(RepaymentRecord.loan_id, RepaymentRecord.due_date, RepaymentRecord.dpd)
            .join(LoanFacility, LoanFacility.id == RepaymentRecord.loan_id)
            .where(LoanFacility.customer_id == cust.id)
            .order_by(RepaymentRecord.id)
        ).all()
    }.values())
    loans = db.execute(select(LoanFacility).where(LoanFacility.customer_id == cust.id)).scalars().all()
    has_income = db.execute(
        select(func.count(func.distinct(CashFlowRecord.month))).where(CashFlowRecord.customer_id == cust.id)
    ).scalar() or 0
    has_exposure = db.execute(
        select(func.count(CustomerExposure.id)).where(CustomerExposure.customer_id == cust.id)
    ).scalar() or 0
    not_evaluated = set()
    if not has_exposure:
        not_evaluated.add("exposure")
    if has_income < 3 or not assessment.cashflow_score:
        not_evaluated.add("cashflow")  # the model needs >= 3 months of cash flow
    facts = {
        "repayment_records": len(repayments),
        "late_payments": sum(1 for _, dpd in repayments if (dpd or 0) > 0),
        "max_dpd": max((dpd or 0 for _, dpd in repayments), default=0),
        "first_due_date": min((d for d, _ in repayments), default=None),
        "last_due_date": max((d for d, _ in repayments), default=None),
        "active_loans": len(loans),
        "total_outstanding": round(sum(l.outstanding_balance for l in loans), 2),
        "income_data_on_file": bool(has_income),
    }

    result = explain_assessment(
        customer_id=customer_id,
        warning_score=assessment.warning_score,
        risk_category=assessment.risk_category,
        rule_scores=rule_scores,
        top_indicators=indicators,
        facts=facts,
        insufficient_data=assessment.risk_category == INSUFFICIENT_DATA,
        not_evaluated=not_evaluated,
        settings=request.app.state.settings,
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
    # Provisional scores of thin-file customers are not risk ratings: keep them out
    # of the average and the top-risk list.
    scored = [a for a in latest_assessments if a.risk_category != INSUFFICIENT_DATA]
    avg_score = sum(a.warning_score for a in scored) / len(scored) if scored else 0.0

    distribution: dict[str, int] = {"Low": 0, "Medium": 0, "High": 0, "Critical": 0, INSUFFICIENT_DATA: 0}
    for a in latest_assessments:
        distribution[a.risk_category] = distribution.get(a.risk_category, 0) + 1

    sorted_assessments = sorted(scored, key=lambda a: -a.warning_score)[:5]
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
