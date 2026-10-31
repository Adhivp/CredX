"""Evidence Challenger: attempts to disprove the leading conclusion.

Challenges are raised by deterministic rules over the current findings (optionally
supplemented by an LLM restricted to the same challenge catalogue and to existing
hypothesis ids). Material challenges either request specialist re-investigation, which
reopens the loop, or, when no agent can resolve them, remain unresolved for a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from app.agents.contracts import CONTEXT_CATEGORIES, AgentId
from app.agents.director import TaskRequest
from app.agents.llm import LLMGateway, sanitize_untrusted
from app.agents.view import InvestigationView
from app.intelligence.scoring import Assessment

CHALLENGE_TYPES = (
    "sustainment_untested",
    "seasonality_untested",
    "data_quality_unverified",
    "single_family_support",
    "conflicting_evidence",
    "human_context_untested",
    "rule_disagreement",
)
DETERIORATING = {
    "emerging_deterioration",
    "sustained_deterioration",
    "accelerating_deterioration",
}
FAMILY_AGENT = {
    "repayment": AgentId.REPAYMENT,
    "cashflow": AgentId.CASHFLOW,
    "exposure": AgentId.EXPOSURE,
}


@dataclass
class ChallengeProposal:
    challenge_type: str
    target_type: str
    target_id: str
    statement: str
    materiality: str
    requests: list[TaskRequest] = field(default_factory=list)
    status: str = "open"
    resolution: str = ""
    raised_by: str = AgentId.CHALLENGER.value


class _LLMChallenge(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenge_type: str
    hypothesis_id: str
    statement: str = Field(max_length=300)


class _LLMChallenges(BaseModel):
    model_config = ConfigDict(extra="forbid")
    challenges: list[_LLMChallenge] = Field(default_factory=list, max_length=2)


def _request(agent: AgentId, code: str, question: str) -> TaskRequest:
    return TaskRequest(
        agent=agent,
        hypothesis_code=code,
        question=question,
        priority=70,
        requested_by=AgentId.CHALLENGER.value,
    )


class Challenger:
    def review(
        self,
        view: InvestigationView,
        provisional: Assessment,
        screening: dict,
        llm: LLMGateway,
    ) -> list[ChallengeProposal]:
        raised: list[ChallengeProposal] = []

        def seen(ctype: str, target: str) -> bool:
            return any(
                c.challenge_type == ctype and c.target_id == target for c in view.challenges
            ) or any(c.challenge_type == ctype and c.target_id == target for c in raised)

        def status_of(code):
            h = view.hypothesis(code)
            return h.status if h else None

        active = view.active_findings()
        supported = [
            view.hypothesis(code)
            for code in ("repayment_deterioration", "cashflow_decline")
            if status_of(code) in {"supported", "contested"}
        ]
        baseline_ran = any(f.agent_id == AgentId.BASELINE.value for f in active)

        if supported and not baseline_ran and not seen("sustainment_untested", supported[0].id):
            raised.append(
                ChallengeProposal(
                    "sustainment_untested",
                    "hypothesis",
                    supported[0].id,
                    "The deterioration has not been tested against the customer's own history: "
                    "is it sustained, or a temporary disruption that is already recovering?",
                    "material",
                    [
                        _request(
                            AgentId.BASELINE,
                            "temporary_disruption",
                            "Measure persistence and recovery against the customer's baseline",
                        )
                    ],
                )
            )

        cash = view.hypothesis("cashflow_decline")
        seasonal_checked = any(
            f.code in {"seasonal_pattern", "seasonality_assessment"} for f in active
        )
        if (
            cash is not None
            and cash.status in {"supported", "contested"}
            and screening.get("cashflow_months", 0) >= 12
            and not seasonal_checked
            and not seen("seasonality_untested", cash.id)
        ):
            enough = screening.get("cashflow_months", 0) >= 15
            raised.append(
                ChallengeProposal(
                    "seasonality_untested",
                    "hypothesis",
                    cash.id,
                    "The inflow decline may be seasonal; it has not been compared with the same "
                    "months of the previous year.",
                    "material" if enough else "minor",
                    [
                        _request(
                            AgentId.BASELINE,
                            "seasonal_pattern",
                            "Compare recent inflows with the same months last year",
                        )
                    ]
                    if enough
                    else [],
                    status="open" if enough else "unresolved",
                    resolution="" if enough else "Fewer than 15 months: cannot test seasonality",
                )
            )

        integrity_ran = any(f.agent_id == AgentId.INTEGRITY.value for f in active)
        if screening.get("data_flags") and not integrity_ran:
            target = view.hypothesis("data_quality_artefact") or (
                supported[0] if supported else None
            )
            if target is not None and not seen("data_quality_unverified", target.id):
                raised.append(
                    ChallengeProposal(
                        "data_quality_unverified",
                        "hypothesis",
                        target.id,
                        "Screening found duplicated, contradictory or missing records that "
                        "have not been verified.",
                        "material",
                        [
                            _request(
                                AgentId.INTEGRITY,
                                "data_quality_artefact",
                                "Verify the records behind the deterioration signals",
                            )
                        ],
                    )
                )

        if provisional.classification in DETERIORATING and len(provisional.elevated_families) == 1:
            family = provisional.elevated_families[0]
            target = view.hypothesis(
                {
                    "repayment": "repayment_deterioration",
                    "cashflow": "cashflow_decline",
                    "exposure": "obligation_pressure",
                }[family]
            )
            missing = [
                (name, agent)
                for name, agent in FAMILY_AGENT.items()
                if name != family
                and not view.agent_ran(agent.value)
                and self._has_data(name, screening)
            ]
            if target is not None and missing and not seen("single_family_support", target.id):
                name, agent = missing[0]
                code = {
                    "repayment": "repayment_deterioration",
                    "cashflow": "cashflow_decline",
                    "exposure": "obligation_pressure",
                }[name]
                raised.append(
                    ChallengeProposal(
                        "single_family_support",
                        "hypothesis",
                        target.id,
                        f"The conclusion rests on the {family} signal alone; independent "
                        f"{name} evidence has not been examined.",
                        "minor",
                        [_request(agent, code, f"Provide independent {name} evidence")],
                    )
                )

        for h in view.hypotheses:
            if h.status != "contested" or seen("conflicting_evidence", h.id):
                continue
            findings = view.findings_for(h)
            agents = {f.agent_id for f in findings}
            requests = []
            if AgentId.BASELINE.value not in agents and h.code != "human_reported_context":
                requests = [
                    _request(
                        AgentId.BASELINE,
                        h.code,
                        "Resolve conflicting evidence using the customer baseline",
                    )
                ]
            pro = [f.code for f in findings if f.direction == "supports"]
            con = [f.code for f in findings if f.direction == "contradicts"]
            raised.append(
                ChallengeProposal(
                    "conflicting_evidence",
                    "hypothesis",
                    h.id,
                    f"Evidence conflicts on '{h.statement}': supporting {', '.join(pro)}; "
                    f"contradicting {', '.join(con)}.",
                    "material",
                    requests,
                    status="open" if requests else "unresolved",
                    resolution=""
                    if requests
                    else "No specialist can resolve this conflict; human judgment required",
                )
            )

        context = view.hypothesis("human_reported_context")
        if context is not None and not view.findings_for(context):
            agents = sorted(
                {a for c in view.context_categories() for a in CONTEXT_CATEGORIES.get(c, ())},
                key=lambda a: a.value,
            )
            pending = [a for a in agents if not view.tasks_for(a.value, context.id)]
            if pending and not seen("human_context_untested", context.id):
                raised.append(
                    ChallengeProposal(
                        "human_context_untested",
                        "hypothesis",
                        context.id,
                        "Human-reported context has not been tested against the data.",
                        "material",
                        [
                            _request(a, "human_reported_context", "Test the reported context")
                            for a in pending
                        ],
                    )
                )

        for f in active:
            if f.agent_id == AgentId.INTEGRITY.value:
                continue  # integrity findings list data issues, not rule disagreements
            if (
                f.direction == "supports"
                and f.contradictions not in ("", "[]")
                and not seen("rule_disagreement", f.id)
            ):
                raised.append(
                    ChallengeProposal(
                        "rule_disagreement",
                        "finding",
                        f.id,
                        f"Finding '{f.code}' disagrees with an existing rule-engine output.",
                        "minor",
                        status="unresolved",
                        resolution="Recorded for human review; lowers confidence only",
                    )
                )

        raised.extend(self._llm_challenges(view, provisional, llm, seen))
        return raised

    @staticmethod
    def _has_data(family: str, screening: dict) -> bool:
        if family == "cashflow":
            return screening.get("cashflow_months", 0) >= 4
        if family == "repayment":
            return screening.get("repayment_periods", 0) > 0
        return screening.get("utilisation") is not None

    def _llm_challenges(self, view, provisional, llm: LLMGateway, seen) -> list[ChallengeProposal]:
        candidates = [h for h in view.hypotheses if h.status in {"supported", "contested"}]
        if not candidates:
            return []
        result = llm.complete_json(
            agent_id=AgentId.CHALLENGER.value,
            purpose="generate_challenges",
            instructions=(
                "Act as a sceptical credit reviewer. Propose at most two challenges to the "
                "supported hypotheses, using only these challenge types: "
                + ", ".join(CHALLENGE_TYPES)
                + ". Reference hypotheses only by the ids provided."
            ),
            data={
                "classification": provisional.classification,
                "hypotheses": [
                    {"id": h.id, "statement": h.statement, "status": h.status} for h in candidates
                ],
                "findings": [
                    {"code": f.code, "direction": f.direction, "summary": f.summary}
                    for f in view.active_findings()
                ],
            },
            schema=_LLMChallenges,
        )
        if result is None:
            return []
        valid_ids = {h.id for h in candidates}
        out = []
        for item in result.challenges:
            if item.challenge_type not in CHALLENGE_TYPES or item.hypothesis_id not in valid_ids:
                llm.flag(type="invalid_llm_challenge", agent="challenger")
                continue
            if seen(item.challenge_type, item.hypothesis_id):
                continue
            statement, flags = sanitize_untrusted(item.statement, 300)
            if flags:
                llm.flag(type="llm_challenge_sanitised", agent="challenger", flags=flags)
            # LLM challenges are advisory: minor, no automatic re-investigation.
            out.append(
                ChallengeProposal(
                    item.challenge_type,
                    "hypothesis",
                    item.hypothesis_id,
                    f"AI-suggested challenge: {statement}",
                    "minor",
                    status="unresolved",
                    resolution="Advisory challenge for human review",
                )
            )
        return out

    def reassess(self, view: InvestigationView) -> dict[str, tuple[str, str]]:
        """New (status, resolution) for open challenges whose follow-up work has finished."""
        import json

        tasks = {t.id: t for t in view.tasks}
        updates = {}
        for c in view.challenges:
            if c.status not in {"open", "unresolved"}:
                continue
            followups = [tasks[t] for t in json.loads(c.followup_task_ids or "[]") if t in tasks]
            target = next((h for h in view.hypotheses if h.id == c.target_id), None)
            if c.challenge_type == "conflicting_evidence":
                if target is not None and target.status != "contested":
                    updates[c.id] = ("addressed", "Conflict no longer present in latest findings")
                elif followups and all(t.status == "completed" for t in followups):
                    updates[c.id] = ("unresolved", "Re-investigation did not resolve the conflict")
                continue
            if not followups:
                continue
            if all(t.status == "completed" for t in followups):
                updates[c.id] = ("addressed", "Requested re-investigation completed")
            elif any(t.status in {"failed", "timeout", "rejected", "cancelled"} for t in followups):
                if all(t.status not in {"authorised", "requested", "running"} for t in followups):
                    updates[c.id] = ("unresolved", "Requested re-investigation did not complete")
        return updates
