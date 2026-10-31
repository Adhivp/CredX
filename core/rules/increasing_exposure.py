"""
Rule 3: Increasing Financial Exposure Detection

Thresholds derived from Home Credit dataset (263,491 customers with bureau data).
Baseline default rate: 8.07%.

Severity bands by utilisation and active loan count:
  NONE:     util <30% AND active ≤2  → 6.40-7.44% default (below baseline)
  LOW:      util 30-50% OR active 3  → 8.19-8.26% default (at baseline)
  MEDIUM:   util 50-70% OR active 4  → 9.23-10.31% default (1.1-1.3x)
  HIGH:     util 70-85% OR active 5  → 10.66-13.01% default (1.3-1.6x)
  CRITICAL: util >85% OR active 6+   → 13.38-19.47% default (1.7-2.4x)

Credit card utilisation is a stronger signal than overall bureau utilisation:
  CC 85-95% → 12.66%, CC 95-100% → 15.54%, CC 100%+ → 20.55%
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import List, Dict, Optional
from enum import Enum


BASELINE_DEFAULT_RATE = 0.0807


class ExposureSeverity(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class ExposureSignal:
    customer_id: str
    severity: ExposureSeverity
    score: float  # 0-100
    total_outstanding: float
    total_sanctioned: float
    utilisation_ratio: float  # outstanding / sanctioned
    active_loan_count: int
    new_facilities_3m: int  # new loans taken in last 3 months
    debt_to_income_ratio: float
    obligation_growth_rate: float  # % change in total obligations over 6 months
    balance_trend: str  # "increasing", "stable", "decreasing"
    indicators: List[str]


def detect_increasing_exposure(
    customer_exposure: pd.DataFrame,
    loan_facilities: pd.DataFrame,
    income_data: Optional[pd.DataFrame] = None,
) -> Dict[str, ExposureSignal]:
    """
    Detect customers with rising financial exposure.

    customer_exposure columns:
        - customer_id: str
        - facility_id: str
        - facility_type: str (e.g. "term_loan", "credit_card", "overdraft")
        - sanctioned_amount: float
        - outstanding_balance: float
        - credit_limit: float (for revolving facilities)
        - current_utilisation: float (for revolving facilities)
        - disbursement_date: datetime
        - tenure_months: int
        - instalment_amount: float
        - snapshot_date: datetime (when this record was captured)

    loan_facilities columns (historical snapshots):
        - customer_id: str
        - facility_id: str
        - outstanding_balance: float
        - snapshot_date: datetime

    income_data columns (optional):
        - customer_id: str
        - monthly_income: float
        - income_date: datetime
    """
    results = {}
    customer_exposure = customer_exposure.copy()
    customer_exposure["disbursement_date"] = pd.to_datetime(customer_exposure["disbursement_date"])
    customer_exposure["snapshot_date"] = pd.to_datetime(customer_exposure["snapshot_date"])

    loan_facilities = loan_facilities.copy()
    loan_facilities["snapshot_date"] = pd.to_datetime(loan_facilities["snapshot_date"])

    latest_date = customer_exposure["snapshot_date"].max()

    income_map = {}
    if income_data is not None and not income_data.empty:
        income_data = income_data.copy()
        income_data["income_date"] = pd.to_datetime(income_data["income_date"])
        income_map = (
            income_data.sort_values("income_date")
            .groupby("customer_id")
            .last()["monthly_income"]
            .to_dict()
        )

    for customer_id, exp_group in customer_exposure.groupby("customer_id"):
        cid = str(customer_id)
        hist = loan_facilities[loan_facilities["customer_id"] == customer_id]
        monthly_income = income_map.get(customer_id, None)
        signal = _analyse_exposure(cid, exp_group, hist, monthly_income, latest_date)
        results[cid] = signal

    return results


def _analyse_exposure(
    customer_id: str,
    exposure: pd.DataFrame,
    balance_history: pd.DataFrame,
    monthly_income: Optional[float],
    reference_date: pd.Timestamp,
) -> ExposureSignal:
    indicators = []

    latest_snapshot = exposure[exposure["snapshot_date"] == exposure["snapshot_date"].max()]

    total_outstanding = float(latest_snapshot["outstanding_balance"].sum())
    total_sanctioned = float(latest_snapshot["sanctioned_amount"].sum())
    utilisation_ratio = total_outstanding / total_sanctioned if total_sanctioned > 0 else 0

    active_loan_count = int(latest_snapshot[latest_snapshot["outstanding_balance"] > 0].shape[0])

    cutoff_3m = reference_date - pd.DateOffset(months=3)
    new_facilities_3m = int(
        latest_snapshot[latest_snapshot["disbursement_date"] >= cutoff_3m].shape[0]
    )

    total_monthly_obligations = float(latest_snapshot["instalment_amount"].sum())
    if monthly_income and monthly_income > 0:
        debt_to_income_ratio = total_monthly_obligations / monthly_income
    else:
        debt_to_income_ratio = -1

    obligation_growth_rate, balance_trend = _calculate_balance_trend(balance_history, customer_id)

    revolving = latest_snapshot[latest_snapshot["facility_type"].isin({"credit_card", "overdraft", "revolving"})]
    revolving_util = 0.0
    if not revolving.empty and revolving["credit_limit"].sum() > 0:
        revolving_util = revolving["current_utilisation"].mean()

    # --- Indicators backed by data ---
    # Util >85% → 13.01-14.52% default (1.6-1.8x baseline)
    if utilisation_ratio > 0.85:
        indicators.append(f"High overall utilisation: {utilisation_ratio:.0%} of sanctioned limits used")
    # Util 70-85% → 12.17% default (1.5x baseline)
    elif utilisation_ratio > 0.70:
        indicators.append(f"Elevated utilisation: {utilisation_ratio:.0%} of sanctioned limits used")

    # Active ≥5 → 10.66% (1.32x), ≥6 → 13.38% (1.66x)
    if active_loan_count >= 5:
        indicators.append(f"Multiple active facilities: {active_loan_count} loans outstanding")
    elif active_loan_count >= 4:
        indicators.append(f"Above-median active facilities: {active_loan_count} loans (P90=4)")

    if new_facilities_3m >= 2:
        indicators.append(f"Rapid new borrowing: {new_facilities_3m} new facilities in last 3 months")
    elif new_facilities_3m == 1:
        indicators.append("New facility opened in last 3 months")

    # PTI >30% → 8.13% (at baseline); >40% → 8.61%; the DTI signal is weaker
    if debt_to_income_ratio > 0.50:
        indicators.append(f"High debt-to-income ratio: {debt_to_income_ratio:.0%} of income committed to repayments")
    elif debt_to_income_ratio > 0.30:
        indicators.append(f"Elevated debt-to-income ratio: {debt_to_income_ratio:.0%}")

    if obligation_growth_rate > 0.2:
        indicators.append(f"Outstanding obligations grew {obligation_growth_rate:.0%} over 6 months")

    if balance_trend == "increasing":
        indicators.append("Outstanding balance trend is increasing")

    # CC util is a stronger signal: >85% → 12.66%, >95% → 15.54%, >100% → 20.55%
    if revolving_util > 0.95:
        indicators.append(f"Revolving credit nearly/fully maxed: {revolving_util:.0%} utilisation")
    elif revolving_util > 0.85:
        indicators.append(f"High revolving credit utilisation: {revolving_util:.0%}")

    score = _calculate_exposure_score(
        utilisation_ratio=utilisation_ratio,
        active_loan_count=active_loan_count,
        new_facilities_3m=new_facilities_3m,
        debt_to_income_ratio=debt_to_income_ratio,
        obligation_growth_rate=obligation_growth_rate,
        balance_trend=balance_trend,
        revolving_util=revolving_util,
    )

    severity = _score_to_severity(score)

    return ExposureSignal(
        customer_id=customer_id,
        severity=severity,
        score=round(score, 2),
        total_outstanding=round(total_outstanding, 2),
        total_sanctioned=round(total_sanctioned, 2),
        utilisation_ratio=round(utilisation_ratio, 4),
        active_loan_count=active_loan_count,
        new_facilities_3m=new_facilities_3m,
        debt_to_income_ratio=round(debt_to_income_ratio, 4),
        obligation_growth_rate=round(obligation_growth_rate, 4),
        balance_trend=balance_trend,
        indicators=indicators,
    )


def _calculate_balance_trend(
    balance_history: pd.DataFrame, customer_id: str
) -> tuple:
    if balance_history.empty or len(balance_history) < 2:
        return 0.0, "stable"

    monthly = (
        balance_history.groupby(balance_history["snapshot_date"].dt.to_period("M"))["outstanding_balance"]
        .sum()
        .sort_index()
    )

    if len(monthly) < 2:
        return 0.0, "stable"

    first_val = monthly.iloc[0]
    last_val = monthly.iloc[-1]

    if first_val > 0:
        growth = (last_val - first_val) / first_val
    elif last_val > 0:
        growth = 1.0
    else:
        growth = 0.0

    if growth > 0.1:
        trend = "increasing"
    elif growth < -0.1:
        trend = "decreasing"
    else:
        trend = "stable"

    return float(growth), trend


def _calculate_exposure_score(
    utilisation_ratio: float,
    active_loan_count: int,
    new_facilities_3m: int,
    debt_to_income_ratio: float,
    obligation_growth_rate: float,
    balance_trend: str,
    revolving_util: float,
) -> float:
    """
    Composite exposure score 0-100.

    Weight allocation derived from Home Credit default rate differentials:
      - Overall utilisation (25%): strongest bureau signal.
        <30% → 6.40%, 50-70% → 10.31%, 85-95% → 13.01%, 100%+ → 19.47%.
      - Revolving/CC utilisation (20%): even stronger per-unit signal.
        <30% → 6.23%, 85-95% → 12.66%, 95-100% → 15.54%, 100%+ → 20.55%.
      - Active loan count (15%): 0-1 → 7.10%, 4 → 9.23%, 6+ → 13.38%.
      - New facilities (10%): rapid new borrowing signals distress.
      - Debt-to-income (15%): PTI 0-10% → 7.18%, 20-30% → 8.76%.
        Weaker signal than utilisation — DTI alone doesn't differentiate well.
      - Balance trend (10%): growth trajectory amplifies other signals.
      - Obligation growth rate (5%): supports trend direction.
    """
    score = 0.0

    # Overall utilisation (weight: 25%)
    # <30% → 6.40%, 30-50% → 8.19%, 50-70% → 10.31%, 70-85% → 12.17%,
    # 85-95% → 13.01%, 95-100% → 14.52%, 100%+ → 19.47%
    if utilisation_ratio <= 0.30:
        score += 0
    elif utilisation_ratio <= 0.50:
        score += 7    # 8.19% — at baseline
    elif utilisation_ratio <= 0.70:
        score += 14   # 10.31% — 1.28x baseline
    elif utilisation_ratio <= 0.85:
        score += 19   # 12.17% — 1.51x baseline
    elif utilisation_ratio <= 0.95:
        score += 23   # 13.01% — 1.61x baseline
    else:
        score += 25   # 14.52-19.47% — 1.80-2.41x baseline

    # Revolving/CC utilisation (weight: 20%)
    # Stronger signal per unit than overall util
    # <30% → 6.23%, 85-95% → 12.66%, 95-100% → 15.54%, 100%+ → 20.55%
    if revolving_util <= 0.30:
        score += 0
    elif revolving_util <= 0.50:
        score += 4
    elif revolving_util <= 0.70:
        score += 8
    elif revolving_util <= 0.85:
        score += 12
    elif revolving_util <= 0.95:
        score += 16   # 12.66% — 1.57x baseline
    else:
        score += 20   # 15.54-20.55% — 1.93-2.55x baseline

    # Active loan count (weight: 15%)
    # P50 = 2, P75 = 3, P90 = 4
    # 0-1 → 7.10%, 3 → 8.26%, 4 → 9.23%, 5 → 10.66%, 6+ → 13.38%
    if active_loan_count <= 2:
        score += 0    # at/below median — no signal
    elif active_loan_count == 3:
        score += 4    # P75 — 8.26%, at baseline
    elif active_loan_count == 4:
        score += 8    # P90 — 9.23%, 1.14x baseline
    elif active_loan_count == 5:
        score += 12   # P95 — 10.66%, 1.32x baseline
    else:
        score += 15   # 13.38% — 1.66x baseline

    # New facilities (weight: 10%)
    if new_facilities_3m >= 3:
        score += 10
    elif new_facilities_3m >= 2:
        score += 7
    elif new_facilities_3m == 1:
        score += 3

    # Debt-to-income (weight: 15%)
    # Weaker signal: DTI 0-20% → 7.98%, 40-60% → 9.24%, 80-100% → 9.93%
    # PTI 0-10% → 7.18%, 20-30% → 8.76%, 40-50% → 8.61%
    if debt_to_income_ratio < 0:
        score += 3    # unknown income — mild uncertainty penalty
    elif debt_to_income_ratio <= 0.20:
        score += 0
    elif debt_to_income_ratio <= 0.40:
        score += 5    # ~8.93% — 1.11x baseline
    elif debt_to_income_ratio <= 0.60:
        score += 9    # ~9.24% — 1.14x baseline
    elif debt_to_income_ratio <= 0.80:
        score += 12   # ~9.45% — 1.17x baseline
    else:
        score += 15   # ~9.93% — 1.23x baseline

    # Balance trend (weight: 10%)
    if balance_trend == "increasing":
        score += 10
    elif balance_trend == "stable":
        score += 2

    # Obligation growth rate (weight: 5%)
    growth_contrib = min(max(obligation_growth_rate, 0) * 25, 5)
    score += growth_contrib

    return min(score, 100.0)


def _score_to_severity(score: float) -> ExposureSeverity:
    """
    Severity thresholds aligned with default rate multiples:
      NONE  (<12):  util <30%, active ≤2 → 6.40-7.44% (below baseline)
      LOW   (12-30): util 30-50%, active 3 → 8.19-8.26% (at baseline)
      MEDIUM(30-52): util 50-70%, active 4 → 9.23-10.31% (1.1-1.3x)
      HIGH  (52-75): util 70-85%, active 5 → 10.66-13.01% (1.3-1.6x)
      CRITICAL(>75): util >85%, active 6+ → 13.38-20.55% (1.7-2.6x)
    """
    if score < 12:
        return ExposureSeverity.NONE
    elif score < 30:
        return ExposureSeverity.LOW
    elif score < 52:
        return ExposureSeverity.MEDIUM
    elif score < 75:
        return ExposureSeverity.HIGH
    return ExposureSeverity.CRITICAL
