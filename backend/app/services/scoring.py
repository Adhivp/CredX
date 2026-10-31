"""Shared scoring pipeline: load data from the DB, optionally cut it to a past date,
run the four rules and the two trained models, and combine them into a warning score.

`POST /ews/assess` (Level 1) and the Level 2 risk-history replay both call
`score_frames`, so a historical snapshot is computed exactly like a live assessment,
only on the data that was known at that date.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.ews import CashFlowRecord, Customer, CustomerExposure, LoanFacility, RepaymentRecord
from app.models.monitoring import EventClassification

# Make the core package importable (repository root locally, / in the Docker image)
_project_root = Path(__file__).resolve().parents[3]
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

logger = logging.getLogger(__name__)

# Customers whose data is too thin to score. Their provisional score is kept for
# audit but must not be read as a risk rating (see core/rules/rules.md, Rule 4).
INSUFFICIENT_DATA = "Insufficient Data"

# Fallback only, when the model files are absent: same weights as core/ews_engine.py.
SIGNAL_WEIGHTS = {"delay": 0.30, "miss": 0.20, "exposure": 0.25, "cashflow": 0.25}

# Final score = warning-score model (XGBoost trained on Home Credit only, see
# core/models/warning_score_model.py), blended with the cash-flow model when it ran:
# Home Credit has no cash-flow data, so that signal cannot be learned by the model and
# keeps its core/ews_engine.py design weight.
CASHFLOW_BLEND_WEIGHT = 0.25

# Event classifications that mean "not the customer's fault": the instalment is
# scored as paid on its due date. The raw record is kept untouched.
EXCLUDING_CLASSIFICATIONS = {"processing_error", "bank_holiday", "data_error"}
EVENT_CLASSIFICATIONS = EXCLUDING_CLASSIFICATIONS | {"customer_delay", "disputed", "confirmed_miss"}


def score_to_category(score: float) -> str:
    if score < 20:
        return "Low"
    if score < 45:
        return "Medium"
    if score < 70:
        return "High"
    return "Critical"


# ── Data frames ──────────────────────────────────────────────────────────


@dataclass
class Frames:
    repayments: pd.DataFrame
    profiles: pd.DataFrame
    exposure: pd.DataFrame
    income: pd.DataFrame
    cashflow: pd.DataFrame
    loans: pd.DataFrame
    notes: dict = field(default_factory=dict)  # customer_id -> [data caveats]


REPAYMENT_COLS = [
    "customer_id", "loan_id", "due_date", "payment_date",
    "amount_due", "amount_paid", "dpd", "repayment_status", "repayment_id",
]


def build_repayment_df(db: Session, customer_ids: list[str] | None = None, apply_classifications: bool = True) -> pd.DataFrame:
    """Repayment records shaped for the core rules, with analyst event
    classifications applied (an excluded event is scored as paid on time)."""
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
            RepaymentRecord.id,
        )
        .join(LoanFacility, LoanFacility.customer_id == Customer.id)
        .join(RepaymentRecord, RepaymentRecord.loan_id == LoanFacility.id)
    )
    if customer_ids:
        query = query.where(Customer.customer_id.in_(customer_ids))
    df = pd.DataFrame(db.execute(query).all(), columns=REPAYMENT_COLS)
    # Re-uploading a file appends duplicate instalments; one row per loan + due date.
    df = df.drop_duplicates(subset=["loan_id", "due_date"], keep="last")
    if apply_classifications and not df.empty:
        excluded = {
            rid for (rid,) in db.execute(
                select(EventClassification.repayment_id).where(EventClassification.excluded_from_scoring == 1)
            ).all()
        }
        mask = df["repayment_id"].isin(excluded)
        if mask.any():
            df = df.copy()
            df.loc[mask, "payment_date"] = df.loc[mask, "due_date"]
            df.loc[mask, "amount_paid"] = df.loc[mask, "amount_due"]
            df.loc[mask, "dpd"] = 0
            df.loc[mask, "repayment_status"] = "on_time"
    return df


def build_customer_profiles_df(db: Session, customer_ids: list[str] | None = None) -> pd.DataFrame:
    query = select(Customer.customer_id, Customer.customer_type, Customer.created_at)
    if customer_ids:
        query = query.where(Customer.customer_id.in_(customer_ids))
    rows = db.execute(query).all()
    if not rows:
        return pd.DataFrame(columns=["customer_id", "customer_type"])
    return pd.DataFrame(rows, columns=["customer_id", "customer_type", "created_at"])


def build_loans_df(db: Session, customer_ids: list[str] | None = None) -> pd.DataFrame:
    """Loans per customer, with credit limit / utilisation from the exposure snapshot."""
    cols = ["customer_id", "loan_id", "sanctioned_amount", "outstanding_balance", "disbursement_date"]
    query = select(
        Customer.customer_id, LoanFacility.loan_id,
        LoanFacility.sanctioned_amount, LoanFacility.outstanding_balance, LoanFacility.disbursement_date,
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


def build_income_df(db: Session, customer_ids: list[str] | None = None) -> pd.DataFrame:
    """Monthly cash-flow inflow is the only income evidence the platform ingests."""
    query = select(
        Customer.customer_id, CashFlowRecord.monthly_inflow, CashFlowRecord.record_date,
    ).join(CashFlowRecord, CashFlowRecord.customer_id == Customer.id)
    if customer_ids:
        query = query.where(Customer.customer_id.in_(customer_ids))
    rows = [(cid, inflow, date or None) for cid, inflow, date in db.execute(query).all()]
    return pd.DataFrame(rows, columns=["customer_id", "monthly_income", "income_date"])


def build_exposure_df(db: Session, customer_ids: list[str] | None = None) -> pd.DataFrame:
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


def build_cashflow_df(db: Session, customer_ids: list[str] | None = None) -> pd.DataFrame:
    query = select(
        Customer.customer_id, CashFlowRecord.month, CashFlowRecord.monthly_inflow, CashFlowRecord.record_date,
    ).join(CashFlowRecord, CashFlowRecord.customer_id == Customer.id)
    if customer_ids:
        query = query.where(Customer.customer_id.in_(customer_ids))
    df = pd.DataFrame(
        db.execute(query).all(), columns=["customer_id", "month", "monthly_inflow", "record_date"],
    )
    return df.drop_duplicates(subset=["customer_id", "month"], keep="last")


def load_frames(db: Session, customer_ids: list[str] | None = None) -> Frames:
    return Frames(
        repayments=build_repayment_df(db, customer_ids),
        profiles=build_customer_profiles_df(db, customer_ids),
        exposure=build_exposure_df(db, customer_ids),
        income=build_income_df(db, customer_ids),
        cashflow=build_cashflow_df(db, customer_ids),
        loans=build_loans_df(db, customer_ids),
    )


# ── Point-in-time cut ────────────────────────────────────────────────────


def _cashflow_dates(cf: pd.DataFrame) -> pd.Series:
    """record_date when present; otherwise month index aligned so the customer's last
    month is the month of their latest dated record (or the portfolio's)."""
    dates = pd.to_datetime(cf["record_date"].replace("", None), errors="coerce")
    if dates.notna().all():
        return dates
    anchor = dates.max() if dates.notna().any() else pd.Timestamp.today().normalize()
    last_month = cf.groupby("customer_id")["month"].transform("max")
    derived = anchor - pd.to_timedelta((last_month - cf["month"]) * 30, unit="D")
    return dates.fillna(derived)


def frames_as_of(frames: Frames, as_of: pd.Timestamp) -> Frames:
    """The data as it stood at `as_of` (inclusive).

    Repayments: instalments due by then; an instalment paid after `as_of` was unpaid
    at that date, so its days-past-due is the days elapsed and nothing was paid.
    Exposure: snapshots taken by then. A customer with no snapshot that early gets the
    facilities disbursed by then from their earliest snapshot (flagged as reconstructed).
    Cash flow / income: months recorded by then.
    """
    notes: dict[str, list[str]] = {}
    rep = frames.repayments
    if not rep.empty:
        rep = rep.copy()
        due = pd.to_datetime(rep["due_date"], errors="coerce")
        paid = pd.to_datetime(rep["payment_date"], errors="coerce")
        rep = rep[due <= as_of].copy()
        due, paid = due[rep.index], paid[rep.index]
        unpaid = paid.isna() | (paid > as_of)
        elapsed = (as_of - due).dt.days.clip(lower=0)
        # Not yet overdue at as_of and unpaid: not an observable event yet.
        rep = rep[~(unpaid & (elapsed <= 0))].copy()
        due, paid, unpaid, elapsed = due[rep.index], paid[rep.index], unpaid[rep.index], elapsed[rep.index]
        recorded = pd.to_numeric(rep["dpd"], errors="coerce").fillna(0)
        never_paid = paid.isna()
        # Paid later: days elapsed so far. Never paid: elapsed, capped at the recorded DPD.
        dpd_unpaid = elapsed.where(~(never_paid & (recorded > 0)), elapsed.combine(recorded, min))
        rep.loc[unpaid, "dpd"] = dpd_unpaid[unpaid].astype(int)
        rep.loc[unpaid, "amount_paid"] = 0.0
        rep.loc[unpaid, "payment_date"] = None
        rep.loc[unpaid, "repayment_status"] = "missed"

    exp = frames.exposure
    if not exp.empty:
        known = exp[exp["snapshot_date"] <= as_of]
        have = set(known["customer_id"].astype(str))
        missing = exp[~exp["customer_id"].astype(str).isin(have)]
        if not missing.empty:
            first = missing.groupby("customer_id")["snapshot_date"].transform("min")
            recon = missing[(missing["snapshot_date"] == first)
                            & (missing["disbursement_date"].isna() | (missing["disbursement_date"] <= as_of))].copy()
            recon["snapshot_date"] = as_of
            for cid in recon["customer_id"].astype(str).unique():
                notes.setdefault(cid, []).append(
                    "Exposure reconstructed from the earliest snapshot (no snapshot on file for this period)"
                )
            known = pd.concat([known, recon], ignore_index=True)
        exp = known

    cf = frames.cashflow
    if not cf.empty:
        cf = cf[_cashflow_dates(cf) <= as_of]

    inc = frames.income
    if not inc.empty:
        inc_dates = pd.to_datetime(inc["income_date"], errors="coerce")
        inc = inc[inc_dates.isna() | (inc_dates <= as_of)]

    loans = frames.loans
    if not loans.empty and "disbursement_date" in loans:
        ld = pd.to_datetime(loans["disbursement_date"].replace("", None), errors="coerce")
        loans = loans[ld.isna() | (ld <= as_of)]

    return replace(frames, repayments=rep, exposure=exp, cashflow=cf, income=inc, loans=loans, notes=notes)


# ── Scoring ──────────────────────────────────────────────────────────────


@dataclass
class ScoredCustomer:
    customer_id: str
    warning_score: float
    risk_category: str
    rule_scores: dict
    indicators: list[str]
    signals_used: list[str]
    score_source: str
    model: object | None  # core.models.warning_score_model.WarningScoreResult
    delay: object | None
    miss: object | None
    exposure: object | None
    history: object | None
    cashflow: object | None

    @property
    def insufficient(self) -> bool:
        return self.risk_category == INSUFFICIENT_DATA

    def row(self) -> dict:
        return {
            "warning_score": round(self.warning_score, 2),
            "risk_category": self.risk_category,
            "rule_delay_score": round(self.rule_scores["delay"], 2),
            "rule_miss_score": round(self.rule_scores["miss"], 2),
            "rule_exposure_score": round(self.rule_scores["exposure"], 2),
            "rule_completeness_score": round(self.rule_scores["completeness"], 2),
            "cashflow_score": round(self.rule_scores["cashflow"], 2),
        }


def _run_signals(name: str, fn, *args) -> dict:
    """Run one rule/model; a failure in one must not silently zero the others."""
    try:
        return fn(*args) or {}
    except Exception:
        logger.exception("%s failed", name)
        return {}


_MODEL_CACHE: dict = {}


def _load_models() -> dict:
    """The models (and their .joblib files) are optional: a missing model degrades
    to the remaining signals. Loaded once per process."""
    if "loaded" not in _MODEL_CACHE:
        def _cf():
            from core.models.cashflow_model import load_cashflow_model, predict_cashflow_deterioration
            return {"model": load_cashflow_model()[0], "predict": predict_cashflow_deterioration}

        def _ws():
            from core.models import warning_score_model as wsm
            return {"model": wsm.load_warning_score_model()[0], "module": wsm}

        _MODEL_CACHE["cashflow"] = _run_signals("Cash-flow model load", _cf)
        _MODEL_CACHE["warning"] = _run_signals("Warning-score model load", _ws)
        _MODEL_CACHE["loaded"] = True
    return _MODEL_CACHE


def score_frames(frames: Frames) -> dict[str, ScoredCustomer]:
    from core.rules.increasing_exposure import detect_increasing_exposure
    from core.rules.missing_history import detect_missing_history
    from core.rules.repayment_delays import detect_repayment_delays
    from core.rules.temporary_vs_sustained import classify_misses

    repayment_df, profiles_df = frames.repayments, frames.profiles
    exposure_df, income_df, cashflow_df = frames.exposure, frames.income, frames.cashflow
    rules_rep = repayment_df.drop(columns=["repayment_id"], errors="ignore")

    # Rules 1-4
    delay_signals = miss_signals = {}
    if not rules_rep.empty:
        delay_signals = _run_signals("Repayment delay rule", detect_repayment_delays, rules_rep)
        miss_signals = _run_signals("Miss classification rule", classify_misses, rules_rep)
    exposure_signals = {}
    if not exposure_df.empty:
        # No separate balance history is ingested; the snapshots double as history.
        exposure_signals = _run_signals(
            "Exposure rule", detect_increasing_exposure, exposure_df, exposure_df, income_df,
        )
    history_signals = _run_signals(
        "Missing history rule", detect_missing_history, profiles_df, rules_rep, frames.loans, income_df,
    )

    models = _load_models()
    # Model 1: cash-flow deterioration (needs >= 3 months per customer)
    cashflow_signals = {}
    cf = models["cashflow"]
    if cf and not cashflow_df.empty:
        cashflow_signals = _run_signals(
            "Cash-flow model", cf["predict"], cashflow_df[["customer_id", "month", "monthly_inflow"]], cf["model"],
        )

    # Model 2: final warning-score model on the same features it was trained on
    income_by_cid = (
        cashflow_df.sort_values("month").groupby("customer_id")["monthly_inflow"]
        .apply(lambda s: s.tail(3).mean()).to_dict()
        if not cashflow_df.empty else {}
    )
    model_results = {}
    ws = models["warning"]
    if ws:
        latest_exposure = exposure_df
        if not exposure_df.empty:
            last_snap = exposure_df.groupby("customer_id")["snapshot_date"].transform("max")
            latest_exposure = exposure_df[exposure_df["snapshot_date"] == last_snap]
        model_results = _run_signals(
            "Warning-score model",
            lambda: ws["module"].predict_warning_scores(
                ws["module"].platform_features(rules_rep, latest_exposure, income_by_cid), ws["model"],
            ),
        )

    results: dict[str, ScoredCustomer] = {}
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
            # Model files absent: rule composite over the signals that ran.
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

        results[cid] = ScoredCustomer(
            customer_id=cid,
            warning_score=warning_score,
            risk_category=INSUFFICIENT_DATA if insufficient else score_to_category(warning_score),
            rule_scores=rule_scores,
            indicators=indicators,
            signals_used=sorted(available),
            score_source=score_source,
            model=model,
            delay=delay, miss=miss, exposure=exposure, history=hist, cashflow=cashflow,
        )
    return results
