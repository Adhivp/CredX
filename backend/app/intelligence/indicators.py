"""Deterministic, reproducible credit-deterioration indicators.

Each function is pure: the same snapshot gives the same output. Results carry the ids
of the records they used and a SHA-256 digest of the input values so any persisted
figure can be recomputed and verified. No LLM is involved anywhere in this module.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from statistics import fmean, pstdev

from app.intelligence.data import CustomerSnapshot, Repayment

VERSION = "1.0"
REVOLVING = {"credit_card", "overdraft", "revolving", "working_capital"}


@dataclass(frozen=True)
class Calculation:
    name: str
    version: str
    input_ids: tuple[str, ...]
    input_digest: str
    output: dict


def _digest(rows: list) -> str:
    return hashlib.sha256(json.dumps(rows, sort_keys=True, default=str).encode()).hexdigest()


def make_calculation(name: str, rows: list[tuple], ids: list[str], output: dict) -> Calculation:
    return Calculation(name, VERSION, tuple(ids), _digest([name, VERSION, rows]), _round(output))


def _round(value):
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, dict):
        return {k: _round(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_round(v) for v in value]
    return value


def _mean(values) -> float | None:
    values = list(values)
    return fmean(values) if values else None


def _slope(values: list[float]) -> float:
    """Ordinary least-squares slope against 0..n-1."""
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2
    y_mean = fmean(values)
    denominator = sum((x - x_mean) ** 2 for x in range(n))
    return sum((x - x_mean) * (y - y_mean) for x, y in enumerate(values)) / denominator


def _trailing(flags: list[bool]) -> int:
    count = 0
    for flag in reversed(flags):
        if not flag:
            break
        count += 1
    return count


def _longest(flags: list[bool]) -> int:
    best = current = 0
    for flag in flags:
        current = current + 1 if flag else 0
        best = max(best, current)
    return best


def dedupe_repayments(repayments: tuple[Repayment, ...]) -> tuple[list[Repayment], list[str]]:
    seen: set[tuple] = set()
    kept, duplicates = [], []
    for r in repayments:
        key = (r.loan_ref, r.due_date)
        if r.due_date is not None and key in seen:
            duplicates.append(r.id)
            continue
        seen.add(key)
        kept.append(r)
    return kept, duplicates


def repayment_contradictions(r: Repayment) -> list[str]:
    issues = []
    if r.status == "on_time" and r.dpd > 0:
        issues.append("on_time_status_with_dpd")
    if r.status == "on_time" and r.amount_due > 0 and r.amount_paid < 0.95 * r.amount_due:
        issues.append("on_time_status_but_underpaid")
    if r.status == "missed" and r.amount_due > 0 and r.amount_paid >= r.amount_due:
        issues.append("missed_status_but_paid_in_full")
    if r.payment_date and r.due_date and r.payment_date <= r.due_date and r.dpd > 5:
        issues.append("paid_on_time_but_dpd_recorded")
    if r.amount_due < 0 or r.amount_paid < 0:
        issues.append("negative_amount")
    if r.status not in {"on_time", "late", "missed", "partial"}:
        issues.append("unknown_status")
    return issues


def _periods(records: list[Repayment]) -> list[dict]:
    """Monthly repayment periods across all of the customer's facilities."""
    buckets: dict[str, list[Repayment]] = defaultdict(list)
    for r in records:
        if r.due_date is not None:
            buckets[r.due_date.strftime("%Y-%m")].append(r)
    periods = []
    for month in sorted(buckets):
        rows = buckets[month]
        due = sum(r.amount_due for r in rows)
        paid = sum(r.amount_paid for r in rows)
        periods.append(
            {
                "month": month,
                "delinquent": any(r.is_delinquent for r in rows),
                "missed": any(r.status == "missed" for r in rows),
                "max_dpd": max(r.dpd for r in rows),
                "shortfall": max(0.0, 1 - paid / due) if due > 0 else 0.0,
                "ids": [r.id for r in rows],
            }
        )
    return periods


