"""Capability 1: historical risk trend analysis.

`rebuild_history` replays the Level 1 pipeline (app/services/scoring.py) at each of
the last N month ends, on the data known at that date, and stores one RiskSnapshot
per customer per month: warning score, rule/model scores, the model's top SHAP
contributors and the raw indicators (repayment, delinquency, exposure, cash flow).

`analyse_trend` compares those snapshots to say whether a customer is improving,
stable or deteriorating, and which indicators moved the score.
"""

from __future__ import annotations

import json
import time

import numpy as np
import pandas as pd
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.models.ews import Customer
from app.models.monitoring import RiskSnapshot
from app.services.scoring import INSUFFICIENT_DATA, Frames, frames_as_of, load_frames, score_frames

HISTORY_PERIODS = 12
REVOLVING = {"credit_card", "credit card", "overdraft", "revolving"}

# Trend thresholds (warning-score points). A 10-point move is half a risk band
# (bands are 20-25 points wide); smaller moves are within normal month-to-month noise.
DETERIORATING_DELTA = 10.0
IMPROVING_DELTA = -10.0
SLOPE_THRESHOLD = 2.0  # points per month over the last 6 months


def _json(v) -> str:
    return json.dumps(v, default=lambda o: o.item() if hasattr(o, "item") else str(o))


def _data_end(frames: Frames) -> pd.Timestamp | None:
    dates = []
    if not frames.repayments.empty:
        dates.append(pd.to_datetime(frames.repayments["due_date"], errors="coerce").max())
    if not frames.exposure.empty:
        dates.append(frames.exposure["snapshot_date"].max())
    if not frames.cashflow.empty:
        dates.append(pd.to_datetime(frames.cashflow["record_date"].replace("", None), errors="coerce").max())
    dates = [d for d in dates if pd.notna(d)]
    return max(dates).normalize() if dates else None


def portfolio_data_end(db: Session) -> pd.Timestamp | None:
    """Latest date anywhere in the portfolio, so a single-customer rebuild uses the
    same month ends as a full one."""
    from sqlalchemy import func

    from app.models.ews import CashFlowRecord, CustomerExposure, RepaymentRecord

    vals = [
        db.execute(select(func.max(RepaymentRecord.due_date))).scalar(),
        db.execute(select(func.max(CustomerExposure.snapshot_date))).scalar(),
        db.execute(select(func.max(CashFlowRecord.record_date))).scalar(),
    ]
    dates = [pd.to_datetime(v, errors="coerce") for v in vals if v]
    dates = [d for d in dates if pd.notna(d)]
    return max(dates).normalize() if dates else None


def period_ends(data_end: pd.Timestamp, n: int = HISTORY_PERIODS) -> list[pd.Timestamp]:
    """The last n month ends up to data_end; the current (partial) month ends at data_end."""
    ends = []
    month_end = data_end + pd.offsets.MonthEnd(0)
    for i in range(n - 1, -1, -1):
        end = month_end - pd.offsets.MonthEnd(i) if i else month_end
        ends.append(min(end, data_end))
    return ends


# ── Raw indicators at a period end ───────────────────────────────────────


def _repayment_metrics(rep: pd.DataFrame, as_of: pd.Timestamp) -> dict:
    if rep.empty:
        return {"instalments": 0}
    rep = rep.copy()
    rep["due"] = pd.to_datetime(rep["due_date"], errors="coerce")
    rep["dpd"] = pd.to_numeric(rep["dpd"], errors="coerce").fillna(0)
    unpaid = rep["payment_date"].isna() & (rep["dpd"] > 0)
    w3 = rep[rep["due"] > as_of - pd.DateOffset(months=3)]
    w6 = rep[rep["due"] > as_of - pd.DateOffset(months=6)]
    month = rep[rep["due"] > as_of - pd.offsets.MonthBegin(1)]
    late6 = w6[w6["dpd"] > 0]
    latest_per_loan = rep.sort_values("due").groupby("loan_id").tail(1)
    current_dpd = int(latest_per_loan.loc[latest_per_loan["payment_date"].isna(), "dpd"].max() or 0) \
        if latest_per_loan["payment_date"].isna().any() else 0
    arrears_dpd = int(rep.loc[unpaid & (rep["due"] > as_of - pd.DateOffset(months=3)), "dpd"].max() or 0) \
        if (unpaid & (rep["due"] > as_of - pd.DateOffset(months=3))).any() else 0
    due6 = float(w6["amount_due"].sum())
    return {
        "instalments": int(len(rep)),
        "late_3m": int((w3["dpd"] > 0).sum()),
        "late_6m": int(len(late6)),
        "missed_6m": int((w6["repayment_status"] == "missed").sum()),
        "partial_6m": int((w6["repayment_status"] == "partial").sum()),
        "max_dpd_3m": int(w3["dpd"].max() or 0) if not w3.empty else 0,
        "max_dpd_6m": int(w6["dpd"].max() or 0) if not w6.empty else 0,
        "month_max_dpd": int(month["dpd"].max() or 0) if not month.empty else 0,
        "current_dpd": max(current_dpd, arrears_dpd),
        "on_time_ratio_6m": round(float((w6["dpd"] <= 0).mean()), 4) if not w6.empty else None,
        "shortfall_ratio_6m": round(1 - float(w6["amount_paid"].sum()) / due6, 4) if due6 > 0 else 0.0,
        # The same late instalment feeds both Rule 1 (delays) and Rule 2 (misses);
        # keeping the event ids lets correlation count it once.
        "late_events_6m": [f"{r.loan_id}:{r.due_date}" for r in late6.itertuples()][:24],
    }


