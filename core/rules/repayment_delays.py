"""
Rule 1: Repeated Repayment Delays Detection

Thresholds derived from Home Credit dataset (307,511 customers, 13.6M instalments).
Baseline default rate: 8.07%.

Severity bands calibrated to default rate multiples of baseline:
  NONE:     late ratio < 5%   → 6.83% default (0.85x baseline)
  LOW:      late ratio 5-10%  → 8.87% default (1.10x baseline)
  MEDIUM:   late ratio 10-20% → 9.76% default (1.21x baseline)
  HIGH:     late ratio 20-30% → 10.83% default (1.34x baseline)
  CRITICAL: late ratio > 30%  → 12.81-15.82% default (1.59-1.96x baseline)
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import List, Dict
from enum import Enum


BASELINE_DEFAULT_RATE = 0.0807


class DelaySeverity(str, Enum):
    NONE = "none"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


@dataclass
class RepaymentDelaySignal:
    customer_id: str
    severity: DelaySeverity
    score: float  # 0-100, higher = more risk
    late_count_3m: int
    late_count_6m: int
    late_count_12m: int
    consecutive_misses: int
    max_dpd: int
    avg_days_late: float
    dpd_trend: str  # "increasing", "stable", "decreasing"
    partial_payment_count: int
    indicators: List[str]


def detect_repayment_delays(repayment_history: pd.DataFrame) -> Dict[str, RepaymentDelaySignal]:
    """
    Analyse repayment history and flag customers with repeated delays.

    Expected columns in repayment_history:
        - customer_id: str
        - loan_id: str
        - due_date: datetime
        - payment_date: datetime (NaT if unpaid)
        - amount_due: float
        - amount_paid: float
        - dpd: int (days past due)
        - repayment_status: str ("on_time", "late", "missed", "partial")

    Returns dict of customer_id -> RepaymentDelaySignal
    """
    results = {}
    repayment_history = repayment_history.copy()
    repayment_history["due_date"] = pd.to_datetime(repayment_history["due_date"])
    repayment_history["payment_date"] = pd.to_datetime(repayment_history["payment_date"])

    latest_date = repayment_history["due_date"].max()

    for customer_id, group in repayment_history.groupby("customer_id"):
        group = group.sort_values("due_date")
        signal = _analyse_customer_delays(str(customer_id), group, latest_date)
        results[str(customer_id)] = signal

    return results


def _analyse_customer_delays(
    customer_id: str, history: pd.DataFrame, reference_date: pd.Timestamp
) -> RepaymentDelaySignal:
    indicators = []

    mask_3m = history["due_date"] >= (reference_date - pd.DateOffset(months=3))
    mask_6m = history["due_date"] >= (reference_date - pd.DateOffset(months=6))
    mask_12m = history["due_date"] >= (reference_date - pd.DateOffset(months=12))

    recent_3m = history[mask_3m]
    recent_6m = history[mask_6m]
    recent_12m = history[mask_12m]

    late_statuses = {"late", "missed", "partial"}

    late_count_3m = recent_3m[recent_3m["repayment_status"].isin(late_statuses)].shape[0]
    late_count_6m = recent_6m[recent_6m["repayment_status"].isin(late_statuses)].shape[0]
    late_count_12m = recent_12m[recent_12m["repayment_status"].isin(late_statuses)].shape[0]

    consecutive_misses = _count_consecutive_misses(history)

    max_dpd = int(history["dpd"].max()) if not history["dpd"].isna().all() else 0
    avg_days_late = float(history.loc[history["dpd"] > 0, "dpd"].mean()) if (history["dpd"] > 0).any() else 0.0

    dpd_trend = _calculate_dpd_trend(recent_6m)

    partial_payment_count = int(history[history["repayment_status"] == "partial"].shape[0])

    total_due = history["amount_due"].sum()
    total_paid = history["amount_paid"].sum()
    shortfall_ratio = 1 - (total_paid / total_due) if total_due > 0 else 0

    # --- Indicators backed by data thresholds ---
    # P75 of late ratio = 10.9%, so ≥2 in 3 months is already above-median risk
    if late_count_3m >= 2:
        indicators.append(f"Frequent delays in last 3 months: {late_count_3m} late/missed payments")
    if late_count_6m >= 3:
        indicators.append(f"Recurring delays in last 6 months: {late_count_6m} late/missed payments")
    if consecutive_misses >= 2:
        indicators.append(f"Consecutive missed payments: {consecutive_misses}")
    # DPD 31-60 → 10.84% default (1.34x baseline) — meaningful jump
    if max_dpd >= 31:
        indicators.append(f"Significant DPD recorded: {max_dpd} days")
    # DPD 61-90 → 14.39% default (1.78x baseline) — severe
    if max_dpd >= 61:
        indicators.append(f"Severe delinquency: {max_dpd} DPD")
    if dpd_trend == "increasing":
        indicators.append("DPD trend is increasing over recent months")
    # P75 of days late = 9, P90 = 20; avg > 10 puts customer in top quartile
    if avg_days_late > 10:
        indicators.append(f"Average delay when late: {avg_days_late:.0f} days")
    if partial_payment_count >= 2:
        indicators.append(f"Repeated partial payments: {partial_payment_count} occurrences")
    if shortfall_ratio > 0.1:
        indicators.append(f"Overall payment shortfall: {shortfall_ratio:.1%} of total due unpaid")

    score = _calculate_delay_score(
        late_count_3m=late_count_3m,
        late_count_6m=late_count_6m,
        late_count_12m=late_count_12m,
        consecutive_misses=consecutive_misses,
        max_dpd=max_dpd,
        avg_days_late=avg_days_late,
        dpd_trend=dpd_trend,
        partial_payment_count=partial_payment_count,
        shortfall_ratio=shortfall_ratio,
        total_records_6m=len(recent_6m),
    )

    severity = _score_to_severity(score)

    return RepaymentDelaySignal(
        customer_id=customer_id,
        severity=severity,
        score=round(score, 2),
        late_count_3m=late_count_3m,
        late_count_6m=late_count_6m,
        late_count_12m=late_count_12m,
        consecutive_misses=consecutive_misses,
        max_dpd=max_dpd,
        avg_days_late=round(avg_days_late, 2),
        dpd_trend=dpd_trend,
        partial_payment_count=partial_payment_count,
        indicators=indicators,
    )


def _count_consecutive_misses(history: pd.DataFrame) -> int:
    statuses = history.sort_values("due_date", ascending=False)["repayment_status"].values
    count = 0
    for status in statuses:
        if status in ("missed", "late", "partial"):
            count += 1
        else:
            break
    return count


def _calculate_dpd_trend(recent_6m: pd.DataFrame) -> str:
    if len(recent_6m) < 4:
        return "stable"
    midpoint = len(recent_6m) // 2
    first_half_avg = recent_6m.iloc[:midpoint]["dpd"].mean()
    second_half_avg = recent_6m.iloc[midpoint:]["dpd"].mean()
    # P50 days late = 4; a shift of 4+ days is meaningful
    diff = second_half_avg - first_half_avg
    if diff > 4:
        return "increasing"
    elif diff < -4:
        return "decreasing"
    return "stable"


def _calculate_delay_score(
    late_count_3m: int,
    late_count_6m: int,
    late_count_12m: int,
    consecutive_misses: int,
    max_dpd: int,
    avg_days_late: float,
    dpd_trend: str,
    partial_payment_count: int,
    shortfall_ratio: float,
    total_records_6m: int,
) -> float:
    """
    Composite delay score 0-100.

    Weight allocation derived from relative default-rate lift per factor:
      - Late ratio (30%): strongest predictor — 0.85x to 1.96x baseline
        across buckets (Home Credit: 6.83% at <5% ratio → 15.82% at >50%).
      - Max DPD (20%): DPD 61-90 shows 1.78x baseline (14.39%).
      - Consecutive misses (15%): streak ≥2 → 10.23% (1.27x baseline).
      - Avg days late (10%): 5-10d → 10.34% (1.28x); diminishing above 60d.
      - DPD trend (10%): increasing trend amplifies other signals.
      - Partial payments & shortfall (10%): underpayment is a leading indicator.
      - 12m context (5%): longer-term late count for context.
    """
    score = 0.0

    # Late ratio — most predictive factor (weight: 30%)
    # Data: 0-5% → 6.83%, 10-20% → 9.76%, 30-50% → 12.81%
    if total_records_6m > 0:
        miss_ratio_6m = late_count_6m / max(total_records_6m, 1)
        # Map ratio to score: <5% = 0, 5-10% = 8, 10-20% = 16, 20-30% = 22, >30% = 30
        if miss_ratio_6m < 0.05:
            score += 0
        elif miss_ratio_6m < 0.10:
            score += 8
        elif miss_ratio_6m < 0.20:
            score += 16
        elif miss_ratio_6m < 0.30:
            score += 22
        else:
            score += 30

    # Max DPD (weight: 20%)
    # Data: 0 → 8.17%, 1-15 → 9.39%, 31-60 → 10.84%, 61-90 → 14.39%
    if max_dpd <= 0:
        score += 0
    elif max_dpd <= 15:
        score += 4   # 9.39% — just above baseline
    elif max_dpd <= 30:
        score += 8   # 9.30% — similar to 1-15
    elif max_dpd <= 60:
        score += 14  # 10.84% — meaningful lift
    elif max_dpd <= 90:
        score += 20  # 14.39% — highest observed DPD-linked default
    else:
        score += 16  # 180+ → 8.77% — paradoxically lower (survivorship bias)

    # Consecutive misses (weight: 15%)
    # Data: streak 1 → 9.34%, streak 2 → 10.23%, streak ≥3 → 10.58%
    if consecutive_misses == 0:
        score += 0
    elif consecutive_misses == 1:
        score += 5   # 9.34% — 1.16x baseline
    elif consecutive_misses == 2:
        score += 10  # 10.23% — 1.27x baseline
    elif consecutive_misses >= 3:
        score += 15  # 10.58% — 1.31x baseline

    # Average days late (weight: 10%)
    # Data: 0-5d → 8.92%, 5-10d → 10.34%, 30-60d → 11.41%
    # P50 = 4d, P75 = 9d, P90 = 20d
    if avg_days_late <= 4:
        score += 0   # at/below median — normal
    elif avg_days_late <= 9:
        score += 3   # P50-P75 range
    elif avg_days_late <= 20:
        score += 6   # P75-P90, 10.34% default
    elif avg_days_late <= 60:
        score += 10  # 11.41% — top decile
    else:
        score += 7   # 60d+ → 7.27% — lower due to survivorship

    # DPD trend (weight: 10%)
    if dpd_trend == "increasing":
        score += 10
    elif dpd_trend == "stable" and max_dpd > 0:
        score += 3

    # Partial payments and shortfall (weight: 10%)
    score += min(partial_payment_count * 2.5, 5)
    score += min(shortfall_ratio * 50, 5)

    # 12-month context (weight: 5%)
    if late_count_12m >= 6:
        score += 5
    elif late_count_12m >= 3:
        score += 3

    return min(score, 100.0)


def _score_to_severity(score: float) -> DelaySeverity:
    """
    Severity thresholds mapped to default rate multiples:
      NONE  (<12):  ≤1.10x baseline → late ratio <5%, max DPD <15
      LOW   (12-28): 1.10-1.21x     → late ratio 5-10%, some DPD
      MEDIUM(28-50): 1.21-1.34x     → late ratio 10-20%, DPD 31-60
      HIGH  (50-72): 1.34-1.78x     → late ratio 20-30%, DPD 61-90
      CRITICAL(>72): 1.59-1.96x+    → late ratio >30%, multiple signals
    """
    if score < 12:
        return DelaySeverity.NONE
    elif score < 28:
        return DelaySeverity.LOW
    elif score < 50:
        return DelaySeverity.MEDIUM
    elif score < 72:
        return DelaySeverity.HIGH
    return DelaySeverity.CRITICAL