def repayment_profile(snapshot: CustomerSnapshot, recent_periods: int = 3) -> Calculation:
    """Deterioration must be shown by consistent records: internally contradictory rows are
    excluded (and reported) unless a human has confirmed them."""
    deduped, duplicates = dedupe_repayments(snapshot.repayments)
    contradictory = [
        r.id
        for r in deduped
        if repayment_contradictions(r) and r.id not in snapshot.confirmed_record_ids
    ]
    excluded = set(contradictory)
    records = [r for r in deduped if r.id not in excluded]
    periods = _periods(records)
    recent, prior = periods[-recent_periods:], periods[:-recent_periods]

    def ratio(ps):
        return sum(p["delinquent"] for p in ps) / len(ps) if ps else None

    def shortfall(ps):
        return _mean(p["shortfall"] for p in ps)

    flags = [p["delinquent"] for p in periods]
    dpd_series = [p["max_dpd"] for p in periods]
    recent_ratio, prior_ratio = ratio(recent), ratio(prior)
    output = {
        "records": len(records),
        "periods": len(periods),
        "duplicates_excluded": duplicates,
        "contradictory_excluded": contradictory,
        "late_count": sum(r.status == "late" for r in records),
        "missed_count": sum(r.status == "missed" for r in records),
        "partial_count": sum(r.status == "partial" for r in records),
        "delinquent_periods": sum(flags),
        "recent_late_ratio": recent_ratio,
        "prior_late_ratio": prior_ratio,
        "late_ratio_change": (
            None if recent_ratio is None or prior_ratio is None else recent_ratio - prior_ratio
        ),
        "recent_max_dpd": max((p["max_dpd"] for p in recent), default=0),
        "prior_max_dpd": max((p["max_dpd"] for p in prior), default=0),
        "recent_mean_dpd": _mean(p["max_dpd"] for p in recent),
        "prior_mean_dpd": _mean(p["max_dpd"] for p in prior),
        "recent_shortfall_ratio": shortfall(recent),
        "prior_shortfall_ratio": shortfall(prior),
        "current_streak": _trailing(flags),
        "longest_streak": _longest(flags),
        "delinquent_in_last_4": sum(flags[-4:]),
        "clean_periods_at_end": _trailing([not f for f in flags]),
        "prior_had_delinquency": any(p["delinquent"] for p in prior),
        "dpd_slope_recent": _slope(dpd_series[-6:]),
        "dpd_series": dpd_series[-12:],
        "delinquency_series": [int(f) for f in flags[-12:]],
        "months": [p["month"] for p in periods[-12:]],
        "recent_period_ids": [i for p in recent for i in p["ids"]],
        "delinquent_record_ids": [r.id for r in records if r.is_delinquent][-12:],
    }
    rows = [
        (r.id, r.loan_ref, str(r.due_date), r.amount_due, r.amount_paid, r.dpd, r.status)
        for r in snapshot.repayments
    ] + [sorted(snapshot.confirmed_record_ids)]
    return make_calculation("repayment_profile", rows, [r.id for r in snapshot.repayments], output)


def cashflow_profile(
    snapshot: CustomerSnapshot, decline_threshold: float = 0.10, recent: int = 3
) -> Calculation:
    seen, flows, duplicates = set(), [], []
    for c in snapshot.cashflows:
        if c.month in seen:
            duplicates.append(c.id)
            continue
        seen.add(c.month)
        flows.append(c)
    inflows = [c.inflow for c in flows]
    n = len(inflows)
    rows = [(c.id, c.month, c.inflow, c.outflow) for c in snapshot.cashflows]
    ids = [c.id for c in snapshot.cashflows]
    months = [c.month for c in flows]
    gaps = sorted(set(range(min(months), max(months) + 1)) - set(months)) if months else []
    if n < 2:
        return make_calculation(
            "cashflow_profile",
            rows,
            ids,
            {"months": n, "sufficient": False, "duplicates_excluded": duplicates, "gaps": gaps},
        )

    baseline = inflows[:-recent] if n > recent else inflows[:1]
    base_mean = fmean(baseline)
    base_std = pstdev(baseline) if len(baseline) > 1 else 0.0
    recent_values = inflows[-recent:]
    recent_mean = fmean(recent_values)
    floor = base_mean * (1 - decline_threshold)
    below = [v < floor for v in inflows]
    previous = inflows[-2 * recent : -recent] if n >= 2 * recent else []
    slope_recent = _slope(recent_values) / base_mean if base_mean else 0.0
    slope_previous = _slope(previous) / base_mean if base_mean and len(previous) > 1 else 0.0
    declines = [inflows[i] < inflows[i - 1] for i in range(1, n)]
    seasonal = None
    if n >= 12 + recent:
        same_months_last_year = inflows[-12 - recent : -12]
        yoy = recent_mean / fmean(same_months_last_year) - 1 if fmean(same_months_last_year) else 0
        seasonal = {
            "yoy_change": yoy,
            "explains_decline": abs(yoy) < decline_threshold and recent_mean < floor,
        }
    dipped_earlier = any(below[:-2])
    output = {
        "months": n,
        "sufficient": n >= 2 * recent,
        "duplicates_excluded": duplicates,
        "gaps": gaps,
        "baseline_mean_inflow": base_mean,
        "baseline_cv": base_std / base_mean if base_mean else 0.0,
        "recent_mean_inflow": recent_mean,
        "recent_cv": pstdev(recent_values) / recent_mean if recent_mean else 0.0,
        "change_vs_baseline": recent_mean / base_mean - 1 if base_mean else 0.0,
        "slope_all": _slope(inflows) / fmean(inflows) if fmean(inflows) else 0.0,
        "slope_recent": slope_recent,
        "slope_previous": slope_previous,
        "acceleration": slope_recent - slope_previous,
        "months_below_baseline_streak": _trailing(below),
        "decline_streak": _trailing(declines),
        "latest_vs_previous": inflows[-1] / inflows[-2] - 1 if inflows[-2] else 0.0,
        "net_negative_recent": sum(
            1 for c in flows[-recent:] if c.outflow is not None and c.outflow > c.inflow
        ),
        "outflow_coverage": sum(c.outflow is not None for c in flows),
        "seasonality": seasonal,
        "recovered_after_dip": dipped_earlier and not below[-1] and not below[-2],
        "inflow_series": inflows[-12:],
        "month_index": months[-12:],
        "recent_record_ids": [c.id for c in flows[-recent:]],
    }
    return make_calculation("cashflow_profile", rows, ids, output)


