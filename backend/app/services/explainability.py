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
            max_tokens=500,
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
    indicators_text = "\n".join(f"- {ind}" for ind in top_indicators) if top_indicators else "- None"
    scores_text = "\n".join(f"- {k}: {v:.1f}/100" for k, v in rule_scores.items())

    return (
        f"Explain the following Early Warning System assessment for customer {customer_id}:\n\n"
        f"Warning Score: {warning_score:.1f}/100 (Risk Category: {risk_category})\n\n"
        f"Rule Scores:\n{scores_text}\n\n"
        f"Active Warning Indicators:\n{indicators_text}\n\n"
        "Provide a clear explanation of why this customer's risk level is what it is, "
        "which factors are the primary drivers, and what actions might be appropriate."
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
