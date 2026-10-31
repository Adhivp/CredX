"""
Rule 2: Temporary vs Sustained Misses Classification

Thresholds derived from Home Credit dataset (30k sampled customers).
Baseline default rate: 8.07%.

Classification calibrated to streak length and recovery rate:
  TEMPORARY:    streak ≤1 AND recovery ≥80% → 7.84-9.34% default (at/below baseline)
  INCONCLUSIVE: streak 2 OR recovery 40-80% → 9.88-10.47% default (~1.3x baseline)
  SUSTAINED:    streak ≥3 OR recovery <40%  → 10.58-12.73% default (1.3-1.6x baseline)

Key insight: recovery rate is the strongest differentiator.
  Recovery 80-100% → 7.84% (below baseline — these customers self-correct)
  Recovery 20-40%  → 12.73% (1.58x baseline — persistent difficulty)
"""

import pandas as pd
import numpy as np
from dataclasses import dataclass
from typing import List, Dict
from enum import Enum


class MissClassification(str, Enum):
    NO_MISS = "no_miss"
    TEMPORARY = "temporary"
    INCONCLUSIVE = "inconclusive"
    SUSTAINED = "sustained"


@dataclass
class MissClassificationSignal:
    customer_id: str
    classification: MissClassification
    confidence: float  # 0-1 how confident we are in the classification
    score: float  # 0-100, higher = more likely sustained
    total_misses: int
    recovered_quickly: int  # paid next instalment in full
    miss_frequency_ratio: float  # misses / total payments in window
    avg_recovery_days: float  # avg days to recover after a miss
    longest_miss_streak: int
    has_clean_history_before: bool  # 6+ months clean before first miss
    indicators: List[str]


def classify_misses(repayment_history: pd.DataFrame) -> Dict[str, MissClassificationSignal]:
    """
    Classify each customer's missed payments as temporary or sustained.

    Expected columns:
        - customer_id: str
        - loan_id: str
        - due_date: datetime
        - payment_date: datetime (NaT if unpaid)
        - amount_due: float
        - amount_paid: float
        - dpd: int
        - repayment_status: str ("on_time", "late", "missed", "partial")

    Returns dict of customer_id -> MissClassificationSignal
    """
    results = {}
    repayment_history = repayment_history.copy()
    repayment_history["due_date"] = pd.to_datetime(repayment_history["due_date"])
    repayment_history["payment_date"] = pd.to_datetime(repayment_history["payment_date"])

    for customer_id, group in repayment_history.groupby("customer_id"):
        group = group.sort_values("due_date")
        signal = _classify_customer_misses(str(customer_id), group)
        results[str(customer_id)] = signal

    return results


def _classify_customer_misses(customer_id: str, history: pd.DataFrame) -> MissClassificationSignal:
    indicators = []
    miss_mask = history["repayment_status"].isin({"late", "missed", "partial"})
    total_misses = int(miss_mask.sum())

    if total_misses == 0:
        return MissClassificationSignal(
            customer_id=customer_id,
            classification=MissClassification.NO_MISS,
            confidence=1.0,
            score=0.0,
            total_misses=0,
            recovered_quickly=0,
            miss_frequency_ratio=0.0,
            avg_recovery_days=0.0,
            longest_miss_streak=0,
            has_clean_history_before=True,
            indicators=["No missed or late payments detected"],
        )

    total_payments = len(history)
    miss_frequency_ratio = total_misses / total_payments if total_payments > 0 else 0

    recovered_quickly = _count_quick_recoveries(history)
    recovery_rate = recovered_quickly / total_misses if total_misses > 0 else 0

    avg_recovery_days = _calculate_avg_recovery_days(history)

    longest_miss_streak = _longest_miss_streak(history)

    has_clean_history_before = _had_clean_history_before_misses(history, min_months=6)

    boundary_delays = _detect_boundary_delays(history)

    recency_weight = _calculate_recency_weight(history)

    miss_clustering = _analyse_miss_clustering(history)

    # --- Indicators backed by data ---
    # Recovery ≥80% → 7.84% default (below baseline) — strong self-correction signal
    if recovery_rate >= 0.8:
        indicators.append(f"High recovery rate: {recovery_rate:.0%} of misses followed by full payment")
    # Recovery <40% → 12.73% default (1.58x baseline) — poor prognosis
    if recovery_rate < 0.4 and total_misses > 1:
        indicators.append(f"Poor recovery: only {recovery_rate:.0%} of misses recovered quickly")
    # Streak ≥3 → 10.58% default (1.31x baseline)
    if longest_miss_streak >= 3:
        indicators.append(f"Extended miss streak: {longest_miss_streak} consecutive missed payments")
    if has_clean_history_before and total_misses <= 2:
        indicators.append("Previously clean repayment history — misses may be temporary")
    if boundary_delays > 0:
        indicators.append(f"{boundary_delays} delays near month-end/start — possible processing delays")
    # P95 late ratio = 32.1%; >30% is extreme
    if miss_frequency_ratio > 0.3:
        indicators.append(f"High miss frequency: {miss_frequency_ratio:.0%} of all instalments missed/late")
    # P90 days late = 20; recovery > 20d is slow
    if avg_recovery_days > 20:
        indicators.append(f"Slow average recovery: {avg_recovery_days:.0f} days to resume payment")
    if miss_clustering == "clustered":
        indicators.append("Misses are clustered together — could indicate a specific stress event")
    elif miss_clustering == "spread":
        indicators.append("Misses spread across timeline — indicates recurring difficulty")
    if recency_weight > 0.7:
        indicators.append("Most misses are recent — deterioration may be ongoing")

    score = _calculate_miss_score(
        total_misses=total_misses,
        miss_frequency_ratio=miss_frequency_ratio,
        recovery_rate=recovery_rate,
        avg_recovery_days=avg_recovery_days,
        longest_miss_streak=longest_miss_streak,
        has_clean_history_before=has_clean_history_before,
        boundary_delays=boundary_delays,
        recency_weight=recency_weight,
        miss_clustering=miss_clustering,
    )

    classification, confidence = _score_to_classification(
        score, total_misses, recovery_rate, has_clean_history_before
    )

    return MissClassificationSignal(
        customer_id=customer_id,
        classification=classification,
        confidence=round(confidence, 2),
        score=round(score, 2),
        total_misses=total_misses,
        recovered_quickly=recovered_quickly,
        miss_frequency_ratio=round(miss_frequency_ratio, 4),
        avg_recovery_days=round(avg_recovery_days, 2),
        longest_miss_streak=longest_miss_streak,
        has_clean_history_before=has_clean_history_before,
        indicators=indicators,
    )