def _exposure_metrics(exp: pd.DataFrame, loans: pd.DataFrame, as_of: pd.Timestamp) -> dict:
    if exp.empty:
        if loans.empty:
            return {}
        return {
            "total_outstanding": round(float(loans["outstanding_balance"].sum()), 2),
            "total_sanctioned": round(float(loans["sanctioned_amount"].sum()), 2),
            "facilities": int(len(loans)),
            "exposure_source": "loan_book",
        }
    latest = exp[exp["snapshot_date"] == exp["snapshot_date"].max()]
    ftype = latest["facility_type"].astype(str).str.lower()
    rev = latest[ftype.isin(REVOLVING)]
    term = latest[~ftype.isin(REVOLVING)]
    limits = rev["credit_limit"].where(rev["credit_limit"] > 0, rev["sanctioned_amount"]).sum()
    new3 = latest["disbursement_date"].notna() & (latest["disbursement_date"] > as_of - pd.DateOffset(months=3))
    return {
        "total_outstanding": round(float(latest["outstanding_balance"].sum()), 2),
        "total_sanctioned": round(float(latest["sanctioned_amount"].sum()), 2),
        "term_utilisation": round(float(term["outstanding_balance"].sum() / term["sanctioned_amount"].sum()), 4)
        if term["sanctioned_amount"].sum() > 0 else None,
        "revolving_utilisation": round(float(rev["outstanding_balance"].sum() / limits), 4) if limits > 0 else None,
        "revolving_outstanding": round(float(rev["outstanding_balance"].sum()), 2),
        "facilities": int(len(latest)),
        "new_facilities_3m": int(new3.sum()),
        "monthly_obligation": round(float(latest["instalment_amount"].sum()), 2),
        "facility_types": sorted(set(ftype)),
        "exposure_source": "snapshot",
    }


def _cashflow_metrics(cf: pd.DataFrame) -> dict:
    if cf.empty:
        return {}
    s = cf.sort_values("month")["monthly_inflow"].astype(float).to_numpy()
    last3 = s[-3:].mean()
    prev3 = s[-6:-3].mean() if len(s) >= 6 else (s[:-3].mean() if len(s) > 3 else None)
    first3 = s[:3].mean()
    return {
        "months_of_cashflow": int(len(s)),
        "avg_inflow_3m": round(float(last3), 2),
        "inflow_change_3m": round(float((last3 - prev3) / prev3), 4) if prev3 else None,
        "inflow_change_total": round(float((last3 - first3) / first3), 4) if first3 and len(s) >= 6 else None,
    }


def customer_metrics(frames: Frames, cid: str, as_of: pd.Timestamp) -> dict:
    def mine(df):
        return df[df["customer_id"].astype(str) == cid] if not df.empty else df

    m = {}
    m.update(_repayment_metrics(mine(frames.repayments), as_of))
    m.update(_exposure_metrics(mine(frames.exposure), mine(frames.loans), as_of))
    m.update(_cashflow_metrics(mine(frames.cashflow)))
    inflow = m.get("avg_inflow_3m")
    if inflow and m.get("total_outstanding") is not None:
        m["debt_to_income"] = round(m["total_outstanding"] / (inflow * 12), 4)
    if inflow and m.get("monthly_obligation"):
        m["payment_to_income"] = round(m["monthly_obligation"] / inflow, 4)
    return m


EPOCH = pd.Timestamp("1970-01-01")


def _days(s) -> pd.Series:
    return (pd.to_datetime(s, errors="coerce") - EPOCH).dt.days.astype(float)


