"""AI explainability service using DeepSeek via Nebius API."""

from __future__ import annotations

import json
import logging

from openai import OpenAI

logger = logging.getLogger(__name__)


def explain_assessment(
    customer_id: str,
    warning_score: float,
    risk_category: str,
    rule_scores: dict,
    top_indicators: list[str],
    settings: object,
) -> dict:
    """Generate a natural-language explanation for a customer's EWS assessment."""

    prompt = _build_prompt(customer_id, warning_score, risk_category, rule_scores, top_indicators)

    api_key = getattr(settings, "nebius_api_key", "")
    if not api_key:
        return _fallback_explanation(customer_id, warning_score, risk_category, rule_scores, top_indicators)

    try:
        client = OpenAI(
            base_url=getattr(settings, "nebius_base_url", ""),
            api_key=api_key,
        )
        response = client.chat.completions.create(
            model=getattr(settings, "nebius_model", "deepseek-ai/DeepSeek-V4-Flash-0731"),
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are a credit risk analyst AI assistant. Provide clear, concise, "
                        "and actionable explanations for early warning system assessments. "
                        "Explain in 3-5 sentences. Be specific about what signals are driving "
                        "the risk and what actions a relationship manager should consider. "
                        "Do not use jargon without explanation."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=1024,
        )
        explanation = response.choices[0].message.content.strip()
        return {
            "customer_id": customer_id,
            "explanation": explanation,
            "contributing_factors": [
                {"signal": k, "score": v} for k, v in rule_scores.items() if v > 0
            ],
        }
    except Exception:
        logger.exception("Nebius API call failed, using fallback explanation")
        return _fallback_explanation(customer_id, warning_score, risk_category, rule_scores, top_indicators)


def _build_prompt(
    customer_id: str,
    warning_score: float,
    risk_category: str,
    rule_scores: dict,
    top_indicators: list[str],
) -> str:
    top_score_name = max(rule_scores, key=rule_scores.get, default="unknown") if rule_scores else "unknown"
    top_score_val = rule_scores.get(top_score_name, 0)
    indicators_brief = "; ".join(top_indicators[:3]) if top_indicators else "None"

    return (
        f"Customer {customer_id}: score {warning_score:.0f}/100 ({risk_category}). "
        f"Top driver: {top_score_name.replace('_', ' ')} at {top_score_val:.0f}/100. "
        f"Signals: {indicators_brief}. "
        f"Explain the risk and recommend actions in 3-4 sentences."
    )


def _fallback_explanation(
    customer_id: str,
    warning_score: float,
    risk_category: str,
    rule_scores: dict,
    top_indicators: list[str],
) -> dict:
    top_driver = max(rule_scores, key=rule_scores.get, default="unknown") if rule_scores else "unknown"
    top_score = rule_scores.get(top_driver, 0)

    if risk_category == "Critical":
        action = "Immediate review and intervention are recommended."
    elif risk_category == "High":
        action = "Investigation by the credit risk team is recommended."
    elif risk_category == "Medium":
        action = "Continued monitoring with increased frequency is advised."
    else:
        action = "No immediate action required; standard monitoring continues."

    explanation = (
        f"Customer {customer_id} has a warning score of {warning_score:.1f}/100, "
        f"placing them in the {risk_category} risk category. "
        f"The primary risk driver is {top_driver.replace('_', ' ')} "
        f"with a score of {top_score:.1f}/100. "
    )
    if top_indicators:
        explanation += f"Key signals include: {'; '.join(top_indicators[:3])}. "
    explanation += action

    return {
        "customer_id": customer_id,
        "explanation": explanation,
        "contributing_factors": [
            {"signal": k, "score": v} for k, v in rule_scores.items() if v > 0
        ],
    }