def _utilisation_value(value: float) -> float:
    # Uploaded utilisation may be a fraction (0.85) or a percentage (85).
    return value / 100 if value > 1.5 else value


def exposure_profile(snapshot: CustomerSnapshot) -> Calculation:
    by_date: dict[str, list] = defaultdict(list)
    for e in snapshot.exposures:
        by_date[str(e.snapshot_date)].append(e)
    snapshots = []
    for key in sorted(by_date):
        rows = by_date[key]
        outstanding = sum(e.outstanding for e in rows)
        limit = sum(e.credit_limit if e.credit_limit > 0 else e.sanctioned for e in rows)
        revolving = [e for e in rows if e.facility_type in REVOLVING]
        snapshots.append(
            {
                "date": key,
                "outstanding": outstanding,
                "limit": limit,
                "utilisation": outstanding / limit if limit else 0.0,
                "revolving_utilisation": _mean(
                    _utilisation_value(e.utilisation) for e in revolving
                ),
                "obligations": sum(e.instalment for e in rows),
                "largest_share": max(e.outstanding for e in rows) / outstanding
                if outstanding
                else 0,
                "facilities": len(rows),
            }
        )
    loans_outstanding = sum(loan.outstanding for loan in snapshot.loans)
    loans_obligations = sum(loan.instalment for loan in snapshot.loans)
    latest = snapshots[-1] if snapshots else None
    earliest = snapshots[0] if snapshots else None
    if latest is None and snapshot.loans:
        sanctioned = sum(loan.sanctioned for loan in snapshot.loans)
        latest = earliest = {
            "date": None,
            "outstanding": loans_outstanding,
            "limit": sanctioned,
            "utilisation": loans_outstanding / sanctioned if sanctioned else 0.0,
            "revolving_utilisation": None,
            "obligations": loans_obligations,
            "largest_share": (
                max(loan.outstanding for loan in snapshot.loans) / loans_outstanding
                if loans_outstanding
                else 0
            ),
            "facilities": len(snapshot.loans),
        }
    output = {
        "snapshots": len(snapshots),
        "source": "exposure_snapshots" if snapshots else ("loan_facilities" if latest else "none"),
        "latest_utilisation": latest["utilisation"] if latest else None,
        "earliest_utilisation": earliest["utilisation"] if earliest else None,
        "utilisation_change": (
            latest["utilisation"] - earliest["utilisation"] if latest and earliest else None
        ),
        "revolving_utilisation": latest["revolving_utilisation"] if latest else None,
        "exposure_growth": (
            latest["outstanding"] / earliest["outstanding"] - 1
            if latest and earliest and earliest["outstanding"]
            else None
        ),
        "monthly_obligations": (latest["obligations"] if latest else 0.0) or loans_obligations,
        "largest_facility_share": latest["largest_share"] if latest else None,
        "facilities": latest["facilities"] if latest else 0,
        "exposure_at_risk": max(latest["outstanding"] if latest else 0.0, loans_outstanding),
        "series": [{"date": s["date"], "utilisation": s["utilisation"]} for s in snapshots[-12:]],
    }
    rows = [
        (
            e.id,
            e.facility_type,
            e.credit_limit,
            e.sanctioned,
            e.outstanding,
            e.instalment,
            str(e.snapshot_date),
            e.utilisation,
        )
        for e in snapshot.exposures
    ] + [(loan.id, loan.sanctioned, loan.outstanding, loan.instalment) for loan in snapshot.loans]
    ids = [e.id for e in snapshot.exposures] + [loan.id for loan in snapshot.loans]
    return make_calculation("exposure_profile", rows, ids, output)