def deterioration_scores(frames: Frames, as_of: pd.Timestamp) -> dict:
    """Model 3 (core/models/deterioration_model.py) on the platform data known at as_of:
    probability of SMA-1 within 6 months + SHAP family contributions and interactions."""
    try:
        from core.models import deterioration_model as dm
        dm.load()
    except Exception:
        return {}
    ids = frames.profiles["customer_id"].astype(str)
    t = pd.Series(float((as_of - EPOCH).days), index=pd.Index(ids, name="customer_id"))
    rep = frames.repayments
    inst = pd.DataFrame({
        "customer_id": rep["customer_id"].astype(str), "due": _days(rep["due_date"]),
        "paid": _days(rep["payment_date"]), "amount_due": rep["amount_due"].astype(float),
        "amount_paid": rep["amount_paid"].astype(float),
    }) if not rep.empty else pd.DataFrame()
    cards = pd.DataFrame()
    exp = frames.exposure
    if not exp.empty:
        rev = exp[exp["facility_type"].astype(str).str.lower().isin(REVOLVING)]
        if not rev.empty:
            cards = pd.DataFrame({
                "customer_id": rev["customer_id"].astype(str), "day": _days(rev["snapshot_date"]),
                "balance": rev["outstanding_balance"].astype(float),
                "limit": rev["credit_limit"].where(rev["credit_limit"] > 0, rev["sanctioned_amount"]).astype(float),
            })
    income = pd.Series(dtype=float)
    if not frames.cashflow.empty:
        income = (frames.cashflow.sort_values("month").groupby(frames.cashflow["customer_id"].astype(str))
                  ["monthly_inflow"].apply(lambda v: v.tail(3).mean()))
    X = dm.snapshot_features(inst, cards, income, t)
    res = dm.explain(X)
    return {cid: {**r, "values": {k: (None if pd.isna(v) else round(float(v), 4)) for k, v in X.loc[cid].items()}}
            for cid, r in zip(X.index, res)}


def _has_data(frames: Frames, cid: str) -> bool:
    return any(
        not df.empty and (df["customer_id"].astype(str) == cid).any()
        for df in (frames.repayments, frames.exposure, frames.cashflow)
    )


# ── Rebuild ──────────────────────────────────────────────────────────────


def rebuild_history(db: Session, customer_ids: list[str] | None = None, periods: int = HISTORY_PERIODS) -> dict:
    """Recompute the month-end risk history for these customers (all when None)."""
    frames = load_frames(db, customer_ids)
    if frames.profiles.empty:
        return {"periods": [], "snapshots": 0}
    end = portfolio_data_end(db) or _data_end(frames)
    if end is None:
        return {"periods": [], "snapshots": 0}
    ids = {c.customer_id: c.id for c in db.execute(
        select(Customer).where(Customer.customer_id.in_(frames.profiles["customer_id"].astype(str).tolist()))
    ).scalars().all()}

    rows = []
    ends = period_ends(end, periods)
    for as_of in ends:
        cut = frames_as_of(frames, as_of)
        scored = score_frames(cut)
        det = deterioration_scores(cut, as_of)
        for cid, s in scored.items():
            if cid not in ids or not _has_data(cut, cid):
                continue
            metrics = customer_metrics(cut, cid, as_of)
            if s.miss is not None:
                metrics["miss_classification"] = str(getattr(s.miss.classification, "value", s.miss.classification))
            if s.cashflow is not None:
                metrics["cashflow_pct_change_6m"] = s.cashflow.pct_change_6m
            if s.history is not None:
                metrics["months_of_history"] = s.history.months_of_history
            if cid in det:
                metrics["deterioration"] = det[cid]
            rows.append(RiskSnapshot(
                customer_id=ids[cid],
                period=as_of.strftime("%Y-%m"),
                period_end=as_of.strftime("%Y-%m-%d"),
                default_probability=s.model.default_probability if s.model else None,
                metrics=_json(metrics),
                model_contributors=_json(s.model.top_contributors if s.model else []),
                indicators=_json(s.indicators[:8]),
                data_notes=_json(cut.notes.get(cid, [])),
                computed_at=int(time.time()),
                **s.row(),
            ))

    db.execute(delete(RiskSnapshot).where(RiskSnapshot.customer_id.in_(list(ids.values()))))
    db.add_all(rows)
    db.commit()
    return {"periods": [e.strftime("%Y-%m") for e in ends], "snapshots": len(rows)}


# ── Read + analyse ───────────────────────────────────────────────────────


def snapshot_dict(s: RiskSnapshot) -> dict:
    return {
        "period": s.period,
        "period_end": s.period_end,
        "warning_score": s.warning_score,
        "risk_category": s.risk_category,
        "default_probability": s.default_probability,
        "scores": {
            "delay": s.rule_delay_score, "miss": s.rule_miss_score, "exposure": s.rule_exposure_score,
            "completeness": s.rule_completeness_score, "cashflow": s.cashflow_score,
        },
        "metrics": json.loads(s.metrics or "{}"),
        "model_contributors": json.loads(s.model_contributors or "[]"),
        "indicators": json.loads(s.indicators or "[]"),
        "data_notes": json.loads(s.data_notes or "[]"),
    }


