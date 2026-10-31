"""Credit Risk Adjudicator.

Recomputes every figure it relies on from a fresh snapshot using trusted code, applies
the deterministic scoring model and the active policy, and proposes a classification,
severity, confidence, recommendation and review pathway. It cannot invent evidence or
approve anything: interventions are proposals that, when consequential, require an
authorised human decision.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from app.agents.contracts import AgentId
from app.agents.llm import LLMGateway, unverified_numbers
from app.agents.tools import CalculationCache
from app.agents.view import InvestigationView
from app.intelligence import scoring
from app.services.policy import PolicyConfig, severity_at_least

PRIMARY_CODES = {
    "repayment": {"repayment_deterioration", "repayment_stable"},
    "cashflow": {"cashflow_decline", "cashflow_stable", "cashflow_seasonal_dip"},
    "exposure": {"obligation_pressure", "exposure_stable"},
}
OPEN_MATERIAL = {"open", "unresolved", "escalated", "upheld"}


@dataclass
class AdjudicationSpec:
    classification: str
    severity: str
    risk_score: float | None
    confidence: float
    recommendation: str
    interventions: list[dict]
    rationale: list[str]
    pathway: str
    escalation_reasons: list[str]
    completeness: str
    exposure_at_risk: float
    finding_ids: list[str]
    inputs: dict = field(default_factory=dict)


class _Explanation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    explanation: str = Field(max_length=700)


def excluded_families(view: InvestigationView) -> frozenset[str]:
    """Families whose primary finding a human rejected or flagged as unsupported."""
    rejected = view.rejected_finding_ids
    latest: dict[str, str] = {}
    for f in view.findings:
        family = view.family_of(f)
        if family in PRIMARY_CODES and f.code in PRIMARY_CODES[family]:
            latest[family] = f.id
    return frozenset(family for family, fid in latest.items() if fid in rejected)


def open_material_challenges(view: InvestigationView) -> int:
    return sum(
        1 for c in view.challenges if c.materiality == "material" and c.status in OPEN_MATERIAL
    )


class Adjudicator:
    def provisional(self, cache: CalculationCache, view: InvestigationView) -> scoring.Assessment:
        return self._assess(cache, view)

    def _assess(self, cache: CalculationCache, view: InvestigationView) -> scoring.Assessment:
        return scoring.assess(
            repayment=cache.get("repayment_profile").output,
            cashflow=cache.get("cashflow_profile").output,
            exposure=cache.get("exposure_profile").output,
            pressure=cache.get("obligation_pressure").output,
            baseline=cache.get("behavioural_baseline").output,
            integrity=cache.get("data_integrity").output,
            scoring=cache.policy.scoring,
            excluded_families=excluded_families(view),
            open_material_challenges=open_material_challenges(view),
        )

    def adjudicate(
        self,
        cache: CalculationCache,
        view: InvestigationView,
        policy: PolicyConfig,
        llm: LLMGateway,
    ) -> AdjudicationSpec:
        a = self._assess(cache, view)
        rep = cache.get("repayment_profile").output
        exposure = cache.get("exposure_profile").output.get("exposure_at_risk") or 0.0
        esc = policy.escalation
        reasons_c, reasons_b = [], []
        determined = a.severity != "undetermined"
        if determined and severity_at_least(a.severity, esc.approval_min_severity):
            reasons_c.append(f"Severity {a.severity} meets the approval threshold")
        if (
            determined
            and exposure >= esc.material_exposure_amount
            and severity_at_least(a.severity, esc.material_exposure_min_severity)
        ):
            reasons_c.append(f"Material exposure at risk ({exposure:,.0f})")
        consequential = [i for i in a.interventions if i in esc.approval_required_interventions]
        if consequential:
            reasons_c.append("Consequential intervention proposed: " + ", ".join(consequential))
        if determined and severity_at_least(a.severity, esc.analyst_review_min_severity):
            reasons_b.append(f"Severity {a.severity} meets the analyst-review threshold")
        if a.confidence < esc.min_confidence_for_autonomy:
            reasons_b.append(f"Confidence {a.confidence:.2f} below autonomy threshold")
        if (
            a.classification == "insufficient_evidence"
            and esc.insufficient_evidence_requires_review
        ):
            reasons_b.append("Insufficient evidence for a risk conclusion")
        unresolved = open_material_challenges(view)
        if unresolved and esc.unresolved_material_challenge_requires_review:
            reasons_b.append(f"{unresolved} material challenge(s) unresolved by agents")
        previous = view.adjudications[-1] if view.adjudications else None
        if (
            previous is not None
            and previous.classification != a.classification
            and esc.classification_change_requires_review
        ):
            reasons_b.append(
                f"Classification changed from {previous.classification} to {a.classification}"
            )
        contested = [h.code for h in view.hypotheses if h.status == "contested"]
        if contested:
            reasons_b.append("Contradictory findings on: " + ", ".join(contested))
        pathway = "C" if reasons_c else "B" if reasons_b else "A"

        rationale = list(a.rationale)
        explanation = self._explain(a, exposure, llm)
        if explanation:
            rationale.append(f"AI explanation (figures verified): {explanation}")
        finding_ids = [f.id for f in view.active_findings()]
        calcs = {name: c.input_digest for name, c in cache.all().items()}
        return AdjudicationSpec(
            classification=a.classification,
            severity=a.severity,
            risk_score=a.risk_score,
            confidence=a.confidence,
            recommendation=a.recommendation,
            interventions=[
                {
                    "id": f"int-{i}",
                    "code": code,
                    "requires_approval": code in esc.approval_required_interventions,
                }
                for i, code in enumerate(a.interventions, 1)
            ],
            rationale=rationale,
            pathway=pathway,
            escalation_reasons=reasons_c + reasons_b,
            completeness="complete"
            if a.classification != "insufficient_evidence"
            else "incomplete",
            exposure_at_risk=float(exposure),
            finding_ids=finding_ids,
            inputs={
                "assessment": a.as_dict(),
                "repayment_missed_count": rep.get("missed_count"),
                "latest_repayment_month": (rep.get("months") or [None])[-1],
                "calculation_digests": calcs,
                "excluded_families": sorted(excluded_families(view)),
                "scoring_version": scoring.SCORING_VERSION,
            },
        )

    def adjudicate_portfolio(
        self, cache: CalculationCache, view: InvestigationView, policy: PolicyConfig
    ) -> AdjudicationSpec:
        p = cache.get("portfolio_profile").output
        clusters = [g for g in p["segments"] if not g.get("suppressed") and g["cluster"]]
        worst = max((g["deteriorating_share"] for g in clusters), default=0.0)
        if p["customers"] < policy.data.portfolio_min_cell_size:
            classification, severity = "insufficient_evidence", "undetermined"
        elif clusters:
            classification = "portfolio_cluster"
            severity = "high" if worst >= 0.5 else "moderate"
        else:
            classification = "portfolio_stable"
            severity = "low" if p["portfolio_deteriorating_share"] < 0.25 else "moderate"
        reasons = []
        if severity in {"moderate", "high"}:
            reasons.append(f"Portfolio severity {severity}")
        if classification == "insufficient_evidence":
            reasons.append("Too few customers in scope for a portfolio conclusion")
        recommendation = "segment_review" if clusters else "continue_monitoring"
        return AdjudicationSpec(
            classification=classification,
            severity=severity,
            risk_score=round(100 * p["portfolio_deteriorating_share"], 2),
            confidence=0.75 if classification != "insufficient_evidence" else 0.3,
            recommendation=recommendation,
            interventions=[{"id": "int-1", "code": recommendation, "requires_approval": False}],
            rationale=[
                f"{len(clusters)} deterioration cluster(s): "
                + ", ".join(g["group"] for g in clusters),
                f"Segment exposure HHI {p['segment_exposure_hhi']:.2f}",
            ],
            pathway="B" if reasons else "A",
            escalation_reasons=reasons,
            completeness="complete",
            exposure_at_risk=float(p["total_exposure"]),
            finding_ids=[f.id for f in view.active_findings()],
            inputs={"portfolio_digest": cache.get("portfolio_profile").input_digest},
        )

    def _explain(self, a: scoring.Assessment, exposure: float, llm: LLMGateway) -> str | None:
        facts = a.as_dict() | {"exposure_at_risk": exposure}
        result = llm.complete_json(
            agent_id=AgentId.ADJUDICATOR.value,
            purpose="explain_adjudication",
            instructions=(
                "Explain this deterministic credit-deterioration adjudication for a credit "
                "officer in at most four sentences. Do not add figures or recommend actions "
                "beyond those listed."
            ),
            data=facts,
            schema=_Explanation,
        )
        if result is None:
            return None
        bad = unverified_numbers(result.explanation, facts)
        if bad:
            llm.flag(type="unverified_numbers", agent=AgentId.ADJUDICATOR.value, numbers=bad[:5])
            return None
        return result.explanation


def serialise(spec: AdjudicationSpec) -> dict:
    return {
        "classification": spec.classification,
        "severity": spec.severity,
        "risk_score": spec.risk_score,
        "confidence": spec.confidence,
        "recommendation": spec.recommendation,
        "pathway": spec.pathway,
        "escalation_reasons": spec.escalation_reasons,
        "interventions": json.loads(json.dumps(spec.interventions)),
    }
