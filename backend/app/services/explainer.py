"""
AI Explainability Service — DeepSeek via Nebius API
Generates natural language explanations of EWS customer risk assessments.
"""

import json
from openai import OpenAI


SYSTEM_PROMPT = (
    "You are a senior credit risk analyst at a financial institution. "
    "You analyze Early Warning System (EWS) data and provide clear, actionable "
    "explanations of customer credit risk. Be specific with numbers. "
    "Be concise — max 3-4 sentences per section. "
    "Always respond in valid JSON with the exact keys requested."
)


def get_llm_client(settings) -> OpenAI:
    return OpenAI(
        base_url=settings.nebius_base_url,
        api_key=settings.nebius_api_key,
    )


def explain_customer_risk(
    client: OpenAI,
    model: str,
    customer_id: str,
    warning_score: float,
    risk_category: str,
    rule_scores: dict,
    indicators: list[str],
    repayment_summary: dict,
    exposure_summary: dict,
) -> dict:
    """Generate AI explanation for a customer's risk assessment."""

    user_prompt = _build_customer_prompt(
        customer_id, warning_score, risk_category,
        rule_scores, indicators, repayment_summary, exposure_summary,
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=800,
        )
        raw = response.choices[0].message.content.strip()
        # Strip markdown fences if the model wraps its reply
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        return json.loads(raw)
    except Exception:
        return _fallback_customer_explanation(
            customer_id, warning_score, risk_category,
            rule_scores, indicators,
        )


def explain_portfolio_risk(
    client: OpenAI,
    model: str,
    portfolio_stats: dict,
) -> dict:
    """Generate AI explanation for portfolio-level risk."""

    user_prompt = _build_portfolio_prompt(portfolio_stats)

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            max_tokens=600,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw[3:]
            if raw.endswith("```"):
                raw = raw[:-3]
            raw = raw.strip()
        return json.loads(raw)
    except Exception:
        return _fallback_portfolio_explanation(portfolio_stats)


# ---- Prompt builders ----

def _build_customer_prompt(
    customer_id: str,
    warning_score: float,
    risk_category: str,
    rule_scores: dict,
    indicators: list[str],
    repayment_summary: dict,
    exposure_summary: dict,
) -> str:
    indicator_text = "\n".join(f"  - {ind}" for ind in indicators) if indicators else "  (none)"

    return f"""Analyze this EWS assessment for customer {customer_id} and return a JSON object.

**Warning Score:** {warning_score}/100 (category: {risk_category})

**Rule Scores (0-100, higher = more risk):**
  - Repayment Delay: {rule_scores.get('delay_score', 0):.1f}
  - Miss Classification: {rule_scores.get('miss_score', 0):.1f}
  - Exposure: {rule_scores.get('exposure_score', 0):.1f}
  - Data Completeness: {rule_scores.get('completeness_score', 0):.1f}
  - Cash Flow Deterioration: {rule_scores.get('cashflow_score', 0):.1f}

**Repayment Summary:**
  - Total payments: {repayment_summary.get('total_payments', 'N/A')}
  - Late payments: {repayment_summary.get('late_count', 'N/A')}
  - Max DPD: {repayment_summary.get('max_dpd', 'N/A')} days
  - Avg days late: {repayment_summary.get('avg_days_late', 'N/A')}

**Exposure Summary:**
  - Active loans: {exposure_summary.get('active_loans', 'N/A')}
  - Utilisation ratio: {exposure_summary.get('utilisation_ratio', 'N/A')}
  - Total outstanding: {exposure_summary.get('total_outstanding', 'N/A')}

**Active Indicators:**
{indicator_text}

Return ONLY a JSON object with these keys:
{{
  "explanation": "<2-3 sentence plain English summary of the customer's risk situation>",
  "risk_factors": ["<top risk factor 1>", "<top risk factor 2>", ...],
  "recommended_actions": ["<action 1>", "<action 2>", ...],
  "assessment_type": "<temporary | sustained | inconclusive>"
}}"""


