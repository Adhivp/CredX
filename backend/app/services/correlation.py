"""Capability 2: multi-signal correlation, learned from data.

Uses the deterioration model's SHAP interaction values (core/models/deterioration_model.py):
  - family effect      = sum of SHAP values of the features in that family
                         (repayment, exposure, affordability), in log-odds;
  - combined effect    = SHAP interaction values *between* families: how much the
                         model learned that signals occurring together add risk beyond
                         their separate effects (positive) or overlap (negative).
No double counting by construction: Shapley values split the model output exactly,
each feature's credit is counted once, and features describing the same underlying
event (e.g. late share and worst DPD from the same late instalments) sit in one family.
"""

from __future__ import annotations

import math

FAMILY_LABELS = {"repayment": "Repayment behaviour", "exposure": "Credit exposure", "affordability": "Affordability (instalment vs income)"}


def _p(logodds: float) -> float:
    return 1 / (1 + math.exp(-logodds))


def correlate(history: list[dict], trend: dict, base_score: float | None = None) -> dict:
    scored = [h for h in history if h["metrics"].get("deterioration")]
    if not scored:
        return {"families": [], "interactions": [], "co_moving": [], "explanation": "Model not available for this customer.",
                "deduplicated": [], "uplift": 0.0, "effective_score": base_score or (history[-1]["warning_score"] if history else 0)}
    now = scored[-1]["metrics"]["deterioration"]
    ref = (scored[-4] if len(scored) >= 4 else scored[0])["metrics"]["deterioration"]
    fams = []
    for f, v in now["family"].items():
        before = ref["family"].get(f, 0.0)
        fams.append({"family": f, "label": FAMILY_LABELS.get(f, f), "effect": round(v, 3), "effect_reference": round(before, 3),
                     "raises_risk": v > 0, "worsening": v > before})
    fams.sort(key=lambda x: -x["effect"])
    inter = [{"pair": k, "labels": [FAMILY_LABELS.get(x, x) for x in k.split("+")], "effect": round(v, 3),
              "effect_reference": round(ref["interactions"].get(k, 0.0), 3)} for k, v in now["interactions"].items()]
    inter.sort(key=lambda x: -x["effect"])
    co = [f["family"] for f in fams if f["raises_risk"] and f["worsening"]]
    joint = sum(i["effect"] for i in inter if i["effect"] > 0)
    total = now["bias"] + sum(now["family"].values()) + sum(now["interactions"].values())
    without_joint = _p(total - joint)

    parts = [f"{f['label'].lower()} {f['effect']:+.2f}" for f in fams if f["raises_risk"]]
    if len(co) >= 2 and joint > 0:
        explanation = (f"{len(co)} independent signal families are raising risk and worsening together ({', '.join(parts)} log-odds). "
                       f"The model learned from Home Credit history that this combination adds {joint:+.2f} log-odds beyond the "
                       f"separate effects: probability {now['p']:.1%} vs {without_joint:.1%} without the combined effect.")
    elif parts:
        explanation = (f"Risk is driven by {', '.join(parts)} log-odds. "
                       + (f"Combined effect between families: {joint:+.2f}." if joint > 0 else "No added risk from signals combining."))
    else:
        explanation = "No signal family raises risk above the portfolio base rate."
    return {
        "families": fams, "interactions": inter, "co_moving": co,
        "combined_effect": round(joint, 3), "probability": round(now["p"], 4),
        "probability_without_combined_effect": round(without_joint, 4),
        "explanation": explanation,
        "deduplicated": [{"event": "Model attribution", "seen_by": ["all features"],
                          "counted_as": "Shapley split: each feature's credit counted once; same-event features grouped in one family",
                          "events": []}],
        "uplift": 0.0,
        "effective_score": history[-1]["warning_score"],
        "base_score": history[-1]["warning_score"],
    }
