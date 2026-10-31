"""
Rule 4: Missing or Incomplete Customer History Detection
Identifies customers with insufficient data for risk assessment.
Distinguishes between genuinely new customers and data gaps.
Does NOT flag missing data as high risk — flags as "insufficient information".
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import List, Dict, Optional, Set
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


# Minimum data requirements for risk assessment
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

MIN_REPAYMENT_RECORDS = 3
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
        exposure = loan_by_customer.get(cid, pd.DataFrame())

        signal = _assess_completeness(cid, profile, rep_hist, loans, income, exposure, reference_date)
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

    # --- Check required fields in profile ---
    for field in ["customer_id", "customer_type"]:
        if field in profile.index and pd.notna(profile[field]):
            available_fields.append(f"profile.{field}")
        else:
            missing_fields.append(f"profile.{field}")

    # --- Repayment history assessment ---
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

    # --- Loan data ---
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

    # --- Income data ---
    has_income_data = not income_data.empty if isinstance(income_data, pd.DataFrame) else False
    if has_income_data:
        available_fields.append("income_data")
    else:
        missing_fields.append("income_data")

    # --- Exposure data ---
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

    # --- Delinquency data ---
    has_delinquency_data = False
    if repayment_records_count > 0 and "dpd" in repayment_hist.columns:
        if repayment_hist["dpd"].notna().any():
            has_delinquency_data = True
            available_fields.append("delinquency.dpd")

    if not has_delinquency_data:
        missing_fields.append("delinquency.dpd")

    # --- Data staleness ---
    data_staleness_days = 0
    if repayment_records_count > 0:
        latest_record = pd.to_datetime(repayment_hist["due_date"]).max()
        data_staleness_days = (reference_date - latest_record).days

    # --- Determine customer data type ---
    onboarding_date = None
    if "onboarding_date" in profile.index and pd.notna(profile.get("onboarding_date")):
        onboarding_date = pd.to_datetime(profile["onboarding_date"])

    customer_data_type = _determine_data_type(
        months_of_history, repayment_records_count, data_staleness_days, onboarding_date, reference_date
    )

    # --- Build indicators ---
    if repayment_records_count == 0:
        indicators.append("No repayment history available — cannot assess repayment behaviour")
    elif repayment_records_count < MIN_REPAYMENT_RECORDS:
        indicators.append(f"Only {repayment_records_count} repayment records — insufficient for pattern analysis")

    if months_of_history < MIN_MONTHS_FOR_ADEQUATE and months_of_history > 0:
        indicators.append(f"Only {months_of_history} months of history — less than {MIN_MONTHS_FOR_ADEQUATE} month minimum for adequate assessment")

    if not has_income_data:
        indicators.append("No income data available — cannot calculate debt-to-income ratio")

    if not has_exposure_data:
        indicators.append("No credit limit/utilisation data — cannot assess exposure levels")

    if not has_delinquency_data:
        indicators.append("No DPD data available — delinquency assessment limited")

    if data_staleness_days > 90:
        indicators.append(f"Data is {data_staleness_days} days old — risk assessment may be outdated")
    elif data_staleness_days > 30:
        indicators.append(f"Last data point is {data_staleness_days} days ago")

    if customer_data_type == CustomerDataType.NEW_CUSTOMER:
        indicators.append("New customer — limited history expected, not a deterioration signal")
    elif customer_data_type == CustomerDataType.DATA_GAP:
        indicators.append("Established customer with missing data — possible data quality issue")
    elif customer_data_type == CustomerDataType.STALE_DATA:
        indicators.append("Data appears stale — recent activity may not be captured")

    if len(missing_fields) == 0:
        indicators.append("All required and recommended data fields are available")

    # --- Completeness score (higher = more complete) ---
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
    risk_assessable = completeness in (DataCompleteness.COMPLETE, DataCompleteness.ADEQUATE)

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

    if months_of_history > 6 and repayment_records < 3:
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
    score = 0.0

    # Repayment history depth (weight: 30%)
    if repayment_records_count >= 12:
        score += 30
    elif repayment_records_count >= 6:
        score += 20
    elif repayment_records_count >= 3:
        score += 12
    elif repayment_records_count >= 1:
        score += 5

    # History duration (weight: 20%)
    if months_of_history >= 12:
        score += 20
    elif months_of_history >= 6:
        score += 14
    elif months_of_history >= 3:
        score += 8
    elif months_of_history >= 1:
        score += 3

    # Data availability (weight: 30%)
    if has_loan_data:
        score += 8
    if has_income_data:
        score += 8
    if has_exposure_data:
        score += 7
    if has_delinquency_data:
        score += 7

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
    if score >= 75:
        return DataCompleteness.COMPLETE
    elif score >= 50:
        return DataCompleteness.ADEQUATE
    elif score >= 25:
        return DataCompleteness.PARTIAL
    return DataCompleteness.INSUFFICIENT
