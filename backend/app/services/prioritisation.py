"""Capability 3: prioritisation, with probability and impact kept separate.

probability  - deterioration model: P(31+ DPD within 6 months), trained on Home Credit.
impact       - exposure at default = outstanding + CCF x undrawn card/overdraft limit,
               CCF measured on Home Credit cards before they went 30+ DPD.
               (No recovery data is available, so no loss-given-default is assumed.)
expected     - probability x exposure at default. The queue is ranked on it.
tiers        - natural breaks (1-D k-means) of log expected loss across the portfolio.
quadrant     - probability above the training base rate x EAD above the portfolio median.
delinquency  - RBI SMA classes: SMA-0 1-30, SMA-1 31-60, SMA-2 61-90, NPA > 90 days.
"""

from __future__ import annotations

import numpy as np


def _meta() -> dict:
    try:
        from core.models import deterioration_model as dm
        m = dm.load()
        return {"ccf": m["ccf"], "base_rate": m["base_rate"]}
    except Exception:
        return {"ccf": 0.0, "base_rate": None}


def delinquency_status(metrics: dict) -> dict:
    dpd = int(max(metrics.get("current_dpd", 0) or 0, metrics.get("month_max_dpd", 0) or 0))
    if dpd <= 0:
        bucket = "Standard"
    elif dpd <= 30:
        bucket = "SMA-0"
    elif dpd <= 60:
        bucket = "SMA-1"
    elif dpd <= 90:
        bucket = "SMA-2"
    else:
        bucket = "NPA"
    return {"dpd": dpd, "bucket": bucket, "delinquent": dpd > 0}


def exposure_at_default(metrics: dict, facilities: list[dict]) -> dict:
    ccf = _meta()["ccf"]
    ead = outstanding = undrawn_total = 0.0
    for f in facilities:
        ftype = (f.get("facility_type") or "").lower()
        bal = float(f.get("outstanding_balance") or 0)
        undrawn = 0.0
        if ftype in ("credit_card", "overdraft", "revolving"):
            undrawn = max(0.0, float(f.get("credit_limit") or f.get("sanctioned_amount") or 0) - bal)
        ead += bal + ccf * undrawn
        outstanding += bal
        undrawn_total += undrawn
    if not facilities and metrics.get("total_outstanding"):
        ead = outstanding = float(metrics["total_outstanding"])
    return {"outstanding": round(outstanding, 2), "undrawn": round(undrawn_total, 2), "ccf": round(ccf, 4), "ead": round(ead, 2)}


def prioritise(latest: dict, trend: dict, correlation: dict, facilities: list[dict],
               model_probability: float | None, corrected_category: str | None = None) -> dict:
    det = latest["metrics"].get("deterioration") or {}
    p = det.get("p")
    exp = exposure_at_default(latest["metrics"], facilities)
    base = _meta()["base_rate"]
    return {
        "probability_of_deterioration": round(p, 4) if p is not None else None,
        "probability_lift": round(p / base, 2) if p is not None and base else None,
        "base_rate": base,
        "exposure": exp,
        "expected_loss": round((p or 0) * exp["ead"], 2),
        "loss_if_default": exp["ead"],
        "delinquency": delinquency_status(latest["metrics"]),
        "warning_level": corrected_category or latest["risk_category"],
        "effective_score": latest["warning_score"],
        "insufficient_data": latest["risk_category"] == "Insufficient Data" or p is None,
        # filled by assign_tiers() once the whole portfolio is known
        "priority_score": 0.0, "tier": "P4", "tier_label": "", "quadrant": {"key": "", "label": "", "description": ""},
        "components": {},
    }


QUADRANTS = {
    (True, True): ("intervene_now", "Intervene now", "Above-base-rate probability, above-median exposure"),
    (True, False): ("resolve_quickly", "Resolve quickly", "Above-base-rate probability, smaller exposure"),
    (False, True): ("protect_exposure", "Protect exposure", "Lower probability, above-median exposure"),
    (False, False): ("routine", "Routine monitoring", "Lower probability, smaller exposure"),
}
TIER_LABELS = ["Immediate", "This week", "This month", "Routine"]


def assign_tiers(priorities: list[dict]) -> None:
    """Natural breaks of log(expected loss) across the portfolio -> P1..P4 (highest = P1)."""
    if not priorities:
        return
    el = np.array([np.log1p(x["expected_loss"]) for x in priorities])
    k = min(4, len(set(np.round(el, 6))))
    labels = np.zeros(len(el), dtype=int)
    if k > 1:
        from sklearn.cluster import KMeans
        km = KMeans(n_clusters=k, n_init=10, random_state=0).fit(el.reshape(-1, 1))
        order = np.argsort(-km.cluster_centers_.ravel())
        rank = {c: r for r, c in enumerate(order)}
        labels = np.array([rank[c] for c in km.labels_])
    median_ead = float(np.median([x["exposure"]["ead"] for x in priorities]))
    max_el = max(el.max(), 1e-9)
    for x, lab, e in zip(priorities, labels, el):
        x["tier"], x["tier_label"] = f"P{lab + 1}", TIER_LABELS[lab]
        x["priority_score"] = round(100 * e / max_el, 1)
        high_p = x["probability_lift"] is not None and x["probability_lift"] > 1
        key, label, desc = QUADRANTS[(high_p, x["exposure"]["ead"] > median_ead)]
        x["quadrant"] = {"key": key, "label": label, "description": desc}
        x["components"] = {"median_ead": round(median_ead, 2)}