def _count_quick_recoveries(history: pd.DataFrame) -> int:
    statuses = history["repayment_status"].values
    amounts_due = history["amount_due"].values
    amounts_paid = history["amount_paid"].values
    count = 0
    for i in range(len(statuses) - 1):
        if statuses[i] in ("late", "missed", "partial"):
            if statuses[i + 1] == "on_time" and amounts_paid[i + 1] >= amounts_due[i + 1] * 0.95:
                count += 1
    return count


def _calculate_avg_recovery_days(history: pd.DataFrame) -> float:
    miss_mask = history["repayment_status"].isin({"late", "missed", "partial"})
    missed = history[miss_mask]
    if missed.empty:
        return 0.0
    recovery_days = []
    for _, row in missed.iterrows():
        if pd.notna(row["payment_date"]) and pd.notna(row["due_date"]):
            delta = (row["payment_date"] - row["due_date"]).days
            if delta > 0:
                recovery_days.append(delta)
    return float(np.mean(recovery_days)) if recovery_days else 0.0


def _longest_miss_streak(history: pd.DataFrame) -> int:
    statuses = history.sort_values("due_date")["repayment_status"].values
    max_streak = 0
    current = 0
    for s in statuses:
        if s in ("late", "missed", "partial"):
            current += 1
            max_streak = max(max_streak, current)
        else:
            current = 0
    return max_streak


def _had_clean_history_before_misses(history: pd.DataFrame, min_months: int = 6) -> bool:
    sorted_h = history.sort_values("due_date")
    first_miss_idx = None
    for i, status in enumerate(sorted_h["repayment_status"].values):
        if status in ("late", "missed", "partial"):
            first_miss_idx = i
            break
    if first_miss_idx is None or first_miss_idx < min_months:
        return False
    pre_miss = sorted_h.iloc[:first_miss_idx]
    return not pre_miss["repayment_status"].isin({"late", "missed", "partial"}).any()


def _detect_boundary_delays(history: pd.DataFrame) -> int:
    missed = history[history["repayment_status"].isin({"late", "missed", "partial"})]
    if missed.empty:
        return 0
    count = 0
    for _, row in missed.iterrows():
        if pd.notna(row["due_date"]):
            day = row["due_date"].day
            if day >= 28 or day <= 3:
                if pd.notna(row["payment_date"]):
                    delay = (row["payment_date"] - row["due_date"]).days
                    if 0 < delay <= 5:
                        count += 1
    return count


def _calculate_recency_weight(history: pd.DataFrame) -> float:
    miss_mask = history["repayment_status"].isin({"late", "missed", "partial"})
    if not miss_mask.any():
        return 0.0
    sorted_h = history.sort_values("due_date")
    n = len(sorted_h)
    midpoint = n // 2
    first_half_misses = miss_mask.iloc[:midpoint].sum()
    second_half_misses = miss_mask.iloc[midpoint:].sum()
    total = first_half_misses + second_half_misses
    if total == 0:
        return 0.0
    return float(second_half_misses / total)