def obligation_pressure(exposure: Calculation, cashflow: Calculation) -> Calculation:
    obligations = exposure.output.get("monthly_obligations") or 0.0
    recent = cashflow.output.get("recent_mean_inflow")
    base = cashflow.output.get("baseline_mean_inflow")
    output = {
        "monthly_obligations": obligations,
        "recent_ratio": obligations / recent if recent else None,
        "baseline_ratio": obligations / base if base else None,
    }
    if output["recent_ratio"] is not None and output["baseline_ratio"] is not None:
        output["ratio_change"] = output["recent_ratio"] - output["baseline_ratio"]
    rows = [exposure.input_digest, cashflow.input_digest]
    return make_calculation(
        "obligation_pressure", rows, list(exposure.input_ids) + list(cashflow.input_ids), output
    )


def behavioural_baseline(
    snapshot: CustomerSnapshot,
    repayment: Calculation,
    cashflow: Calculation,
    min_periods: int,
    min_months: int,
    recovery_clean_periods: int,
    recent: int = 3,
) -> Calculation:
    """Customer-relative deviation, persistence, acceleration and recovery."""
    rep = repayment.output
    dpd = rep.get("dpd_series", [])
    delinquency = rep.get("delinquency_series", [])
    base_dpd = dpd[:-recent]
    base_rate = sum(delinquency[:-recent]) / len(base_dpd) if base_dpd else None
    dpd_mean = fmean(base_dpd) if base_dpd else 0.0
    dpd_std = max(pstdev(base_dpd), 1.0) if len(base_dpd) > 1 else 1.0
    clean_baseline = base_rate is None or base_rate < 0.25
    rep_deviation = [
        (bool(flag) and clean_baseline) or d > dpd_mean + 2 * dpd_std
        for flag, d in zip(delinquency, dpd, strict=False)
    ]
    recent_dpd = dpd[-recent:]

    cf = cashflow.output
    inflows = cf.get("inflow_series", [])
    base_in = inflows[:-recent]
    in_mean = fmean(base_in) if base_in else None
    in_std = max(pstdev(base_in), 0.02 * in_mean) if in_mean and len(base_in) > 1 else None
    cash_deviation = [v < in_mean - 1.5 * in_std for v in inflows] if in_mean and in_std else []
    recent_in = inflows[-recent:]
    rep_periods, cash_months = rep.get("periods", 0), cf.get("months", 0)
    rep_persistence = _trailing(rep_deviation)
    if sum(rep_deviation[-4:]) >= 3:  # same rule the scoring model uses
        rep_persistence = max(rep_persistence, sum(rep_deviation[-4:]))
    cash_persistence = _trailing(cash_deviation)
    output = {
        "repayment_history_periods": rep_periods,
        "cashflow_history_months": cash_months,
        "sufficient_history": rep_periods >= min_periods and cash_months >= min_months,
        "baseline_delinquency_rate": base_rate,
        "baseline_mean_dpd": dpd_mean,
        "recent_dpd_z": ((fmean(recent_dpd) - dpd_mean) / dpd_std) if recent_dpd else None,
        "repayment_deviation_periods": rep_persistence,
        "baseline_mean_inflow": in_mean,
        "recent_inflow_z": (
            (fmean(recent_in) - in_mean) / in_std if in_mean and in_std and recent_in else None
        ),
        "cashflow_deviation_months": cash_persistence,
        "persistence_periods": max(rep_persistence, cash_persistence),
        "repayment_accelerating": len(recent_dpd) == recent
        and all(recent_dpd[i] > recent_dpd[i - 1] for i in range(1, recent))
        # Widening by at least 10 days across the window; trivial drift is not acceleration.
        and recent_dpd[-1] - recent_dpd[0] >= 10,
        "cashflow_accelerating": len(recent_in) == recent
        and all(recent_in[i] < recent_in[i - 1] for i in range(1, recent))
        and (cf.get("acceleration") or 0) < -0.02,
        "repayment_recovering": rep.get("prior_had_delinquency", False)
        and rep.get("clean_periods_at_end", 0) >= recovery_clean_periods,
        "cashflow_recovering": bool(cf.get("recovered_after_dip")),
        # Latest month improved while the recent window still deviates from the norm.
        "partial_recovery_signal": (cf.get("latest_vs_previous") or 0) > 0.05
        and any(cash_deviation[-recent:]),
    }
    rows = [repayment.input_digest, cashflow.input_digest, min_periods, min_months]
    return make_calculation(
        "behavioural_baseline",
        rows,
        list(repayment.input_ids) + list(cashflow.input_ids),
        output,
    )


