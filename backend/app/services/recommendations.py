"""Capability 4: intervention recommendations without hand-set thresholds.

Which scenario applies is decided by evidence the system already derives from data:
  - the deterioration model's SHAP attribution: a signal family recommends its action
    only when it pushes this customer's risk up (contribution > 0);
  - Level 1 Rule 2 (temporary vs sustained misses; thresholds derived from Home
    Credit in core/rules/rules.md) separates a one-off miss from repeated delays;
  - the cash-flow model (Model 1) above its training base rate;
  - RBI SMA classes for delinquency (SMA-1+ escalation, SMA-2/NPA restructuring review);
  - Level 1 Rule 4 (data completeness) for insufficient information.
Scenario -> action follows the table in the Level 2 brief. Target date defaults to
30 days (RBI prudential framework review period); analysts can change it. Nothing here
acts on an account: customer-impacting actions need a second user's approval.
"""

from __future__ import annotations

import json
from pathlib import Path

PLAYBOOK = {
    "temporary_missed_instalment": {"action": "Monitor repayment recovery and verify payment status",
                                    "owner_role": "relationship_manager", "customer_impacting": False, "requires_approval": False},
    "repeated_repayment_delays": {"action": "Contact customer and assess repayment difficulties",
                                  "owner_role": "relationship_manager", "customer_impacting": False, "requires_approval": False},
    "declining_cash_flow": {"action": "Initiate financial health review",
                            "owner_role": "risk_analyst", "customer_impacting": False, "requires_approval": False},
    "increasing_exposure": {"action": "Review affordability and overall obligations",
                            "owner_role": "risk_analyst", "customer_impacting": False, "requires_approval": False},
    "sustained_high_risk": {"action": "Escalate to credit risk or collections team",
                            "owner_role": "collections", "customer_impacting": True, "requires_approval": True},
    "restructuring_review": {"action": "Prepare restructuring / credit-limit review for credit committee decision",
                             "owner_role": "credit_committee", "customer_impacting": True, "requires_approval": True},
    "insufficient_information": {"action": "Request additional financial or repayment evidence",
                                 "owner_role": "risk_analyst", "customer_impacting": False, "requires_approval": False},
}
for v in PLAYBOOK.values():
    v["target_days"] = 30

SCENARIO_LABELS = {
    "temporary_missed_instalment": "Temporary missed instalment",
    "repeated_repayment_delays": "Repeated repayment delays",
    "declining_cash_flow": "Declining income or cash flow",
    "increasing_exposure": "Increasing credit exposure",
    "sustained_high_risk": "Sustained high-risk deterioration",
    "restructuring_review": "Severe delinquency with income stress",
    "insufficient_information": "Insufficient information",
}
URGENCY = {"P1": 0, "P2": 1, "P3": 2, "P4": 3}


def _cashflow_base_rate() -> float | None:
    try:
        p = Path(__file__).resolve().parents[3] / "core" / "models" / "saved" / "cashflow_model.metrics.json"
        return json.loads(p.read_text())["declining_share"]
    except Exception:
        return None


def _rec(code: str, priority: str, rationale: str, evidence: list[str]) -> dict:
    p = PLAYBOOK[code]
    return {"scenario": code, "scenario_label": SCENARIO_LABELS[code], "action": p["action"], "owner_role": p["owner_role"],
            "target_days": p["target_days"], "customer_impacting": p["customer_impacting"],
            "requires_approval": p["requires_approval"], "priority": priority, "rationale": rationale, "evidence": evidence}


def recommend(history: list[dict], trend: dict, correlation: dict, priority: dict, excluded_events: int = 0) -> list[dict]:
    tier = priority.get("tier", "P3")
    if not history:
        return [_rec("insufficient_information", tier, "No assessment history exists.", ["No risk snapshot on file"])]
    latest = history[-1]
    m, sc = latest["metrics"], latest["scores"]
    det = m.get("deterioration") or {}
    fam = det.get("family", {})
    vals = det.get("values", {})
    dq = priority.get("delinquency", {})
    recs = []

    if latest["risk_category"] == "Insufficient Data" or not det:
        gaps = [f"Data completeness {sc['completeness']:.0f}/100 (Rule 4)"]
        if not m.get("months_of_cashflow"):
            gaps.append("No cash-flow / income data on file")
        if not m.get("exposure_source"):
            gaps.append("No exposure snapshot on file")
        recs.append(_rec("insufficient_information", tier, "Rule 4 cannot rate this customer on the data held.", gaps))

    if dq.get("bucket") in ("SMA-1", "SMA-2", "NPA") and trend.get("direction") != "improving":
        recs.append(_rec("sustained_high_risk", tier,
                         f"Account is {dq['bucket']} ({dq['dpd']} days past due) and not improving.",
                         [f"RBI class {dq['bucket']}", f"Trend: {trend.get('direction')}",
                          f"P(31+ DPD in 6 months) {priority.get('probability_of_deterioration') or 0:.1%}"]))
        cf = sc["cashflow"] / 100
        cf_base = _cashflow_base_rate()
        if dq["bucket"] in ("SMA-2", "NPA") and ((cf_base and cf > cf_base) or fam.get("affordability", 0) > 0):
            recs.append(_rec("restructuring_review", tier,
                             "Severe delinquency together with income stress; resolution is a credit-committee decision.",
                             [f"RBI class {dq['bucket']}", f"Cash-flow model probability {cf:.0%} (base {cf_base:.0%})" if cf_base else "",
                              f"Affordability effect {fam.get('affordability', 0):+.2f} log-odds"]))

    if fam.get("repayment", 0) > 0:
        miss = (m.get("miss_classification") or "").lower()
        ev = [f"Repayment family raises risk {fam['repayment']:+.2f} log-odds",
              f"Late share (3 months) {vals.get('late_ratio_3m') if vals.get('late_ratio_3m') is not None else '–'}",
              f"Worst DPD (3 months) {vals.get('max_dpd_3m') if vals.get('max_dpd_3m') is not None else '–'}"]
        if miss:
            ev.append(f"Rule 2 classifies the misses as {miss}")
        if excluded_events:
            ev.append(f"{excluded_events} event(s) reclassified by an analyst and excluded")
        if miss == "temporary":
            recs.append(_rec("temporary_missed_instalment", tier, "Rule 2 sees a temporary miss: verify status before treating it as a problem.", ev))
        else:
            recs.append(_rec("repeated_repayment_delays", tier, "Repayment behaviour is raising risk and is not classed as temporary.", ev))

    cf = sc["cashflow"] / 100
    cf_base = _cashflow_base_rate()
    if fam.get("affordability", 0) > 0 or (cf_base and cf > cf_base):
        ev = []
        if fam.get("affordability", 0) > 0:
            ev.append(f"Instalment-to-income raises risk {fam['affordability']:+.2f} log-odds (value {vals.get('instalment_to_income')})")
        if cf_base and cf > cf_base:
            ev.append(f"Cash-flow model: {cf:.0%} probability of sustained inflow decline (base rate {cf_base:.0%})")
        recs.append(_rec("declining_cash_flow", tier, "Income or affordability is pushing risk up.", ev))

    if fam.get("exposure", 0) > 0:
        recs.append(_rec("increasing_exposure", tier, "Card / revolving usage is pushing risk up.",
                         [f"Exposure family raises risk {fam['exposure']:+.2f} log-odds",
                          f"Card utilisation {vals.get('card_util')}, 3-month change {vals.get('card_util_change')}"]))

    for r in recs:
        r["evidence"] = [e for e in r["evidence"] if e]
    recs.sort(key=lambda r: not r["requires_approval"])
    return recs