def _analyse_miss_clustering(history: pd.DataFrame) -> str:
    miss_indices = []
    sorted_h = history.sort_values("due_date").reset_index(drop=True)
    for i, status in enumerate(sorted_h["repayment_status"].values):
        if status in ("late", "missed", "partial"):
            miss_indices.append(i)
    if len(miss_indices) < 2:
        return "isolated"
    gaps = [miss_indices[i + 1] - miss_indices[i] for i in range(len(miss_indices) - 1)]
    avg_gap = np.mean(gaps)
    if avg_gap <= 2:
        return "clustered"
    elif avg_gap <= 4:
        return "mixed"
    return "spread"


def _calculate_miss_score(
    total_misses: int,
    miss_frequency_ratio: float,
    recovery_rate: float,
    avg_recovery_days: float,
    longest_miss_streak: int,
    has_clean_history_before: bool,
    boundary_delays: int,
    recency_weight: float,
    miss_clustering: str,
) -> float:
    """
    Composite miss classification score 0-100.

    Weight allocation derived from Home Credit default rate differentials:
      - Recovery rate (30%): strongest differentiator.
        80-100% recovery → 7.84% default (below baseline, self-correcting).
        20-40% recovery  → 12.73% default (1.58x baseline, persistent).
      - Streak length (25%): streak 0 → 6.70%, streak 2 → 10.23%, streak ≥3 → 10.58%.
      - Miss frequency (15%): late ratio 30-50% → 12.81% default.
      - Recency (10%): recent misses indicate ongoing deterioration.
      - Clustering (10%): spread misses → recurring; clustered → event-driven.
      - Mitigating factors (-10%): clean history, boundary delays reduce score.
    """
    score = 0.0

    # Recovery rate — inverted: poor recovery = high score (weight: 30%)
    # 80-100% recovery → 7.84% default; <20% → 9.45%; 20-40% → 12.73%
    if recovery_rate >= 0.8:
        score += 0    # below baseline — self-correcting
    elif recovery_rate >= 0.6:
        score += 10   # 9.88% — just above baseline
    elif recovery_rate >= 0.4:
        score += 20   # 10.47% — 1.3x baseline
    else:
        score += 30   # 12.73% — 1.58x baseline

    # Streak length (weight: 25%)
    # Streak 0 → 6.70%, 1 → 9.34%, 2 → 10.23%, ≥3 → 10.58%
    if longest_miss_streak == 0:
        score += 0
    elif longest_miss_streak == 1:
        score += 8    # 9.34% — 1.16x baseline
    elif longest_miss_streak == 2:
        score += 16   # 10.23% — 1.27x baseline
    elif longest_miss_streak <= 5:
        score += 22   # 10.43-10.58% — 1.30x baseline
    else:
        score += 25   # extended streak

    # Miss frequency (weight: 15%)
    # P95 late ratio = 32.1%; >30% is top 5% of customers
    score += min(miss_frequency_ratio * 50, 15)

    # Recency (weight: 10%)
    score += recency_weight * 10

    # Clustering (weight: 10%)
    if miss_clustering == "spread":
        score += 10   # recurring difficulty
    elif miss_clustering == "mixed":
        score += 5
    elif miss_clustering == "clustered":
        score += 2    # event-driven — less concerning

    # Mitigating factors (weight: -10%)
    if has_clean_history_before and total_misses <= 2:
        score -= 8
    if boundary_delays > 0 and boundary_delays >= total_misses * 0.5:
        score -= 5

    return max(0.0, min(score, 100.0))


def _score_to_classification(
    score: float, total_misses: int, recovery_rate: float, has_clean_history: bool
) -> tuple:
    """
    Classification thresholds:
      TEMPORARY (<25):     streak ≤1 + recovery ≥80% → 7.84-9.34% default
      INCONCLUSIVE (25-50): streak 2 or recovery 40-80% → 9.88-10.47%
      SUSTAINED (>50):     streak ≥3 or recovery <40% → 10.58-12.73%

    Hard override: single miss with full recovery + clean history is always TEMPORARY.
    Data shows recovery ≥80% pulls default below baseline regardless of other factors.
    """
    if total_misses == 1 and recovery_rate == 1.0 and has_clean_history:
        return MissClassification.TEMPORARY, 0.92

    if total_misses == 1 and recovery_rate == 1.0:
        return MissClassification.TEMPORARY, 0.80

    if score < 25:
        confidence = min(0.6 + (25 - score) / 60, 0.95)
        return MissClassification.TEMPORARY, confidence
    elif score < 50:
        confidence = 0.4 + abs(score - 37.5) / 50
        return MissClassification.INCONCLUSIVE, min(confidence, 0.7)
    else:
        confidence = min(0.5 + (score - 50) / 100, 0.95)
        return MissClassification.SUSTAINED, confidence
