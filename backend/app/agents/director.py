"""Investigation Director: hypotheses, planning, delegation control and termination.

The Director does not run every specialist. It screens the evidence, raises only the
hypotheses the evidence warrants, selects the specialists those hypotheses need, and
authorises (or refuses) every delegation request against depth, budget, duplicate and
cycle rules. Alternative explanations are left to the Evidence Challenger.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from app.agents.budget import Budget
from app.agents.contracts import (
    CONTEXT_CATEGORIES,
    DELEGATION_EDGES,
    HYPOTHESES,
    SPECIALISTS,
    AgentId,
    ContextItem,
)
from app.agents.llm import LLMGateway, unverified_numbers
from app.agents.tools import CalculationCache
from app.agents.view import InvestigationView
from app.models.investigation import AgentTask
from app.services.policy import PolicyConfig

PRIMARY = ("repayment_deterioration", "cashflow_decline", "obligation_pressure")


@dataclass
class HypothesisProposal:
    code: str
    statement: str
    priority: int
    rationale: str
    status: str = "open"
    source: str = AgentId.DIRECTOR.value


@dataclass
class TaskRequest:
    agent: AgentId
    hypothesis_code: str
    question: str
    priority: int
    requested_by: str
    requested_evidence: list[str] = field(default_factory=list)
    parent: AgentTask | None = None
    extra_key: str = ""


class _Priority(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str
    priority: int = Field(ge=0, le=100)
    rationale: str = Field(default="", max_length=300)


class _PlanAdvice(BaseModel):
    model_config = ConfigDict(extra="forbid")
    priorities: list[_Priority] = Field(max_length=12)


class Director:
    def screen(self, cache: CalculationCache) -> dict:
        integrity = cache.get("data_integrity").output
        rep = cache.get("repayment_profile").output
        cf = cache.get("cashflow_profile").output
        exp = cache.get("exposure_profile").output
        pressure = cache.get("obligation_pressure").output
        return {
            "sufficiency": integrity["sufficiency"],
            "data_flags": bool(
                integrity["duplicate_repayment_ids"]
                or integrity["duplicate_cashflow_ids"]
                or integrity["contradictions"]
                or integrity["cashflow_gaps"]
                or integrity["stale"]
            ),
            "repayment_periods": rep.get("periods", 0),
            "delinquent_periods": rep.get("delinquent_periods", 0),
            "recent_late_ratio": rep.get("recent_late_ratio"),
            "cashflow_months": cf.get("months", 0),
            "cashflow_change": cf.get("change_vs_baseline"),
            "utilisation": exp.get("latest_utilisation"),
            "utilisation_change": exp.get("utilisation_change"),
            "obligation_ratio": pressure.get("recent_ratio"),
        }

    def hypothesise(
        self,
        screening: dict,
        context: list[ContextItem],
        existing: set[str],
        policy: PolicyConfig,
        scope: str,
        llm: LLMGateway,
    ) -> list[HypothesisProposal]:
        proposals: list[HypothesisProposal] = []

        def add(code, priority, rationale, status="open", statement=None):
            if code in existing or any(p.code == code for p in proposals):
                return
            proposals.append(
                HypothesisProposal(
                    code, statement or HYPOTHESES[code].statement, priority, rationale, status
                )
            )

        if scope == "portfolio":
            add("portfolio_cluster", 80, "Portfolio-scope investigation requested")
            add("portfolio_concentration", 60, "Portfolio-scope investigation requested")
            return proposals

        for item in context:
            add(
                "human_reported_context",
                95,
                f"Human-reported context ({item.category}) to be tested against the data",
                statement=f"Reported {item.category.replace('_', ' ')} explains the behaviour",
            )
        if screening["sufficiency"] == "insufficient":
            add("data_quality_artefact", 90, "Too little history to assess deterioration")
            return proposals
        if screening["data_flags"] or screening["sufficiency"] == "limited":
            add("data_quality_artefact", 70, "Screening found data-quality issues or gaps")

        threshold = policy.scoring.cashflow_decline_threshold
        deterioration = False
        if screening["delinquent_periods"]:
            recent = screening["recent_late_ratio"] or 0
            add(
                "repayment_deterioration",
                90 if recent > 0 else 55,
                f"{screening['delinquent_periods']} delinquent period(s) in the history",
            )
            deterioration = recent > 0
        change = screening["cashflow_change"]
        if screening["cashflow_months"] >= 4 and change is not None and change < -threshold / 2:
            add("cashflow_decline", 85, "Recent inflows below the customer's baseline")
            deterioration = True
        util, util_change = screening["utilisation"], screening["utilisation_change"] or 0
        ratio = screening["obligation_ratio"]
        if (util is not None and util > 0.85) or util_change > 0.05 or (ratio or 0) > 0.5:
            add("obligation_pressure", 65, "High or rising utilisation / obligation ratio")
        if not any(p.code in PRIMARY for p in proposals) and not (existing & set(PRIMARY)):
            add("stable_profile", 50, "No deterioration signal in screening")
        if deterioration:
            add("temporary_disruption", 40, "Competing explanation", status="deferred")
            if screening["cashflow_months"] >= 12:
                add("seasonal_pattern", 35, "Competing explanation", status="deferred")
            add("segment_wide_stress", 30, "Competing explanation", status="deferred")

        self._advise(proposals, screening, llm)
        return proposals

    def _advise(self, proposals: list[HypothesisProposal], screening: dict, llm: LLMGateway):
        """Optional LLM re-prioritisation. It can reorder, never add or invent hypotheses."""
        open_props = [p for p in proposals if p.status == "open"]
        if len(open_props) < 2:
            return
        advice = llm.complete_json(
            agent_id=AgentId.DIRECTOR.value,
            purpose="prioritise_hypotheses",
            instructions=(
                "Given screening metrics for a credit customer, assign each listed hypothesis "
                "code a priority from 0 to 100 and a one-sentence rationale. Use only the codes "
                "provided."
            ),
            data={
                "screening": screening,
                "hypotheses": [{"code": p.code, "statement": p.statement} for p in open_props],
            },
            schema=_PlanAdvice,
        )
        if advice is None:
            return
        by_code = {p.code: p for p in open_props}
        for item in advice.priorities:
            target = by_code.get(item.code)
            if target is None:
                llm.flag(type="unknown_hypothesis_code", agent="director", code=item.code[:48])
                continue
            target.priority = item.priority
            if item.rationale and not unverified_numbers(item.rationale, screening):
                target.rationale = f"{target.rationale}. AI rationale: {item.rationale}"

    def plan(self, view: InvestigationView) -> list[TaskRequest]:
        requests = []
        for h in view.hypotheses:
            if h.status not in {"open", "activated"}:
                continue
            definition = HYPOTHESES.get(h.code)
            if definition is None:
                continue
            agents = list(definition.agents)
            if h.code == "human_reported_context":
                agents = sorted(
                    {a for n in view.context_categories() for a in CONTEXT_CATEGORIES.get(n, ())},
                    key=lambda a: a.value,
                )
            for agent in agents:
                if any(t.status != "rejected" for t in view.tasks_for(agent.value, h.id)):
                    continue
                requests.append(
                    TaskRequest(
                        agent=agent,
                        hypothesis_code=h.code,
                        question=h.statement,
                        priority=h.priority,
                        requested_by=AgentId.DIRECTOR.value,
                    )
                )
        return sorted(requests, key=lambda r: -r.priority)

    def authorise(
        self,
        request: TaskRequest,
        view: InvestigationView,
        budget: Budget,
        policy: PolicyConfig,
        scope: str,
        key: str,
        duplicate: AgentTask | None,
    ) -> tuple[bool, str, int]:
        """Return (authorised, reason, delegation depth)."""
        definition = HYPOTHESES.get(request.hypothesis_code)
        if request.agent not in SPECIALISTS:
            return False, "unknown_specialist", 0
        if definition is None:
            return False, "unknown_hypothesis", 0
        if (definition.scope == "portfolio") != (scope == "portfolio"):
            return False, "hypothesis_not_applicable_to_scope", 0
        if scope == "portfolio" and request.agent != AgentId.PORTFOLIO:
            return False, "specialist_not_applicable_to_portfolio_scope", 0
        depth = 0
        parent = request.parent
        if parent is not None:
            origin = AgentId(parent.agent_id)
            if request.agent not in DELEGATION_EDGES.get(origin, frozenset()):
                return False, f"delegation_not_permitted:{origin.value}->{request.agent.value}", 0
            depth = parent.delegation_depth + 1
            if depth > policy.budgets.max_delegation_depth:
                return False, "max_delegation_depth", depth
            ancestors, cursor = {origin.value}, parent
            by_id = {t.id: t for t in view.tasks}
            while cursor is not None and cursor.parent_task_id:
                cursor = by_id.get(cursor.parent_task_id)
                if cursor is not None:
                    ancestors.add(cursor.agent_id)
            if request.agent.value in ancestors:
                return False, "circular_delegation", depth
        elif request.requested_by == AgentId.CHALLENGER.value:
            depth = 1
        if duplicate is not None:
            return False, f"duplicate_of:{duplicate.id}", depth
        if not budget.reserve_task():
            return False, "budget_exhausted:max_tasks", depth
        return True, "authorised", depth

    def evaluate(self, view: InvestigationView, budget: Budget) -> tuple[str, list[str]]:
        if budget.exhausted:
            return "incomplete", [f"Budget exhausted: {budget.exhausted_reason}"]
        if not any(t.status == "completed" for t in view.tasks):
            return "incomplete", ["No specialist task completed"]
        unresolved = [
            c
            for c in view.challenges
            if c.materiality == "material" and c.status in {"open", "unresolved"}
        ]
        if unresolved:
            return "await_human", [c.statement for c in unresolved]
        return "adjudicate", ["Material questions answered or reviewed by a human"]

    def hypothesis_status(self, view: InvestigationView, hypothesis) -> str:
        if hypothesis.code == "stable_profile":
            primaries = [view.hypothesis(c) for c in PRIMARY]
            tested = [h for h in primaries if h is not None and view.findings_for(h)]
            if not tested:
                return hypothesis.status
            statuses = {self.hypothesis_status(view, h) for h in tested}
            if statuses & {"supported", "contested"}:
                return "refuted"
            return "supported" if statuses == {"refuted"} else "inconclusive"
        findings = view.findings_for(hypothesis)
        if not findings:
            return hypothesis.status
        directions = {f.direction for f in findings}
        if {"supports", "contradicts"} <= directions:
            return "contested"
        if "supports" in directions:
            return "supported"
        if "contradicts" in directions:
            return "refuted"
        return "inconclusive"
