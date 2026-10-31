"""AI explainability service using DeepSeek via Nebius (OpenAI-compatible API).

Returns a structured explanation (summary, risk factors, recommended actions) so
the UI can render sections instead of a raw markdown blob. Falls back to a
deterministic rule-based explanation when the API key is missing or the call fails.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache

from openai import OpenAI

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash-0731"
TIMEOUT_SECONDS = 20

SYSTEM_PROMPT = (
    "You are a senior credit risk analyst explaining Early Warning System (EWS) results "
    "to a relationship manager. Use only the facts given; never invent numbers. "
    "Missing data is NOT evidence of bad credit: when the customer is flagged as "
    "insufficient data, say the risk is unknown rather than high or low, and focus the "
    "actions on collecting the missing information. "
    "Respond with a single JSON object and nothing else."
)

RESPONSE_SHAPE = (
    '{"summary": "2-3 plain-English sentences on what drives this assessment", '
    '"risk_factors": ["short factor with the number behind it", ...], '
    '"recommended_actions": ["concrete next step", ...]}'
)

RULE_LABELS = {
    "repayment_delay": "Repayment delay",
    "miss_classification": "Miss pattern",
    "exposure": "Exposure",
    "data_completeness": "Data completeness",
    "cashflow": "Cash flow",
}


@lru_cache(maxsize=4)
def _client(base_url: str, api_key: str) -> OpenAI:
    return OpenAI(base_url=base_url, api_key=api_key, timeout=TIMEOUT_SECONDS, max_retries=1)


def explain_assessment(
    customer_id: str,
    warning_score: float,
    risk_category: str,
    rule_scores: dict,
    top_indicators: list[str],
    settings: object,
    facts: dict | None = None,
    insufficient_data: bool = False,
    not_evaluated: set[str] | None = None,
) -> dict:
    """Generate a structured explanation for a customer's EWS assessment.

    not_evaluated: signals that did not run for this customer (stored as 0). They are
    labelled as such so the model does not read "0/100" as "healthy".
    """
    facts = facts or {}
    not_evaluated = not_evaluated or set()
    base = {
        "customer_id": customer_id,
        "data_status": "insufficient" if insufficient_data else "assessable",
        "contributing_factors": [
            {"signal": k, "score": v} for k, v in rule_scores.items()
            if v > 0 and k != "data_completeness" and k not in not_evaluated
        ],
    }

    api_key = getattr(settings, "nebius_api_key", "")
    model = getattr(settings, "nebius_model", "") or DEFAULT_MODEL
    if api_key:
        try:
            response = _client(getattr(settings, "nebius_base_url", ""), api_key).chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _build_prompt(
                        customer_id, warning_score, risk_category, rule_scores,
                        top_indicators, facts, insufficient_data, not_evaluated,
                    )},
                ],
                temperature=0.2,
                max_tokens=700,
                response_format={"type": "json_object"},
                # DeepSeek V4 reasons by default; this task does not need it and
                # thinking roughly doubles latency.
                extra_body={"chat_template_kwargs": {"thinking": False}},
            )
            parsed = _parse(response.choices[0].message.content or "")
            return {**base, **parsed, "source": "ai", "model": model}
        except Exception:
            logger.exception("Nebius explainability call failed; using rule-based fallback")

    return {
        **base,
        **_fallback(customer_id, warning_score, risk_category, rule_scores, top_indicators, facts, insufficient_data),
        "source": "fallback",
        "model": None,
    }


def _parse(raw: str) -> dict:
    raw = raw.strip()
    data = json.loads(raw[raw.find("{"): raw.rfind("}") + 1])
    summary = str(data.get("summary") or data.get("explanation") or "").strip()
    if not summary:
        raise ValueError("empty summary")
    return {
        "explanation": summary,
        "risk_factors": [str(x) for x in data.get("risk_factors") or []][:5],
        "recommended_actions": [str(x) for x in data.get("recommended_actions") or []][:5],
    }


def _build_prompt(
    customer_id: str,
    warning_score: float,
    risk_category: str,
    rule_scores: dict,
    top_indicators: list[str],
    facts: dict,
    insufficient_data: bool,
    not_evaluated: set[str],
) -> str:
    scores = "\n".join(
        f"- {RULE_LABELS.get(k, k)}: "
        + ("not evaluated for this customer (no conclusion either way)" if k in not_evaluated
           else f"{v:.0f}/100" + (" (higher = more complete)" if k == "data_completeness" else ""))
        for k, v in rule_scores.items()
    )
    signals = "\n".join(f"- {i}" for i in top_indicators) or "- none"
    fact_lines = "\n".join(f"- {k.replace('_', ' ')}: {v}" for k, v in facts.items()) or "- none"
    status = (
        "INSUFFICIENT DATA: the file is too thin to rate. The provisional score below is "
        "not a risk rating and must not be described as low or high risk."
        if insufficient_data
        else f"Rated {risk_category} risk."
    )
    return (
        f"Customer {customer_id}\n"
        f"Status: {status}\n"
        f"Warning score: {warning_score:.0f}/100 (0 = no warning, 100 = severe)\n\n"
        f"Rule scores (0-100, higher = more risk unless noted):\n{scores}\n\n"
        f"Customer facts:\n{fact_lines}\n\n"
        f"Signals raised by the rules:\n{signals}\n\n"
        f"Return JSON exactly in this shape: {RESPONSE_SHAPE}\n"
        "Give 2-4 risk factors and 2-3 recommended actions."
    )


def _fallback(
    customer_id: str,
    warning_score: float,
    risk_category: str,
    rule_scores: dict,
    top_indicators: list[str],
    facts: dict,
    insufficient_data: bool,
) -> dict:
    if insufficient_data:
        records = facts.get("repayment_records", 0)
        return {
            "explanation": (
                f"Customer {customer_id} cannot be rated yet: only {records} repayment record(s) "
                "are on file, which is below the 6 needed for reliable pattern analysis. "
                "The risk is unknown rather than confirmed good or bad, so the provisional "
                "score should not be used for decisions."
            ),
            "risk_factors": top_indicators[:4] or ["Thin credit file"],
            "recommended_actions": [
                "Collect income evidence (pay slips or bank statements) to compute debt-to-income",
                "Request additional credit references or a bureau report",
                "Keep exposure conservative until at least 6 months of repayments accrue",
            ],
        }

    risk_scores = {k: v for k, v in rule_scores.items() if k != "data_completeness"}
    top_driver = max(risk_scores, key=risk_scores.get, default="unknown")
    actions = {
        "Critical": ["Escalate to the senior credit risk team", "Contact the customer directly", "Review collateral and recovery options"],
        "High": ["Schedule a relationship manager review", "Increase monitoring frequency"],
        "Medium": ["Monitor more frequently", "Flag for the next review cycle"],
    }.get(risk_category, ["Continue standard monitoring"])
    return {
        "explanation": (
            f"Customer {customer_id} has a warning score of {warning_score:.0f}/100 ({risk_category}). "
            f"The main driver is {RULE_LABELS.get(top_driver, top_driver).lower()} "
            f"at {risk_scores.get(top_driver, 0):.0f}/100."
        ),
        "risk_factors": top_indicators[:4] or ["No significant risk factors identified"],
        "recommended_actions": actions,
    }
