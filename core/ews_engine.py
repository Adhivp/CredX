"""
Early Warning System — Core Engine
Unified functions that combine all rules and models into callable backend functions.

Usage:
    from core.ews_engine import (
        run_full_assessment,
        run_rules_only,
        run_single_customer,
        calculate_warning_score,
        generate_watchlist,
        classify_alert,
    )
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Any
from enum import Enum

from core.rules.repayment_delays import detect_repayment_delays, RepaymentDelaySignal
from core.rules.temporary_vs_sustained import classify_misses, MissClassificationSignal
from core.rules.increasing_exposure import detect_increasing_exposure, ExposureSignal
from core.rules.missing_history import detect_missing_history, MissingHistorySignal
from core.models.cashflow_model import predict_cashflow_deterioration, CashFlowSignal, load_cashflow_model
from core.models.warning_score_model import predict_warning_score, load_warning_score_model, WarningScoreResult


class RiskCategory(str, Enum):
    LOW = "Low"
    MEDIUM = "Medium"
    HIGH = "High"
    CRITICAL = "Critical"


class AlertDecision(str, Enum):
    CONTINUE_MONITORING = "Continue Monitoring"
    REQUIRES_INVESTIGATION = "Requires Investigation"
    FALSE_POSITIVE = "False Positive"
    ESCALATE_FOR_REVIEW = "Escalate for Review"


@dataclass
class CustomerAssessment:
    customer_id: str
    warning_score: float
    risk_category: str
    repayment_delay: Optional[dict]
    miss_classification: Optional[dict]
    exposure: Optional[dict]
    missing_history: Optional[dict]
    cashflow: Optional[dict]
    warning_model: Optional[dict]
    all_indicators: List[str]
    top_risk_factors: List[str]
    assessable: bool


@dataclass
class WatchlistEntry:
    customer_id: str
    warning_score: float
    risk_category: str
    top_indicators: List[str]
    active_signals: int
    monitoring_status: str


@dataclass
class AlertClassification:
    customer_id: str
    original_score: float
    original_category: str
    decision: str
    reason: str
    analyst_id: Optional[str]
    timestamp: str
    preserved_evidence: dict


def run_full_assessment(
    repayment_history: pd.DataFrame,
    customer_profiles: pd.DataFrame,
    customer_exposure: Optional[pd.DataFrame] = None,
    loan_facilities: Optional[pd.DataFrame] = None,
    income_data: Optional[pd.DataFrame] = None,
    cashflow_data: Optional[pd.DataFrame] = None,
) -> Dict[str, CustomerAssessment]:
    """
    Run the complete EWS assessment pipeline for all customers.

    This is the main entry point. It runs all 4 rules, the cashflow model,
    and the warning score model, then combines everything into a per-customer
    assessment.

    Returns dict of customer_id -> CustomerAssessment
    """
    # Run all rules
    delay_signals = detect_repayment_delays(repayment_history)
    miss_signals = classify_misses(repayment_history)

    exposure_signals = {}
    if customer_exposure is not None and loan_facilities is not None:
        exposure_signals = detect_increasing_exposure(customer_exposure, loan_facilities, income_data)

    history_signals = detect_missing_history(
        customer_profiles, repayment_history, loan_facilities, income_data
    )

    # Run cashflow model
    cashflow_signals = {}
    if cashflow_data is not None and not cashflow_data.empty:
        try:
            cf_model = load_cashflow_model()
            cashflow_signals = predict_cashflow_deterioration(cashflow_data, cf_model[0])
        except FileNotFoundError:
            pass

    # Combine all signals into assessments
    all_customer_ids = set()
    all_customer_ids.update(delay_signals.keys())
    all_customer_ids.update(miss_signals.keys())
    all_customer_ids.update(history_signals.keys())

    results = {}
    for cid in all_customer_ids:
        assessment = _build_assessment(
            cid,
            delay_signals.get(cid),
            miss_signals.get(cid),
            exposure_signals.get(cid),
            history_signals.get(cid),
            cashflow_signals.get(cid),
        )
        results[cid] = assessment

    return results


def run_rules_only(
    repayment_history: pd.DataFrame,
    customer_profiles: pd.DataFrame,
    customer_exposure: Optional[pd.DataFrame] = None,
    loan_facilities: Optional[pd.DataFrame] = None,
    income_data: Optional[pd.DataFrame] = None,
) -> Dict[str, dict]:
    """
    Run only the rule-based signals (no ML models).
    Lighter weight, suitable for real-time monitoring.

    Returns dict of customer_id -> {rule_name: signal_dict}
    """
    delay_signals = detect_repayment_delays(repayment_history)
    miss_signals = classify_misses(repayment_history)

    exposure_signals = {}
    if customer_exposure is not None and loan_facilities is not None:
        exposure_signals = detect_increasing_exposure(customer_exposure, loan_facilities, income_data)

    history_signals = detect_missing_history(
        customer_profiles, repayment_history, loan_facilities, income_data
    )

    all_ids = set(delay_signals.keys()) | set(miss_signals.keys()) | set(history_signals.keys())

    results = {}
    for cid in all_ids:
        results[cid] = {
            "repayment_delays": asdict(delay_signals[cid]) if cid in delay_signals else None,
            "miss_classification": asdict(miss_signals[cid]) if cid in miss_signals else None,
            "exposure": asdict(exposure_signals[cid]) if cid in exposure_signals else None,
            "missing_history": asdict(history_signals[cid]) if cid in history_signals else None,
        }

    return results


def run_single_customer(
    customer_id: str,
    repayment_history: pd.DataFrame,
    customer_profile: pd.DataFrame,
    customer_exposure: Optional[pd.DataFrame] = None,
    loan_facilities: Optional[pd.DataFrame] = None,
    income_data: Optional[pd.DataFrame] = None,
    cashflow_data: Optional[pd.DataFrame] = None,
) -> CustomerAssessment:
    """
    Run full assessment for a single customer.
    Filters all dataframes to the given customer_id before processing.
    """
    cid = str(customer_id)
    rep = repayment_history[repayment_history["customer_id"].astype(str) == cid]
    prof = customer_profile[customer_profile["customer_id"].astype(str) == cid]

    exp = None
    if customer_exposure is not None:
        exp = customer_exposure[customer_exposure["customer_id"].astype(str) == cid]

    loans = None
    if loan_facilities is not None:
        loans = loan_facilities[loan_facilities["customer_id"].astype(str) == cid]

    inc = None
    if income_data is not None:
        inc = income_data[income_data["customer_id"].astype(str) == cid]

    cf = None
    if cashflow_data is not None:
        cf = cashflow_data[cashflow_data["customer_id"].astype(str) == cid]

    assessments = run_full_assessment(rep, prof, exp, loans, inc, cf)
    return assessments.get(cid, _empty_assessment(cid))


def calculate_warning_score(
    rule_scores: dict,
    raw_features: dict,
) -> WarningScoreResult:
    """
    Calculate warning score from pre-computed rule scores and raw features.
    Useful when rules have already been run separately.

    rule_scores: {
        "delay_score": float,
        "miss_score": float,
        "exposure_score": float,
        "completeness_score": float,
        "cashflow_score": float,
    }

    raw_features: {
        "late_ratio": float,
        "max_dpd": int,
        "avg_days_late": float,
        "consecutive_misses": int,
        "recovery_rate": float,
        "active_loans": int,
        "utilisation_ratio": float,
        "cc_utilisation": float,
        "debt_to_income": float,
        "payment_to_income": float,
        "months_of_history": int,
    }
    """
    feature_dict = {
        "rule_delay_score": rule_scores.get("delay_score", 0),
        "rule_miss_score": rule_scores.get("miss_score", 0),
        "rule_exposure_score": rule_scores.get("exposure_score", 0),
        "rule_completeness_score": rule_scores.get("completeness_score", 0),
        "model_cashflow_score": rule_scores.get("cashflow_score", 0),
        **raw_features,
    }

    try:
        model, _ = load_warning_score_model()
        return predict_warning_score(feature_dict, model)
    except FileNotFoundError:
        return _fallback_warning_score(feature_dict)


def generate_watchlist(
    assessments: Dict[str, CustomerAssessment],
    min_score: float = 20.0,
    max_results: Optional[int] = None,
    filter_category: Optional[str] = None,
) -> List[WatchlistEntry]:
    """
    Generate a sorted watchlist from customer assessments.
    Filters by minimum score and optionally by risk category.
    Returns highest risk first.
    """
    entries = []
    for cid, assessment in assessments.items():
        if assessment.warning_score < min_score:
            continue
        if filter_category and assessment.risk_category != filter_category:
            continue

        active_signals = sum(1 for s in [
            assessment.repayment_delay,
            assessment.miss_classification,
            assessment.exposure,
            assessment.cashflow,
        ] if s is not None and s.get("score", 0) > 20)

        entries.append(WatchlistEntry(
            customer_id=cid,
            warning_score=assessment.warning_score,
            risk_category=assessment.risk_category,
            top_indicators=assessment.top_risk_factors[:3],
            active_signals=active_signals,
            monitoring_status="Active" if assessment.assessable else "Insufficient Data",
        ))

    entries.sort(key=lambda x: -x.warning_score)

    if max_results:
        entries = entries[:max_results]

    return entries


def classify_alert(
    customer_id: str,
    assessment: CustomerAssessment,
    decision: str,
    reason: str,
    analyst_id: Optional[str] = None,
) -> AlertClassification:
    """
    Record an analyst's decision on an early warning alert.
    Preserves original score and evidence regardless of the decision.

    decision must be one of:
        "Continue Monitoring", "Requires Investigation",
        "False Positive", "Escalate for Review"
    """
    valid_decisions = {d.value for d in AlertDecision}
    if decision not in valid_decisions:
        raise ValueError(f"Invalid decision '{decision}'. Must be one of: {valid_decisions}")

    return AlertClassification(
        customer_id=customer_id,
        original_score=assessment.warning_score,
        original_category=assessment.risk_category,
        decision=decision,
        reason=reason,
        analyst_id=analyst_id,
        timestamp=pd.Timestamp.now().isoformat(),
        preserved_evidence={
            "all_indicators": assessment.all_indicators,
            "top_risk_factors": assessment.top_risk_factors,
            "rule_scores": {
                "repayment_delay": assessment.repayment_delay.get("score") if assessment.repayment_delay else None,
                "miss_classification": assessment.miss_classification.get("score") if assessment.miss_classification else None,
                "exposure": assessment.exposure.get("score") if assessment.exposure else None,
                "cashflow": assessment.cashflow.get("score") if assessment.cashflow else None,
            },
        },
    )


# --- Internal helpers ---

def _build_assessment(
    customer_id: str,
    delay: Optional[RepaymentDelaySignal],
    miss: Optional[MissClassificationSignal],
    exposure: Optional[ExposureSignal],
    history: Optional[MissingHistorySignal],
    cashflow: Optional[CashFlowSignal],
) -> CustomerAssessment:
    all_indicators = []
    scores = []

    delay_dict = asdict(delay) if delay else None
    miss_dict = asdict(miss) if miss else None
    exposure_dict = asdict(exposure) if exposure else None
    history_dict = asdict(history) if history else None
    cashflow_dict = asdict(cashflow) if cashflow else None

    if delay:
        all_indicators.extend(delay.indicators)
        scores.append(("Repayment Delays", delay.score))
    if miss:
        all_indicators.extend(miss.indicators)
        scores.append(("Miss Pattern", miss.score))
    if exposure:
        all_indicators.extend(exposure.indicators)
        scores.append(("Exposure", exposure.score))
    if cashflow:
        all_indicators.extend(cashflow.indicators)
        scores.append(("Cash Flow", cashflow.score))
    if history:
        all_indicators.extend(history.indicators)

    assessable = history.risk_assessable if history else True

    # Composite warning score (weighted average of available scores)
    if scores:
        weights = {"Repayment Delays": 0.30, "Miss Pattern": 0.20, "Exposure": 0.25, "Cash Flow": 0.25}
        total_weight = sum(weights.get(name, 0.2) for name, _ in scores)
        warning_score = sum(weights.get(name, 0.2) * s for name, s in scores) / total_weight
    else:
        warning_score = 0.0

    if not assessable:
        warning_score = min(warning_score, 30)

    # Try ML-based score if model is available
    warning_model_dict = None
    try:
        feature_dict = {
            "customer_id": customer_id,
            "rule_delay_score": delay.score if delay else 0,
            "rule_miss_score": miss.score if miss else 0,
            "rule_exposure_score": exposure.score if exposure else 0,
            "rule_completeness_score": history.completeness_score if history else 100,
            "model_cashflow_score": cashflow.score if cashflow else 0,
            "late_ratio": delay.late_count_6m / max(delay.late_count_6m + 1, 1) if delay else 0,
            "max_dpd": delay.max_dpd if delay else 0,
            "avg_days_late": delay.avg_days_late if delay else 0,
            "consecutive_misses": delay.consecutive_misses if delay else 0,
            "recovery_rate": miss.miss_frequency_ratio if miss else 1.0,
            "active_loans": exposure.active_loan_count if exposure else 0,
            "utilisation_ratio": exposure.utilisation_ratio if exposure else 0,
            "cc_utilisation": 0,
            "debt_to_income": exposure.debt_to_income_ratio if exposure else 0,
            "payment_to_income": 0,
            "months_of_history": history.months_of_history if history else 0,
        }
        model, _ = load_warning_score_model()
        ws_result = predict_warning_score(feature_dict, model)
        warning_score = ws_result.warning_score
        warning_model_dict = asdict(ws_result)
    except (FileNotFoundError, Exception):
        pass

    if warning_score < 20:
        risk_category = "Low"
    elif warning_score < 45:
        risk_category = "Medium"
    elif warning_score < 70:
        risk_category = "High"
    else:
        risk_category = "Critical"

    top_risk_factors = [name for name, s in sorted(scores, key=lambda x: -x[1]) if s > 20]

    return CustomerAssessment(
        customer_id=customer_id,
        warning_score=round(warning_score, 2),
        risk_category=risk_category,
        repayment_delay=delay_dict,
        miss_classification=miss_dict,
        exposure=exposure_dict,
        missing_history=history_dict,
        cashflow=cashflow_dict,
        warning_model=warning_model_dict,
        all_indicators=all_indicators,
        top_risk_factors=top_risk_factors,
        assessable=assessable,
    )


def _empty_assessment(customer_id: str) -> CustomerAssessment:
    return CustomerAssessment(
        customer_id=customer_id,
        warning_score=0.0,
        risk_category="Low",
        repayment_delay=None,
        miss_classification=None,
        exposure=None,
        missing_history=None,
        cashflow=None,
        warning_model=None,
        all_indicators=["No data available for assessment"],
        top_risk_factors=[],
        assessable=False,
    )


def _fallback_warning_score(feature_dict: dict) -> WarningScoreResult:
    """Rule-based fallback when ML model is not available."""
    rule_scores = [
        feature_dict.get("rule_delay_score", 0),
        feature_dict.get("rule_miss_score", 0),
        feature_dict.get("rule_exposure_score", 0),
        feature_dict.get("model_cashflow_score", 0),
    ]
    score = np.mean(rule_scores) if rule_scores else 0
    if score < 20:
        cat = "Low"
    elif score < 45:
        cat = "Medium"
    elif score < 70:
        cat = "High"
    else:
        cat = "Critical"

    return WarningScoreResult(
        customer_id=feature_dict.get("customer_id", "unknown"),
        warning_score=round(score, 2),
        risk_category=cat,
        top_contributors=[],
        rule_scores={},
        indicators=[f"Fallback scoring (no ML model): {score:.1f}/100"],
    )
