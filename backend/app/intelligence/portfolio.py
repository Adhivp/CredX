"""Portfolio-level deterioration clusters and concentration.

Output is aggregate-only: no customer identifiers are returned, and any group smaller
than ``portfolio_min_cell_size`` is suppressed so individual customers cannot be
inferred from a small cell.
"""

from __future__ import annotations

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.intelligence import indicators
from app.intelligence.data import load_customer_snapshot
from app.intelligence.scoring import evaluate_snapshot
from app.models.ews import Customer
from app.services.policy import PolicyConfig

DETERIORATING = {
    "emerging_deterioration",
    "sustained_deterioration",
    "accelerating_deterioration",
}
MAX_CUSTOMERS = 5000


def portfolio_profile(
    db: Session, customer_condition, policy: PolicyConfig, segment: str | None = None
) -> indicators.Calculation:
    query = select(Customer).where(customer_condition).order_by(Customer.customer_id)
    if segment:
        query = query.where(Customer.portfolio_segment == segment)
    customers = db.scalars(query.limit(MAX_CUSTOMERS)).all()
    rows = []
    for customer in customers:
        snapshot = load_customer_snapshot(db, customer.id)
        evaluation = evaluate_snapshot(snapshot, policy)
        a = evaluation.assessment
        exposure = evaluation.calculations["exposure_profile"].output.get("exposure_at_risk") or 0
        cash = evaluation.calculations["cashflow_profile"].output
        rows.append(
            {
                "segment": customer.portfolio_segment or "unspecified",
                "product": snapshot.loans[0].product_type if snapshot.loans else "none",
                "employment": customer.employment_category or "unspecified",
                "classification": a.classification,
                "score": a.risk_score,
                "exposure": float(exposure),
                "cash_declining": (cash.get("change_vs_baseline") or 0)
                < -policy.scoring.cashflow_decline_threshold,
                "digest": evaluation.calculations["data_integrity"].input_digest,
            }
        )
    min_cell = policy.data.portfolio_min_cell_size
    total = len(rows)
    total_exposure = sum(r["exposure"] for r in rows)
    portfolio_share = (
        sum(r["classification"] in DETERIORATING for r in rows) / total if total else 0.0
    )

    def groups(key: str) -> list[dict]:
        buckets: dict[str, list[dict]] = defaultdict(list)
        for r in rows:
            buckets[r[key]].append(r)
        out = []
        for name in sorted(buckets):
            members = buckets[name]
            if len(members) < min_cell:
                out.append({"group": name, "suppressed": True, "reason": "below_min_cell_size"})
                continue
            deteriorating = [m for m in members if m["classification"] in DETERIORATING]
            scored = [m["score"] for m in members if m["score"] is not None]
            share = len(deteriorating) / len(members)
            out.append(
                {
                    "group": name,
                    "suppressed": False,
                    "customers": len(members),
                    "deteriorating": len(deteriorating),
                    "deteriorating_share": share,
                    "insufficient_evidence": sum(
                        m["classification"] == "insufficient_evidence" for m in members
                    ),
                    "exposure": sum(m["exposure"] for m in members),
                    "deteriorating_exposure": sum(m["exposure"] for m in deteriorating),
                    "exposure_share": (
                        sum(m["exposure"] for m in members) / total_exposure
                        if total_exposure
                        else 0
                    ),
                    "mean_score": sum(scored) / len(scored) if scored else None,
                    "cash_declining_share": sum(m["cash_declining"] for m in members)
                    / len(members),
                    "cluster": share >= portfolio_share + 0.15 and len(deteriorating) >= 2,
                }
            )
        return out

    segments = groups("segment")
    shares = [g["exposure_share"] for g in segments if not g["suppressed"]]
    output = {
        "customers": total,
        "suppressed_below": min_cell,
        "portfolio_deteriorating_share": portfolio_share,
        "total_exposure": total_exposure,
        "segments": segments,
        "products": groups("product"),
        "employment": groups("employment"),
        "segment_exposure_hhi": sum(s * s for s in shares),
        "clusters": [g["group"] for g in segments if not g["suppressed"] and g["cluster"]],
    }
    return indicators.make_calculation(
        "portfolio_profile",
        sorted(r["digest"] for r in rows) + [segment or "", min_cell],
        [],
        output,
    )