def _build_portfolio_prompt(portfolio_stats: dict) -> str:
    dist = portfolio_stats.get("risk_distribution", {})
    top_risks = portfolio_stats.get("top_risks", [])
    top_text = "\n".join(f"  - {r}" for r in top_risks) if top_risks else "  (none identified)"

    return f"""Analyze this portfolio-level EWS summary and return a JSON object.

**Portfolio Overview:**
  - Total customers monitored: {portfolio_stats.get('total_customers', 'N/A')}
  - Average warning score: {portfolio_stats.get('avg_score', 'N/A'):.1f}

**Risk Distribution:**
  - Low: {dist.get('Low', 0)}
  - Medium: {dist.get('Medium', 0)}
  - High: {dist.get('High', 0)}
  - Critical: {dist.get('Critical', 0)}

**Top Emerging Risks:**
{top_text}

Return ONLY a JSON object with these keys:
{{
  "summary": "<2-3 sentence overview of portfolio health>",
  "key_concerns": ["<concern 1>", "<concern 2>", ...],
  "recommendations": ["<recommendation 1>", "<recommendation 2>", ...]
}}"""


# ---- Fallbacks (no LLM) ----

def _fallback_customer_explanation(
    customer_id: str,
    warning_score: float,
    risk_category: str,
    rule_scores: dict,
    indicators: list[str],
) -> dict:
    ranked = sorted(
        [
            ("Repayment delays", rule_scores.get("delay_score", 0)),
            ("Miss pattern", rule_scores.get("miss_score", 0)),
            ("Exposure level", rule_scores.get("exposure_score", 0)),
            ("Cash flow deterioration", rule_scores.get("cashflow_score", 0)),
        ],
        key=lambda x: -x[1],
    )
    risk_factors = [f"{name} (score {score:.0f}/100)" for name, score in ranked if score > 15]

    top_score_name = ranked[0][0] if ranked else "Unknown"

    if risk_category == "Critical":
        explanation = (
            f"Customer {customer_id} has a critical warning score of {warning_score:.0f}/100. "
            f"The primary driver is {top_score_name.lower()}. "
            "Immediate review and intervention are recommended."
        )
        actions = ["Escalate to senior credit risk team", "Initiate direct customer contact", "Review collateral and recovery options"]
        assessment = "sustained"
    elif risk_category == "High":
        explanation = (
            f"Customer {customer_id} scored {warning_score:.0f}/100, indicating elevated credit risk. "
            f"{top_score_name} is the leading concern. Close monitoring is required."
        )
        actions = ["Schedule relationship manager review", "Increase monitoring frequency", "Assess restructuring options"]
        assessment = "sustained"
    elif risk_category == "Medium":
        explanation = (
            f"Customer {customer_id} scored {warning_score:.0f}/100 with moderate risk signals. "
            f"{top_score_name} shows early signs of stress. Continued monitoring advised."
        )
        actions = ["Continue standard monitoring", "Flag for next review cycle"]
        assessment = "inconclusive"
    else:
        explanation = (
            f"Customer {customer_id} scored {warning_score:.0f}/100. "
            "No significant deterioration detected. Standard monitoring applies."
        )
        actions = ["Maintain regular monitoring schedule"]
        assessment = "temporary"

    return {
        "explanation": explanation,
        "risk_factors": risk_factors or ["No significant risk factors identified"],
        "recommended_actions": actions,
        "assessment_type": assessment,
    }


def _fallback_portfolio_explanation(portfolio_stats: dict) -> dict:
    dist = portfolio_stats.get("risk_distribution", {})
    total = portfolio_stats.get("total_customers", 0)
    high_critical = dist.get("High", 0) + dist.get("Critical", 0)
    pct = (high_critical / total * 100) if total > 0 else 0

    summary = (
        f"Portfolio of {total} monitored customers: "
        f"{high_critical} ({pct:.1f}%) are rated High or Critical risk. "
        f"Average warning score is {portfolio_stats.get('avg_score', 0):.1f}/100."
    )

    concerns = []
    if dist.get("Critical", 0) > 0:
        concerns.append(f"{dist['Critical']} customers at Critical risk require immediate attention")
    if pct > 10:
        concerns.append(f"High/Critical concentration at {pct:.1f}% exceeds typical 5-8% threshold")

    recommendations = ["Review all Critical-rated customers within 48 hours"]
    if pct > 10:
        recommendations.append("Investigate portfolio-wide stress factors driving elevated risk")

    return {
        "summary": summary,
        "key_concerns": concerns or ["Portfolio risk within normal parameters"],
        "recommendations": recommendations,
    }