def data_integrity(
    snapshot: CustomerSnapshot, min_periods: int, min_months: int, stale_after_days: int
) -> Calculation:
    _, rep_duplicates = dedupe_repayments(snapshot.repayments)
    months_seen, cash_duplicates = set(), []
    for c in snapshot.cashflows:
        if c.month in months_seen:
            cash_duplicates.append(c.id)
        months_seen.add(c.month)
    contradictions = [
        {"record_id": r.id, "issue": issue}
        for r in snapshot.repayments
        for issue in repayment_contradictions(r)
    ]
    for c in snapshot.cashflows:
        if c.inflow < 0:
            contradictions.append({"record_id": c.id, "issue": "negative_inflow"})
    undated = [r.id for r in snapshot.repayments if r.due_date is None]
    months = sorted(months_seen)
    gaps = sorted(set(range(months[0], months[-1] + 1)) - set(months)) if months else []
    periods = len({r.due_date.strftime("%Y-%m") for r in snapshot.repayments if r.due_date})
    latest_due = max((r.due_date for r in snapshot.repayments if r.due_date), default=None)
    staleness = (
        (snapshot.data_as_of - latest_due).days if latest_due and snapshot.data_as_of else None
    )
    delinquent = any(r.is_delinquent for r in snapshot.repayments)
    total = len(snapshot.repayments) + len(snapshot.cashflows)
    contradiction_share = len({c["record_id"] for c in contradictions}) / total if total else 0
    missing = []
    if not snapshot.repayments:
        missing.append("repayment_history")
    if not snapshot.cashflows:
        missing.append("cashflow_history")
    if not snapshot.exposures:
        missing.append("exposure_snapshots")
    if periods < 3 and len(months) < 3:
        sufficiency = "insufficient"
    elif (
        periods < min_periods
        or len(months) < min_months
        or contradiction_share > 0.10
        or (staleness is not None and staleness > stale_after_days)
    ):
        sufficiency = "limited"
    else:
        sufficiency = "sufficient"
    output = {
        "sufficiency": sufficiency,
        "repayment_periods": periods,
        "repayment_records": len(snapshot.repayments),
        "cashflow_months": len(months),
        "exposure_records": len(snapshot.exposures),
        "duplicate_repayment_ids": rep_duplicates,
        "duplicate_cashflow_ids": cash_duplicates,
        "contradictions": contradictions[:50],
        "contradiction_share": contradiction_share,
        "undated_repayment_ids": undated,
        "cashflow_gaps": gaps,
        "staleness_days": staleness,
        "stale": staleness is not None and staleness > stale_after_days,
        "missing_sources": missing,
        "new_customer": periods < min_periods and not delinquent,
        "excluded_by_human_review": sorted(snapshot.excluded_record_ids),
        "confirmed_by_human_review": sorted(snapshot.confirmed_record_ids),
    }
    rows = (
        [
            (
                r.id,
                r.loan_ref,
                str(r.due_date),
                str(r.payment_date),
                r.amount_due,
                r.amount_paid,
                r.dpd,
                r.status,
            )
            for r in snapshot.repayments
        ]
        + [(c.id, c.month, c.inflow, c.outflow) for c in snapshot.cashflows]
        + [
            min_periods,
            min_months,
            stale_after_days,
            str(snapshot.data_as_of),
            sorted(snapshot.excluded_record_ids),
            sorted(snapshot.confirmed_record_ids),
        ]
    )
    ids = [r.id for r in snapshot.repayments] + [c.id for c in snapshot.cashflows]
    return make_calculation("data_integrity", rows, ids, output)
