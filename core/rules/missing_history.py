"""
Rule 4: Missing or Incomplete Customer History Detection

Thresholds derived from Home Credit dataset (307,511 customers).
Baseline default rate: 8.07%.

Key insight: missing data does NOT automatically mean higher risk.
  No instalment history → 5.98% default (LOWER than baseline — new customers)
  No bureau history → 10.12% default (1.25x baseline — data gap is a concern)
  Both inst+bureau → 7.84% default (below baseline — full picture helps)

Data availability across the dataset:
  94.8% have instalment records, 85.7% bureau, 28.3% credit card, 94.1% POS cash.
  Common missing fields: EXT_SOURCE_1 (56%), OWN_CAR_AGE (66%), OCCUPATION_TYPE (31%).

Completeness thresholds:
  COMPLETE (≥75):    has instalment + bureau + ≥12 records → full assessment possible
  ADEQUATE (50-74):  has instalment + ≥6 records → reliable pattern analysis
  PARTIAL (25-49):   some data but <6 records or missing bureau
  INSUFFICIENT (<25): <3 records or no instalment history
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import List, Dict, Optional
from enum import Enum


class DataCompleteness(str, Enum):
    COMPLETE = "complete"
    ADEQUATE = "adequate"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"


class CustomerDataType(str, Enum):
    NEW_CUSTOMER = "new_customer"
    DATA_GAP = "data_gap"
    STALE_DATA = "stale_data"
    NORMAL = "normal"


@dataclass
class MissingHistorySignal:
    customer_id: str
    completeness: DataCompleteness
    customer_data_type: CustomerDataType
    completeness_score: float  # 0-100, higher = MORE complete (inverted from other scores)
    risk_assessable: bool  # whether enough data exists to make a risk judgement
    months_of_history: int
    missing_fields: List[str]
    available_fields: List[str]
    repayment_records_count: int
    has_income_data: bool
    has_exposure_data: bool
    has_delinquency_data: bool
    data_staleness_days: int  # days since last data point
    indicators: List[str]


REQUIRED_FIELDS = {
    "customer_profile": ["customer_id", "customer_type"],
    "loan_info": ["loan_id", "sanctioned_amount", "outstanding_balance"],
    "repayment_history": ["due_date", "amount_due", "repayment_status"],
}

RECOMMENDED_FIELDS = {
    "income_data": ["monthly_income"],
    "exposure_data": ["credit_limit", "current_utilisation"],
    "delinquency_data": ["dpd"],
}

# From Home Credit: P10 = 7 records, P25 = 12, P50 = 25
MIN_REPAYMENT_RECORDS = 3
MIN_RECORDS_FOR_ADEQUATE = 6
MIN_RECORDS_FOR_COMPLETE = 12
MIN_MONTHS_FOR_ADEQUATE = 6
MIN_MONTHS_FOR_COMPLETE = 12


def detect_missing_history(
    customer_profiles: pd.DataFrame,
    repayment_history: pd.DataFrame,
    loan_facilities: Optional[pd.DataFrame] = None,
    income_data: Optional[pd.DataFrame] = None,
    exposure_data: Optional[pd.DataFrame] = None,
) -> Dict[str, MissingHistorySignal]:
    """
    Assess data completeness for each customer.

    customer_profiles columns:
        - customer_id: str
        - customer_type: str (e.g. "individual", "sme")
        - onboarding_date: datetime (optional)

    repayment_history columns:
        - customer_id, due_date, amount_due, amount_paid, repayment_status, dpd

    loan_facilities columns (optional):
        - customer_id, facility_id, sanctioned_amount, outstanding_balance,
          credit_limit, current_utilisation

    income_data columns (optional):
        - customer_id, monthly_income, income_date
    """
    results = {}
    all_customer_ids = set(customer_profiles["customer_id"].astype(str).unique())

    repayment_by_customer = dict(tuple(repayment_history.groupby(repayment_history["customer_id"].astype(str))))

    loan_by_customer = {}
    if loan_facilities is not None and not loan_facilities.empty:
        loan_by_customer = dict(tuple(loan_facilities.groupby(loan_facilities["customer_id"].astype(str))))

    income_by_customer = {}
    if income_data is not None and not income_data.empty:
        income_data = income_data.copy()
        income_data["income_date"] = pd.to_datetime(income_data["income_date"])
        income_by_customer = dict(tuple(income_data.groupby(income_data["customer_id"].astype(str))))

    reference_date = pd.Timestamp.now()
    if not repayment_history.empty:
        repayment_history_copy = repayment_history.copy()
        repayment_history_copy["due_date"] = pd.to_datetime(repayment_history_copy["due_date"])
        reference_date = repayment_history_copy["due_date"].max()

    for cid in all_customer_ids:
        profile = customer_profiles[customer_profiles["customer_id"].astype(str) == cid].iloc[0]
        rep_hist = repayment_by_customer.get(cid, pd.DataFrame())
        loans = loan_by_customer.get(cid, pd.DataFrame())
        income = income_by_customer.get(cid, pd.DataFrame())
        exposure_df = loan_by_customer.get(cid, pd.DataFrame())

        signal = _assess_completeness(cid, profile, rep_hist, loans, income, exposure_df, reference_date)
        results[cid] = signal

    return results


def _assess_completeness(
    customer_id: str,
    profile: pd.Series,
    repayment_hist: pd.DataFrame,
    loan_data: pd.DataFrame,
    income_data: pd.DataFrame,
    exposure_data: pd.DataFrame,
    reference_date: pd.Timestamp,
) -> MissingHistorySignal:
    indicators = []
    missing_fields = []
    available_fields = []

    for field in ["customer_id", "customer_type"]:
        if field in profile.index and pd.notna(profile[field]):
            available_fields.append(f"profile.{field}")
        else:
            missing_fields.append(f"profile.{field}")

    repayment_records_count = len(repayment_hist)
    months_of_history = 0

    if repayment_records_count > 0:
        rep_dates = pd.to_datetime(repayment_hist["due_date"])
        date_range = (rep_dates.max() - rep_dates.min()).days
        months_of_history = max(date_range // 30, 1)
        available_fields.append("repayment_history")

        for col in ["due_date", "amount_due", "repayment_status"]:
            if col in repayment_hist.columns and repayment_hist[col].notna().any():
                available_fields.append(f"repayment.{col}")
            else:
                missing_fields.append(f"repayment.{col}")
    else:
        missing_fields.append("repayment_history")

    has_loan_data = not loan_data.empty if isinstance(loan_data, pd.DataFrame) else False
    if has_loan_data:
        available_fields.append("loan_facilities")
        for col in ["sanctioned_amount", "outstanding_balance"]:
            if col in loan_data.columns and loan_data[col].notna().any():
                available_fields.append(f"loan.{col}")
            else:
                missing_fields.append(f"loan.{col}")
    else:
        missing_fields.append("loan_facilities")

    has_income_data = not income_data.empty if isinstance(income_data, pd.DataFrame) else False
    if has_income_data:
        available_fields.append("income_data")
    else:
        missing_fields.append("income_data")

    has_exposure_data = False
    if has_loan_data:
        if "credit_limit" in loan_data.columns and loan_data["credit_limit"].notna().any():
            has_exposure_data = True
            available_fields.append("exposure.credit_limit")
        if "current_utilisation" in loan_data.columns and loan_data["current_utilisation"].notna().any():
            has_exposure_data = True
            available_fields.append("exposure.utilisation")

    if not has_exposure_data:
        missing_fields.append("exposure_data")

    has_delinquency_data = False
    if repayment_records_count > 0 and "dpd" in repayment_hist.columns:
        if repayment_hist["dpd"].notna().any():
            has_delinquency_data = True
            available_fields.append("delinquency.dpd")

    if not has_delinquency_data:
        missing_fields.append("delinquency.dpd")

    data_staleness_days = 0
    if repayment_records_count > 0:
        latest_record = pd.to_datetime(repayment_hist["due_date"]).max()
        data_staleness_days = (reference_date - latest_record).days

    onboarding_date = None
    if "onboarding_date" in profile.index and pd.notna(profile.get("onboarding_date")):
        onboarding_date = pd.to_datetime(profile["onboarding_date"])

    customer_data_type = _determine_data_type(
        months_of_history, repayment_records_count, data_staleness_days, onboarding_date, reference_date
    )

    # --- Indicators backed by data ---
    if repayment_records_count == 0:
        # No-instalment customers → 5.98% default (actually LOWER than baseline)
        indicators.append("No repayment history available — cannot assess repayment behaviour")
    elif repayment_records_count < MIN_REPAYMENT_RECORDS:
        # Records 1-3 → 8.25% default — at baseline but insufficient for pattern detection
        indicators.append(f"Only {repayment_records_count} repayment records — insufficient for pattern analysis (minimum {MIN_REPAYMENT_RECORDS})")

    # P25 = 12 records; <6 records is bottom decile
    if 0 < repayment_records_count < MIN_RECORDS_FOR_ADEQUATE:
        indicators.append(f"Only {repayment_records_count} records — below P10 threshold of 7 for reliable analysis")

    if months_of_history < MIN_MONTHS_FOR_ADEQUATE and months_of_history > 0:
        indicators.append(f"Only {months_of_history} months of history — less than {MIN_MONTHS_FOR_ADEQUATE} months for adequate assessment")

    if not has_income_data:
        indicators.append("No income data available — cannot calculate debt-to-income ratio")

    # 71.7% of customers lack credit card data — this is normal, not a gap
    if not has_exposure_data and has_loan_data:
        indicators.append("No credit limit/utilisation data — exposure assessment limited to loan balances")

    if not has_delinquency_data:
        indicators.append("No DPD data available — delinquency assessment limited")

    if data_staleness_days > 90:
        indicators.append(f"Data is {data_staleness_days} days old — risk assessment may be outdated")
    elif data_staleness_days > 30:
        indicators.append(f"Last data point is {data_staleness_days} days ago")

    if customer_data_type == CustomerDataType.NEW_CUSTOMER:
        # No-instalment default = 5.98% — lower than baseline; do NOT inflate risk
        indicators.append("New customer — limited history expected, not a deterioration signal (default rate 5.98% — below baseline)")
    elif customer_data_type == CustomerDataType.DATA_GAP:
        # No-bureau default = 10.12% — 1.25x baseline; data gap IS concerning
        indicators.append("Established customer with missing data — possible data quality issue (no-bureau default rate 10.12%)")
    elif customer_data_type == CustomerDataType.STALE_DATA:
        indicators.append("Data appears stale — recent activity may not be captured")

    if len(missing_fields) == 0:
        indicators.append("All required and recommended data fields are available")

    completeness_score = _calculate_completeness_score(
        repayment_records_count=repayment_records_count,
        months_of_history=months_of_history,
        has_income_data=has_income_data,
        has_exposure_data=has_exposure_data,
        has_delinquency_data=has_delinquency_data,
        has_loan_data=has_loan_data,
        data_staleness_days=data_staleness_days,
        missing_field_count=len(missing_fields),
    )

    completeness = _score_to_completeness(completeness_score)
    # rules.md: ADEQUATE needs >= 6 monthly instalments. Enforce that as a hard gate,
    # so a thin file that scores well on other fields is not judged on 1-2 payments.
    # Count distinct due dates: re-uploaded or duplicated rows must not add depth.
    distinct_due_dates = repayment_hist["due_date"].nunique() if repayment_records_count else 0
    risk_assessable = (
        completeness in (DataCompleteness.COMPLETE, DataCompleteness.ADEQUATE)
        and distinct_due_dates >= MIN_RECORDS_FOR_ADEQUATE
    )

    return MissingHistorySignal(
        customer_id=customer_id,
        completeness=completeness,
        customer_data_type=customer_data_type,
        completeness_score=round(completeness_score, 2),
        risk_assessable=risk_assessable,
        months_of_history=months_of_history,
        missing_fields=missing_fields,
        available_fields=available_fields,
        repayment_records_count=repayment_records_count,
        has_income_data=has_income_data,
        has_exposure_data=has_exposure_data,
        has_delinquency_data=has_delinquency_data,
        data_staleness_days=data_staleness_days,
        indicators=indicators,
    )


def _determine_data_type(
    months_of_history: int,
    repayment_records: int,
    staleness_days: int,
    onboarding_date: Optional[pd.Timestamp],
    reference_date: pd.Timestamp,
) -> CustomerDataType:
    if onboarding_date is not None:
        months_since_onboarding = (reference_date - onboarding_date).days // 30
        if months_since_onboarding < 6:
            return CustomerDataType.NEW_CUSTOMER

    if repayment_records == 0 and months_of_history == 0:
        return CustomerDataType.NEW_CUSTOMER

    if staleness_days > 90:
        return CustomerDataType.STALE_DATA

    if months_of_history < 3 and repayment_records > 0:
        return CustomerDataType.NEW_CUSTOMER

    # Established customer (>6 months) with very few records is a data gap
    if months_of_history > 6 and repayment_records < MIN_REPAYMENT_RECORDS:
        return CustomerDataType.DATA_GAP

    return CustomerDataType.NORMAL


def _calculate_completeness_score(
    repayment_records_count: int,
    months_of_history: int,
    has_income_data: bool,
    has_exposure_data: bool,
    has_delinquency_data: bool,
    has_loan_data: bool,
    data_staleness_days: int,
    missing_field_count: int,
) -> float:
    """
    Completeness score 0-100 (higher = more complete).

    Weights derived from Home Credit data availability and impact:
      - Repayment history depth (35%): most important for EWS.
        P10=7, P25=12, P50=25 records. ≥12 records (P25) = full credit.
        Records 24+ → 7.65% default (best assessment quality).
      - Bureau/loan data availability (20%): 85.7% have bureau data.
        No bureau → 10.12% default — missing bureau is a concern.
      - History duration (15%): months of observation.
      - Income data (10%): available for ~100% in Home Credit.
      - Data freshness (10%): stale data reduces confidence.
      - Field coverage (10%): completeness of individual fields.
    """
    score = 0.0

    # Repayment history depth (weight: 35%)
    # P10=7, P25=12, P50=25 records
    if repayment_records_count >= MIN_RECORDS_FOR_COMPLETE:  # ≥12 — at P25
        score += 35
    elif repayment_records_count >= MIN_RECORDS_FOR_ADEQUATE:  # ≥6
        score += 24
    elif repayment_records_count >= MIN_REPAYMENT_RECORDS:  # ≥3
        score += 14
    elif repayment_records_count >= 1:
        score += 5

    # Bureau/loan data (weight: 20%)
    # 85.7% have bureau; no-bureau → 10.12% default (1.25x baseline)
    if has_loan_data:
        score += 12
    if has_exposure_data:
        score += 8

    # History duration (weight: 15%)
    if months_of_history >= MIN_MONTHS_FOR_COMPLETE:
        score += 15
    elif months_of_history >= MIN_MONTHS_FOR_ADEQUATE:
        score += 10
    elif months_of_history >= 3:
        score += 6
    elif months_of_history >= 1:
        score += 2

    # Income data (weight: 10%)
    if has_income_data:
        score += 7
    if has_delinquency_data:
        score += 3

    # Data freshness (weight: 10%)
    if data_staleness_days <= 7:
        score += 10
    elif data_staleness_days <= 30:
        score += 7
    elif data_staleness_days <= 90:
        score += 3

    # Field coverage (weight: 10%)
    if missing_field_count == 0:
        score += 10
    elif missing_field_count <= 2:
        score += 6
    elif missing_field_count <= 4:
        score += 3

    return min(score, 100.0)


def _score_to_completeness(score: float) -> DataCompleteness:
    """
    Completeness thresholds:
      COMPLETE (≥75):     has instalment + bureau + ≥12 records
      ADEQUATE (50-74):   has instalment + ≥6 records
      PARTIAL (25-49):    some data but <6 records or missing bureau
      INSUFFICIENT (<25): <3 records or no instalment history
    """
    if score >= 75:
        return DataCompleteness.COMPLETE
    elif score >= 50:
        return DataCompleteness.ADEQUATE
    elif score >= 25:
        return DataCompleteness.PARTIAL
    return DataCompleteness.INSUFFICIENT