def load_history(db: Session, internal_ids: list[str] | None = None, up_to: str | None = None) -> dict[str, list[dict]]:
    """internal customer id -> snapshots (oldest first), optionally only up to a period."""
    q = select(RiskSnapshot).order_by(RiskSnapshot.period)
    if internal_ids is not None:
        q = q.where(RiskSnapshot.customer_id.in_(internal_ids))
    if up_to:
        q = q.where(RiskSnapshot.period <= up_to)
    out: dict[str, list[dict]] = {}
    for s in db.execute(q).scalars().all():
        out.setdefault(s.customer_id, []).append(snapshot_dict(s))
    return out


def _logit(p: float) -> float:
    p = min(max(p, 1e-4), 1 - 1e-4)
    return float(np.log(p / (1 - p)))


def _model_meta() -> dict:
    try:
        from core.models import deterioration_model as dm
        m = dm.load()
        return {"cuts": m["trend_cuts"], "base_rate": m["base_rate"], "labels": dm.LABELS, "family": dm.FAMILY}
    except Exception:
        return {}


def analyse_trend(history: list[dict]) -> dict:
    """Direction from the deterioration model's probability over time.

    Cut points on the 3-month change in log-odds were learned from Home Credit
    (core/models/deterioration_model.py: decision tree vs. the actual outcome);
    improving mirrors the first cut. Drivers = features whose SHAP contribution
    changed most between the two months (no hand-set materiality thresholds)."""
    meta = _model_meta()
    if not history:
        return {"direction": "no_history", "summary": "No risk history yet. Run an assessment.", "periods": 0}
    latest = history[-1]
    scored = [h for h in history if h["metrics"].get("deterioration")]
    if latest["risk_category"] == INSUFFICIENT_DATA or not scored or not meta:
        return {"direction": "insufficient_data", "periods": len(history), "latest_period": latest["period"],
                "summary": "Not enough data to establish a trend.", "drivers": [], "series": []}
    if len(scored) < 2:
        return {"direction": "insufficient_history", "periods": len(history), "latest_period": latest["period"],
                "summary": "Only one scored month so far.", "drivers": [], "series": []}
    now = scored[-1]
    ref = scored[-4] if len(scored) >= 4 else scored[0]
    p_now, p_ref = now["metrics"]["deterioration"]["p"], ref["metrics"]["deterioration"]["p"]
    delta = _logit(p_now) - _logit(p_ref)
    c1, c2 = meta["cuts"][0], meta["cuts"][-1]
    if delta >= c2:
        direction, strength = "deteriorating", "sharply"
    elif delta >= c1:
        direction, strength = "deteriorating", ""
    elif delta <= -c1:
        direction, strength = "improving", ""
    else:
        direction, strength = "stable", ""

    fa, fb = now["metrics"]["deterioration"]["features"], ref["metrics"]["deterioration"]["features"]
    va, vb = now["metrics"]["deterioration"]["values"], ref["metrics"]["deterioration"]["values"]
    drivers = []
    for f in fa:
        ch = fa[f] - fb.get(f, 0.0)
        if ch:
            drivers.append({"metric": f, "label": meta["labels"][f], "family": meta["family"][f],
                            "before": vb.get(f), "after": va.get(f), "contribution_change": round(ch, 4),
                            "direction": "worsening" if ch > 0 else "improving"})
    drivers.sort(key=lambda d: -abs(d["contribution_change"]))
    drivers = drivers[:6]

    elevated_run = 0
    for h in reversed(scored):
        if h["metrics"]["deterioration"]["p"] > meta["base_rate"]:
            elevated_run += 1
        else:
            break
    worse = [d["label"] for d in drivers if d["direction"] == "worsening"][:3]
    better = [d["label"] for d in drivers if d["direction"] == "improving"][:2]
    summary = (f"Probability of reaching 31+ DPD within 6 months moved from {p_ref:.1%} ({ref['period']}) "
               f"to {p_now:.1%} ({now['period']}): {(strength + ' ') if strength else ''}{direction}.")
    if worse:
        summary += " Pushing risk up: " + ", ".join(worse) + "."
    if better:
        summary += " Pulling risk down: " + ", ".join(better) + "."
    return {
        "direction": direction, "strength": strength, "summary": summary, "periods": len(history),
        "latest_period": now["period"], "reference_period": ref["period"],
        "p_now": round(p_now, 4), "p_reference": round(p_ref, 4),
        "delta_logodds": round(delta, 3), "learned_cuts": [round(c1, 3), round(c2, 3)],
        "delta_3m": round(latest["warning_score"] - ref["warning_score"], 2),
        "above_base_rate_months": elevated_run,
        "series": [{"period": h["period"], "p": h["metrics"]["deterioration"]["p"]} for h in scored],
        "drivers": drivers,
    }
