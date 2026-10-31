"""Grounded LLM case briefing for the investigation workspace.

The LLM only rewrites facts the system computed (model probability, SHAP family
effects, trend, delinquency, recommendations). Guard against hallucination: every
number in the reply must appear in the facts sent; otherwise the reply is rejected
and a template briefing is returned instead."""

from __future__ import annotations

import json
import logging
import re

from app.services.explainability import DEFAULT_MODEL, _client

logger = logging.getLogger(__name__)

SYSTEM = ("You are a credit-risk analyst assistant. Write a short case briefing (4-6 sentences) for a colleague "
          "using ONLY the JSON facts given. Do not invent numbers, causes or actions. Quote numbers exactly as given. "
          "Recommend only the actions listed. Reply as JSON: {\"briefing\": \"...\"}")


def _facts(customer_id: str, a: dict) -> dict:
    p, t, c = a["priority"], a["trend"], a["correlation"]
    return {
        "customer": customer_id,
        "probability_31dpd_6m_pct": None if p["probability_of_deterioration"] is None else round(p["probability_of_deterioration"] * 100, 1),
        "times_base_rate": p["probability_lift"],
        "trend": t.get("direction"), "trend_from_pct": round((t.get("p_reference") or 0) * 100, 1),
        "trend_to_pct": round((t.get("p_now") or 0) * 100, 1),
        "rbi_class": p["delinquency"]["bucket"], "days_past_due": p["delinquency"]["dpd"],
        "exposure_at_default_inr": round(p["exposure"]["ead"]), "expected_loss_inr": round(p["expected_loss"]),
        "priority_tier": p["tier"],
        "signal_effects_logodds": {f["label"]: f["effect"] for f in c.get("families", [])},
        "combined_effect_logodds": c.get("combined_effect"),
        "top_drivers": [d["label"] for d in t.get("drivers", [])[:3]],
        "recommended_actions": [r["action"] for r in a["recommendations"]],
    }


def _template(f: dict) -> str:
    s = (f"{f['customer']}: {f['probability_31dpd_6m_pct']}% probability of reaching 31+ days past due within 6 months "
         f"({f['times_base_rate']}x the base rate), trend {f['trend']} ({f['trend_from_pct']}% → {f['trend_to_pct']}%). "
         f"RBI class {f['rbi_class']} ({f['days_past_due']} days past due); exposure at default ₹{f['exposure_at_default_inr']:,}, "
         f"expected loss ₹{f['expected_loss_inr']:,}, tier {f['priority_tier']}.")
    if f["top_drivers"]:
        s += " Main drivers: " + ", ".join(f["top_drivers"]) + "."
    if f["recommended_actions"]:
        s += " Recommended: " + "; ".join(f["recommended_actions"]) + "."
    return s


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in re.findall(r"\d[\d,]*\.?\d*", text)}


def brief(customer_id: str, analysis: dict, settings) -> dict:
    f = _facts(customer_id, analysis)
    key = getattr(settings, "nebius_api_key", "")
    model = getattr(settings, "nebius_model", "") or DEFAULT_MODEL
    if key:
        try:
            r = _client(getattr(settings, "nebius_base_url", ""), key).chat.completions.create(
                model=model, temperature=0.1, max_tokens=500, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": json.dumps(f)}],
                extra_body={"chat_template_kwargs": {"thinking": False}},
            )
            raw = r.choices[0].message.content or ""
            text = str(json.loads(raw[raw.find("{"): raw.rfind("}") + 1]).get("briefing", "")).strip()
            allowed = _numbers(json.dumps(f)) | {"6", "31"}
            unknown = _numbers(text) - allowed
            if text and not unknown:
                return {"briefing": text, "source": "ai", "model": model, "facts": f}
            logger.warning("Briefing rejected: numbers not in facts: %s", unknown)
        except Exception:
            logger.exception("Briefing LLM call failed")
    return {"briefing": _template(f), "source": "template", "model": None, "facts": f}
