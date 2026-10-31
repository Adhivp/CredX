"""Explicit investigation state machine.

    intake -> hypothesise -> plan -> dispatch <-> delegate -> challenge -+-> dispatch
                                                                          +-> evaluate
    evaluate -> await_human (pause) | adjudicate -> route (pause) | incomplete

Edges are conditional: ``plan`` only dispatches the specialists the hypotheses need;
``delegate`` loops back to ``dispatch`` while the Director authorises specialist
requests; ``challenge`` opens a new re-investigation round when material challenges
need more work; ``evaluate`` pauses for a human when a material question cannot be
resolved by agents. Every node transition, task, finding and decision is persisted, so
a paused investigation resumes from the database after a human acts.

Termination is guaranteed by the run budget (rounds, tasks, delegation depth,
challenge cycles, LLM calls/tokens/cost and wall-clock time). Exhaustion yields an
``incomplete`` outcome; the graph never forces a conclusion.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field

from sqlalchemy import or_, select, text, true, update
from sqlalchemy.orm import Session

from app.agents import store
from app.agents.adjudicator import Adjudicator, serialise
from app.agents.budget import Budget
from app.agents.challenger import Challenger
from app.agents.contracts import (
    DATA_SCOPES,
    HYPOTHESES,
    SPECIALISTS,
    AgentId,
    AgentResult,
    ContextItem,
    TaskSpec,
)
from app.agents.director import Director, TaskRequest
from app.agents.llm import LLMGateway, sanitize_untrusted
from app.agents.specialists import REGISTRY
from app.agents.tools import CalculationCache, ToolContext, ToolPermissionError
from app.agents.view import InvestigationView, load_view
from app.intelligence import indicators
from app.intelligence.data import CustomerSnapshot, load_customer_snapshot
from app.intelligence.portfolio import portfolio_profile
from app.models.ews import Customer
from app.models.governance import AuditLog
from app.models.investigation import (
    Adjudication,
    AgentTask,
    EvidenceChallenge,
    HumanDecision,
    Hypothesis,
    Investigation,
    LLMCall,
    ReviewItem,
)
from app.services.policy import PolicyConfig, get_policy_version

logger = logging.getLogger(__name__)
SYSTEM = "system"
DIRECTOR = AgentId.DIRECTOR.value
STARTABLE = {
    "created",
    "reopened",
    "incomplete",
    "failed",
    "awaiting_analyst_review",
    "awaiting_approval",
}


@dataclass
class RunContext:
    db: Session
    investigation: Investigation
    policy: PolicyConfig
    budget: Budget
    llm: LLMGateway
    cache: CalculationCache
    snapshot: CustomerSnapshot | None
    data_digest: str
    screening: dict = field(default_factory=dict)
    pending_followups: list[tuple[AgentTask, object]] = field(default_factory=list)
    calc_ids: dict[str, str] = field(default_factory=dict)
    wave: int = 0
    nodes: list[str] = field(default_factory=list)
    adjudication: Adjudication | None = None
    flagged_notes: set[str] = field(default_factory=set)


class InvestigationGraph:
    def __init__(self, session_factory, settings, llm_client=None):
        self.session_factory = session_factory
        self.settings = settings
        self.llm_client = llm_client
        self.director = Director()
        self.challenger = Challenger()
        self.adjudicator = Adjudicator()

    # ── Runner ──────────────────────────────────────────────────────────

    def run(self, investigation_id: str, trigger: str = "start") -> str:
        with self.session_factory() as db:
            if not self._acquire(db, investigation_id):
                return "already_running"
            investigation = db.get(Investigation, investigation_id)
            rc = None
            try:
                rc = self._context(db, investigation)
                store.emit(db, investigation, "run_started", SYSTEM, "graph", {"trigger": trigger})
                node = "intake"
                while node:
                    db.refresh(investigation, ["cancel_requested"])
                    if investigation.cancel_requested:
                        node = self._cancel(rc)
                        break
                    if not rc.budget.check_wall() and node not in {"incomplete"}:
                        node = "incomplete"
                    rc.nodes.append(node)
                    store.emit(db, investigation, "node_entered", SYSTEM, "graph", {"node": node})
                    node = getattr(self, f"_node_{node}")(rc)
                    self._flush_llm(rc)
                    db.commit()
            except Exception as exc:
                logger.exception("Investigation %s failed", investigation_id)
                db.rollback()
                investigation = db.get(Investigation, investigation_id)
                if (
                    investigation.status in store.TRANSITIONS
                    and "failed" in store.TRANSITIONS[investigation.status]
                ):
                    store.transition(
                        db, investigation, "failed", SYSTEM, "graph", type(exc).__name__
                    )
                investigation.outcome = "failed"
                db.commit()
            finally:
                investigation = db.get(Investigation, investigation_id)
                if rc is not None:
                    self._flush_llm(rc)
                    usage = json.loads(investigation.usage or "{}")
                    usage["runs"] = usage.get("runs", 0) + 1
                    usage["last_run"] = rc.budget.snapshot() | {"nodes": rc.nodes}
                    totals = usage.setdefault("totals", {})
                    for key in ("rounds", "tasks", "llm_calls", "tokens"):
                        totals[key] = totals.get(key, 0) + rc.budget.snapshot()[key]
                    totals["cost_usd"] = round(
                        totals.get("cost_usd", 0) + rc.budget.snapshot()["cost_usd"], 6
                    )
                    investigation.usage = json.dumps(usage)
                investigation.run_state = "idle"
                db.commit()
            return investigation.status

    def _acquire(self, db: Session, investigation_id: str) -> bool:
        stale = int(time.time()) - 3600
        result = db.execute(
            update(Investigation)
            .where(
                Investigation.id == investigation_id,
                or_(Investigation.run_state == "idle", Investigation.updated_at < stale),
            )
            .values(run_state="running", updated_at=int(time.time()))
        )
        db.commit()
        return result.rowcount == 1

    def _context(self, db: Session, investigation: Investigation) -> RunContext:
        policy = get_policy_version(db, investigation.policy_version_id)
        budget = Budget(policy.budgets)
        llm = LLMGateway(self.settings, budget, client=self.llm_client)
        snapshot = None
        if investigation.scope_type == "customer":
            snapshot = load_customer_snapshot(
                db, investigation.customer_id, *self.record_reviews(db, investigation.id)
            )
        segment = (
            investigation.portfolio_segment
            if investigation.scope_type == "portfolio"
            else (snapshot.segment or None)
        )

        def load_portfolio():
            with self.session_factory() as read:
                condition = Customer.portfolio_segment == segment if segment else true()
                return portfolio_profile(read, condition, policy)

        cache = CalculationCache(snapshot, policy, load_portfolio)
        digest = (
            cache.get("data_integrity").input_digest
            if snapshot
            else f"portfolio:{segment or 'all'}"
        )
        return RunContext(db, investigation, policy, budget, llm, cache, snapshot, digest)

    @staticmethod
    def record_reviews(db: Session, investigation_id: str) -> tuple[frozenset, frozenset]:
        """(disputed, confirmed) record ids from the latest human decision on each record.

        Disputed records are excluded from analysis; confirmed ones are trusted even if they
        fail an integrity check. Both remain in the evidence graph unchanged.
        """
        state: dict[str, str] = {}
        for d in db.scalars(
            select(HumanDecision)
            .where(
                HumanDecision.investigation_id == investigation_id,
                HumanDecision.target_type == "record",
            )
            .order_by(text("human_decisions.rowid"))
        ):
            state[d.target_id] = d.action
        return (
            frozenset(r for r, a in state.items() if a == "dispute_evidence"),
            frozenset(r for r, a in state.items() if a == "confirm_evidence"),
        )

    def _flush_llm(self, rc: RunContext) -> None:
        records, flags = rc.llm.drain()
        for r in records:
            rc.db.add(
                LLMCall(
                    investigation_id=rc.investigation.id,
                    task_id=r.task_id,
                    agent_id=r.agent_id,
                    purpose=r.purpose,
                    model=r.model,
                    status=r.status,
                    prompt_tokens=r.prompt_tokens,
                    completion_tokens=r.completion_tokens,
                    cost_usd=r.cost_usd,
                    latency_ms=r.latency_ms,
                    error=r.error,
                )
            )
        for flag in flags:
            store.emit(rc.db, rc.investigation, "security_flag", SYSTEM, "llm_guard", flag)
            rc.db.add(
                AuditLog(
                    action="llm_guard",
                    resource_type="investigation",
                    resource_id=rc.investigation.id,
                    outcome="blocked",
                    detail=json.dumps(flag, default=str),
                )
            )

    # ── Nodes ───────────────────────────────────────────────────────────

    def _node_intake(self, rc: RunContext) -> str:
        inv = rc.investigation
        if inv.status in STARTABLE:
            store.transition(rc.db, inv, "investigating", SYSTEM, "graph", "run started")
        elif inv.status != "investigating":
            raise store.InvalidTransition(f"Cannot run investigation in status {inv.status}")
        if not self._start_round(rc, "initial" if inv.current_round == 0 else "resumed"):
            return "incomplete"
        if rc.snapshot is not None:
            rc.screening = self.director.screen(rc.cache)
            store.emit(rc.db, inv, "evidence_screened", "agent", DIRECTOR, rc.screening)
        return "hypothesise"

    def _node_hypothesise(self, rc: RunContext) -> str:
        view = load_view(rc.db, rc.investigation)
        context = self._context_items(rc, view)
        existing = {h.code for h in view.hypotheses}
        proposals = self.director.hypothesise(
            rc.screening,
            context,
            existing,
            rc.policy,
            rc.investigation.scope_type,
            rc.llm,
        )
        for p in proposals:
            h = Hypothesis(
                investigation_id=rc.investigation.id,
                code=p.code,
                statement=p.statement,
                status=p.status,
                priority=p.priority,
                source=p.source,
                rationale=p.rationale,
                round_created=rc.investigation.current_round,
            )
            rc.db.add(h)
            rc.db.flush()
            store.emit(
                rc.db,
                rc.investigation,
                "hypothesis_created",
                "agent",
                DIRECTOR,
                {
                    "hypothesis_id": h.id,
                    "code": p.code,
                    "status": p.status,
                    "priority": p.priority,
                    "rationale": p.rationale,
                },
            )
        return "plan"

    def _node_plan(self, rc: RunContext) -> str:
        view = load_view(rc.db, rc.investigation)
        for task in [t for t in view.tasks if t.status == "requested"]:
            self._authorise_human_task(rc, task)
        view = load_view(rc.db, rc.investigation)
        for request in self.director.plan(view):
            self._create_task(rc, request, view)
            view = load_view(rc.db, rc.investigation)
        selected = sorted({t.agent_id for t in view.tasks if t.status == "authorised"})
        store.emit(
            rc.db,
            rc.investigation,
            "plan_created",
            "agent",
            DIRECTOR,
            {
                "selected_specialists": selected,
                "not_selected": sorted(a.value for a in SPECIALISTS if a.value not in selected),
            },
        )
        return "dispatch" if self._runnable(view) else "challenge"

    def _node_dispatch(self, rc: RunContext) -> str:
        view = load_view(rc.db, rc.investigation)
        runnable = self._runnable(view)
        if not runnable:
            return "challenge"
        rc.wave += 1
        started = store.now()
        for t in runnable:
            t.status, t.started_at, t.wave = "running", started, rc.wave
            t.attempts += 1
            store.emit(
                rc.db,
                rc.investigation,
                "task_started",
                "agent",
                t.agent_id,
                {"agent": t.agent_id, "wave": rc.wave},
                task_id=t.id,
            )
        store.emit(
            rc.db,
            rc.investigation,
            "wave_started",
            SYSTEM,
            "graph",
            {
                "wave": rc.wave,
                "task_ids": [t.id for t in runnable],
                "agents": [t.agent_id for t in runnable],
                "parallel": len(runnable) > 1,
            },
        )
        rc.db.commit()  # no write transaction is held while specialists run

        context = self._context_items(rc, view)
        specs = {t.id: self._spec(rc, t, view, context) for t in runnable}
        pool = ThreadPoolExecutor(
            max_workers=min(len(runnable), rc.policy.budgets.max_parallel_tasks)
        )
        futures = {pool.submit(self._execute, rc, specs[t.id]): t for t in runnable}
        done, pending = wait(futures, timeout=rc.policy.budgets.task_timeout_seconds)
        pool.shutdown(wait=False, cancel_futures=True)

        for future, task in futures.items():
            if future in pending:
                self._task_failed(rc, task, "timeout", "Task exceeded its deadline")
                continue
            outcome = future.result()
            if isinstance(outcome["error"], ToolPermissionError):
                self._task_failed(rc, task, "failed", str(outcome["error"]), retry=False)
                store.emit(
                    rc.db,
                    rc.investigation,
                    "security_flag",
                    "agent",
                    task.agent_id,
                    {"type": "tool_permission_denied", "detail": str(outcome["error"])},
                    task_id=task.id,
                )
                continue
            if outcome["error"] is not None:
                self._task_failed(rc, task, "failed", type(outcome["error"]).__name__)
                continue
            self._task_completed(rc, task, outcome)
        self._update_hypotheses(rc)
        return "delegate"

    def _node_delegate(self, rc: RunContext) -> str:
        pending, rc.pending_followups = rc.pending_followups, []
        view = load_view(rc.db, rc.investigation)
        for origin, followup in pending:
            request = TaskRequest(
                agent=followup.target_agent,
                hypothesis_code=followup.hypothesis_code,
                question=followup.question,
                priority=60,
                requested_by=f"agent:{origin.agent_id}",
                requested_evidence=followup.requested_evidence,
                parent=origin,
            )
            self._create_task(rc, request, view, reason=followup.reason)
            view = load_view(rc.db, rc.investigation)
        return "dispatch" if self._runnable(view) else "challenge"

    def _node_challenge(self, rc: RunContext) -> str:
        db, inv = rc.db, rc.investigation
        view = load_view(db, inv)
        for cid, (status, resolution) in self.challenger.reassess(view).items():
            challenge = db.get(EvidenceChallenge, cid)
            challenge.status, challenge.resolution = status, resolution
            challenge.resolved_at = store.now() if status == "addressed" else None
            store.emit(
                db,
                inv,
                "challenge_updated",
                "agent",
                AgentId.CHALLENGER.value,
                {"challenge_id": cid, "status": status, "resolution": resolution},
            )
        view = load_view(db, inv)
        if inv.scope_type == "portfolio":
            return "dispatch" if self._runnable(view) else "evaluate"
        provisional = self.adjudicator.provisional(rc.cache, view)
        proposals = self.challenger.review(view, provisional, rc.screening, rc.llm)
        with_requests = []
        for p in proposals:
            row = EvidenceChallenge(
                investigation_id=inv.id,
                round=inv.current_round,
                target_type=p.target_type,
                target_id=p.target_id,
                challenge_type=p.challenge_type,
                statement=p.statement,
                materiality=p.materiality,
                status=p.status,
                raised_by=p.raised_by,
                resolution=p.resolution,
            )
            db.add(row)
            db.flush()
            store.emit(
                db,
                inv,
                "challenge_raised",
                "agent",
                AgentId.CHALLENGER.value,
                {
                    "challenge_id": row.id,
                    "type": p.challenge_type,
                    "materiality": p.materiality,
                    "statement": p.statement,
                    "requests": [r.agent.value for r in p.requests],
                },
            )
            if p.requests:
                with_requests.append((row, p.requests))
        if with_requests:
            if rc.budget.start_challenge_cycle() and self._start_round(rc, "re-investigation"):
                for row, requests in with_requests:
                    ids = []
                    for r in requests:
                        task = self._create_task(rc, r, load_view(db, inv), challenge=row)
                        if task is not None:
                            ids.append(task.id)
                    row.followup_task_ids = json.dumps(ids)
                    if not ids:
                        row.status, row.resolution = "unresolved", "Re-investigation not authorised"
            else:
                for row, _ in with_requests:
                    row.status = "unresolved"
                    row.resolution = "Re-investigation budget exhausted"
                # Not a reason to abandon the case: the unresolved questions go to a human.
                store.emit(
                    db,
                    inv,
                    "reinvestigation_budget_exhausted",
                    "agent",
                    DIRECTOR,
                    {"reason": rc.budget.exhausted_reason or "max_challenge_cycles"},
                )
                rc.budget.exhausted_reason = None
        db.flush()
        return "dispatch" if self._runnable(load_view(db, inv)) else "evaluate"

    def _node_evaluate(self, rc: RunContext) -> str:
        view = load_view(rc.db, rc.investigation)
        decision, reasons = self.director.evaluate(view, rc.budget)
        store.emit(
            rc.db,
            rc.investigation,
            "completeness_evaluated",
            "agent",
            DIRECTOR,
            {"decision": decision, "reasons": reasons},
        )
        return {"await_human": "await_human", "adjudicate": "adjudicate"}.get(
            decision, "incomplete"
        )

    def _node_await_human(self, rc: RunContext) -> None:
        view = load_view(rc.db, rc.investigation)
        reasons = []
        for c in view.challenges:
            if c.materiality == "material" and c.status in {"open", "unresolved"}:
                c.status = "escalated"
                reasons.append(c.statement)
                store.emit(
                    rc.db,
                    rc.investigation,
                    "challenge_escalated",
                    "agent",
                    DIRECTOR,
                    {"challenge_id": c.id},
                )
        self._open_review(rc, "analyst_review", "credit_analyst", "pre_adjudication", reasons)
        store.transition(
            rc.db,
            rc.investigation,
            "awaiting_analyst_review",
            "agent",
            DIRECTOR,
            "Material question requires human judgment",
        )
        rc.investigation.outcome = "awaiting_human"
        return None

    def _node_adjudicate(self, rc: RunContext) -> str:
        view = load_view(rc.db, rc.investigation)
        if rc.investigation.scope_type == "portfolio":
            spec = self.adjudicator.adjudicate_portfolio(rc.cache, view, rc.policy)
        else:
            # Recalculate from a fresh snapshot with trusted code before adjudicating.
            fresh = load_customer_snapshot(
                rc.db,
                rc.investigation.customer_id,
                rc.snapshot.excluded_record_ids,
                rc.snapshot.confirmed_record_ids,
            )
            rc.cache = CalculationCache(fresh, rc.policy, rc.cache.portfolio_loader)
            spec = self.adjudicator.adjudicate(rc.cache, view, rc.policy, rc.llm)
        inputs_calc = indicators.make_calculation(
            "adjudication_inputs", [spec.inputs], [], spec.inputs
        )
        calc_id = store.store_calculation(rc.db, rc.investigation.id, inputs_calc, rc.calc_ids)
        for calc in rc.cache.all().values():
            store.store_calculation(rc.db, rc.investigation.id, calc, rc.calc_ids)
        row = Adjudication(
            investigation_id=rc.investigation.id,
            round=rc.investigation.current_round,
            classification=spec.classification,
            severity=spec.severity,
            risk_score=spec.risk_score,
            confidence=spec.confidence,
            recommendation=spec.recommendation,
            proposed_interventions=json.dumps(spec.interventions),
            rationale=json.dumps(spec.rationale),
            pathway=spec.pathway,
            escalation_reasons=json.dumps(spec.escalation_reasons),
            completeness=spec.completeness,
            exposure_at_risk=spec.exposure_at_risk,
            scoring_version=spec.inputs.get("scoring_version", "portfolio-1.0"),
            policy_version_id=rc.investigation.policy_version_id,
            calculation_id=calc_id,
            finding_ids=json.dumps(spec.finding_ids),
            created_at=store.now(),
        )
        rc.db.add(row)
        rc.db.flush()
        rc.adjudication = row
        store.emit(
            rc.db,
            rc.investigation,
            "adjudication_recorded",
            "agent",
            AgentId.ADJUDICATOR.value,
            {"adjudication_id": row.id} | serialise(spec),
        )
        return "route"

    def _node_route(self, rc: RunContext) -> None:
        inv, adj = rc.investigation, rc.adjudication
        reasons = json.loads(adj.escalation_reasons)
        inv.pathway = adj.pathway
        inv.outcome = "adjudicated"
        if adj.pathway == "A":
            self._close_reviews(rc)
            store.transition(rc.db, inv, "monitoring", "agent", DIRECTOR, "Path A: autonomous")
        elif inv.scope_type == "portfolio":
            self._open_review(
                rc, "portfolio_review", "portfolio_manager", "post_adjudication", reasons, adj.id
            )
            store.transition(
                rc.db,
                inv,
                "awaiting_analyst_review",
                "agent",
                DIRECTOR,
                "Portfolio review required",
            )
        elif adj.pathway == "B":
            self._open_review(
                rc, "analyst_review", "credit_analyst", "post_adjudication", reasons, adj.id
            )
            store.transition(
                rc.db, inv, "awaiting_analyst_review", "agent", DIRECTOR, "Path B: analyst review"
            )
        else:
            self._open_review(
                rc, "authorised_approval", "credit_manager", "approval", reasons, adj.id
            )
            store.transition(
                rc.db,
                inv,
                "awaiting_approval",
                "agent",
                DIRECTOR,
                "Path C: authorised approval required",
            )
        return None

    def _node_incomplete(self, rc: RunContext) -> None:
        reason = rc.budget.exhausted_reason or "insufficient_progress"
        self._open_review(
            rc,
            "analyst_review",
            "credit_analyst",
            "incomplete",
            [f"Investigation incomplete: {reason}"],
        )
        store.transition(rc.db, rc.investigation, "incomplete", SYSTEM, "graph", reason)
        rc.investigation.outcome = "budget_exhausted" if rc.budget.exhausted else "incomplete"
        return None

    def _cancel(self, rc: RunContext) -> None:
        for task in load_view(rc.db, rc.investigation).tasks:
            if task.status in {"requested", "authorised", "running"}:
                task.status = "cancelled"
        self._close_reviews(rc)
        if rc.investigation.status != "cancelled":
            store.transition(
                rc.db, rc.investigation, "cancelled", SYSTEM, "graph", "Cancellation requested"
            )
        rc.investigation.outcome = "cancelled"
        rc.db.commit()
        return None

    # ── Helpers ─────────────────────────────────────────────────────────

    def _start_round(self, rc: RunContext, reason: str) -> bool:
        if not rc.budget.start_round():
            return False
        lifetime_cap = rc.policy.budgets.max_rounds * 4
        if rc.investigation.current_round >= lifetime_cap:
            rc.budget.exhausted_reason = "lifetime_round_cap"
            return False
        rc.investigation.current_round += 1
        store.emit(
            rc.db,
            rc.investigation,
            "round_started",
            "agent",
            DIRECTOR,
            {"round": rc.investigation.current_round, "reason": reason},
        )
        return True

    def _context_items(self, rc: RunContext, view: InvestigationView) -> list[ContextItem]:
        items = []
        for note in view.notes:
            if note.kind != "context" or not note.category:
                continue
            clean, flags = sanitize_untrusted(note.content)
            if flags and note.id not in rc.flagged_notes:
                rc.flagged_notes.add(note.id)
                rc.llm.flag(type="untrusted_note", note_id=note.id, flags=flags)
            items.append(
                ContextItem(
                    note_id=note.id, category=note.category, text=clean, author_role=note.role
                )
            )
        return items

    def _spec(
        self, rc: RunContext, task: AgentTask, view: InvestigationView, context: list[ContextItem]
    ) -> TaskSpec:
        hypothesis = next((h for h in view.hypotheses if h.id == task.hypothesis_id), None)
        return TaskSpec(
            task_id=task.id,
            investigation_id=task.investigation_id,
            scope=rc.investigation.scope_type,
            hypothesis_id=task.hypothesis_id,
            hypothesis_code=hypothesis.code if hypothesis else "unspecified",
            question=sanitize_untrusted(task.question, 300)[0],
            requested_evidence=json.loads(task.requested_evidence),
            permitted_data=json.loads(task.data_scope).get("tables", []),
            assigned_agent=AgentId(task.agent_id),
            priority=task.priority,
            depends_on=json.loads(task.depends_on),
            deadline_seconds=task.deadline_seconds,
            budget=json.loads(task.budget),
            round=task.round,
            delegation_depth=task.delegation_depth,
            requested_by=task.requested_by,
            portfolio_segment=(
                rc.investigation.portfolio_segment
                if rc.investigation.scope_type == "portfolio"
                else (rc.snapshot.segment if rc.snapshot else None)
            ),
            context=context,
        )

    def _execute(self, rc: RunContext, spec: TaskSpec) -> dict:
        started = time.monotonic()
        ctx = ToolContext(spec.assigned_agent, spec, rc.cache, rc.llm)
        try:
            result = REGISTRY[spec.assigned_agent].run(ctx)
            error = None
        except Exception as exc:  # captured and classified by the graph
            result, error = None, exc
        return {
            "result": result,
            "error": error,
            "tools": ctx.calls,
            "ms": int((time.monotonic() - started) * 1000),
        }

    def _task_completed(self, rc: RunContext, task: AgentTask, outcome: dict) -> None:
        result = outcome["result"]
        if (
            not isinstance(result, AgentResult)
            or result.task_id != task.id
            or (result.agent_id.value != task.agent_id)
        ):
            self._task_failed(rc, task, "failed", "Result failed contract validation", retry=False)
            return
        for spec in result.findings:
            if spec.hypothesis_code in HYPOTHESES:
                self._ensure_hypothesis(rc, spec.hypothesis_code, task.agent_id, activate=False)
        findings, rejected = store.persist_result(
            rc.db, rc.investigation, task, result, rc.cache.all(), rc.snapshot, rc.calc_ids
        )
        task.status = "completed"
        task.completion_status = result.completion_status
        task.completed_at = store.now()
        for f in findings:
            store.emit(
                rc.db,
                rc.investigation,
                "finding_recorded",
                "agent",
                task.agent_id,
                {
                    "finding_id": f.id,
                    "code": f.code,
                    "direction": f.direction,
                    "confidence": f.confidence,
                },
                task_id=task.id,
            )
        if rejected:
            store.emit(
                rc.db,
                rc.investigation,
                "security_flag",
                SYSTEM,
                "evidence_validator",
                {"type": "evidence_reference_rejected", "items": rejected[:20]},
                task_id=task.id,
            )
        store.emit(
            rc.db,
            rc.investigation,
            "task_completed",
            "agent",
            task.agent_id,
            {
                "completion_status": result.completion_status,
                "findings": len(findings),
                "followups": [f.target_agent.value for f in result.followups],
                "llm_status": result.llm_status,
                "duration_ms": outcome["ms"],
                "tools": [c["tool"] for c in outcome["tools"]],
            },
            task_id=task.id,
        )
        rc.pending_followups.extend((task, f) for f in result.followups)

    def _task_failed(
        self, rc: RunContext, task: AgentTask, status: str, error: str, retry: bool = True
    ) -> None:
        task.error = error[:500]
        if retry and task.attempts <= rc.policy.budgets.max_task_retries:
            task.status = "authorised"
            store.emit(
                rc.db,
                rc.investigation,
                "task_retry_scheduled",
                SYSTEM,
                "graph",
                {"status": status, "error": task.error, "attempt": task.attempts},
                task_id=task.id,
            )
            return
        task.status = status
        task.completed_at = store.now()
        store.emit(
            rc.db,
            rc.investigation,
            f"task_{status}",
            SYSTEM,
            "graph",
            {"error": task.error},
            task_id=task.id,
        )

    def _runnable(self, view: InvestigationView) -> list[AgentTask]:
        done = {t.id for t in view.tasks if t.status == "completed"}
        return [
            t
            for t in view.tasks
            if t.status == "authorised" and all(d in done for d in json.loads(t.depends_on))
        ]

    def _ensure_hypothesis(
        self, rc: RunContext, code: str, source: str, activate: bool = True
    ) -> Hypothesis:
        h = store.hypothesis_by_code(rc.db, rc.investigation.id, code)
        if h is None:
            h = Hypothesis(
                investigation_id=rc.investigation.id,
                code=code,
                statement=HYPOTHESES[code].statement,
                status="open",
                priority=60,
                source=source,
                rationale=f"Raised by {source}",
                round_created=rc.investigation.current_round,
            )
            rc.db.add(h)
            rc.db.flush()
            store.emit(
                rc.db,
                rc.investigation,
                "hypothesis_created",
                "agent",
                source,
                {"hypothesis_id": h.id, "code": code, "status": "open"},
            )
        elif h.status == "deferred" and activate:
            h.status = "open"
            store.emit(
                rc.db,
                rc.investigation,
                "hypothesis_activated",
                "agent",
                source,
                {"hypothesis_id": h.id, "code": code},
            )
        return h

    def _create_task(
        self,
        rc: RunContext,
        request: TaskRequest,
        view: InvestigationView,
        reason: str = "",
        challenge: EvidenceChallenge | None = None,
    ):
        key = store.dedup_key(
            request.agent.value, request.hypothesis_code, rc.data_digest, request.extra_key
        )
        duplicate = store.find_duplicate(rc.db, rc.investigation.id, key)
        ok, why, depth = self.director.authorise(
            request, view, rc.budget, rc.policy, rc.investigation.scope_type, key, duplicate
        )
        payload = {
            "agent": request.agent.value,
            "hypothesis": request.hypothesis_code,
            "requested_by": request.requested_by,
            "reason": reason or request.question,
            "decision": why,
            "depth": depth,
            "challenge_id": challenge.id if challenge else None,
        }
        if not ok:
            store.emit(
                rc.db,
                rc.investigation,
                "delegation_rejected",
                "agent",
                DIRECTOR,
                payload,
                task_id=request.parent.id if request.parent else None,
            )
            return None
        h = self._ensure_hypothesis(rc, request.hypothesis_code, request.requested_by)
        task = AgentTask(
            investigation_id=rc.investigation.id,
            hypothesis_id=h.id,
            agent_id=request.agent.value,
            question=request.question,
            requested_evidence=json.dumps(request.requested_evidence),
            data_scope=json.dumps(
                {
                    "tables": sorted(DATA_SCOPES[request.agent]),
                    "customer_id": rc.investigation.customer_id,
                    "segment": rc.investigation.portfolio_segment,
                }
            ),
            priority=request.priority,
            depends_on="[]",
            requested_by=request.requested_by,
            parent_task_id=request.parent.id if request.parent else None,
            delegation_depth=depth,
            round=rc.investigation.current_round,
            status="authorised",
            dedup_key=key,
            deadline_seconds=rc.policy.budgets.task_timeout_seconds,
            budget=json.dumps({"max_llm_calls": 2}),
        )
        rc.db.add(task)
        rc.db.flush()
        event = "delegation_authorised" if request.parent or challenge else "task_authorised"
        store.emit(
            rc.db,
            rc.investigation,
            event,
            "agent",
            DIRECTOR,
            payload | {"task": task.id},
            task_id=task.id,
        )
        return task

    def _authorise_human_task(self, rc: RunContext, task: AgentTask) -> None:
        agent = AgentId(task.agent_id)
        h = rc.db.get(Hypothesis, task.hypothesis_id) if task.hypothesis_id else None
        # Portfolio scope admits only the portfolio specialist; customer scope admits all
        # specialists (the portfolio specialist then reports segment aggregates only).
        applicable = agent in SPECIALISTS and (
            rc.investigation.scope_type == "customer" or agent == AgentId.PORTFOLIO
        )
        if not applicable or not rc.budget.reserve_task():
            task.status = "rejected"
            task.error = "not applicable to scope" if not applicable else "budget exhausted"
            store.emit(
                rc.db,
                rc.investigation,
                "delegation_rejected",
                "agent",
                DIRECTOR,
                {"agent": task.agent_id, "requested_by": task.requested_by, "decision": task.error},
                task_id=task.id,
            )
            return
        task.status = "authorised"
        task.round = rc.investigation.current_round
        task.data_scope = json.dumps(
            {
                "tables": sorted(DATA_SCOPES[agent]),
                "customer_id": rc.investigation.customer_id,
                "segment": rc.investigation.portfolio_segment,
            }
        )
        if h is not None and h.status == "deferred":
            h.status = "open"
        store.emit(
            rc.db,
            rc.investigation,
            "task_authorised",
            "agent",
            DIRECTOR,
            {
                "agent": task.agent_id,
                "requested_by": task.requested_by,
                "decision": "authorised_human_request",
            },
            task_id=task.id,
        )

    def _update_hypotheses(self, rc: RunContext) -> None:
        view = load_view(rc.db, rc.investigation)
        for h in view.hypotheses:
            new = self.director.hypothesis_status(view, h)
            if new != h.status:
                old, h.status, h.updated_at = h.status, new, store.now()
                store.emit(
                    rc.db,
                    rc.investigation,
                    "hypothesis_status_changed",
                    "agent",
                    DIRECTOR,
                    {"hypothesis_id": h.id, "code": h.code, "from": old, "to": new},
                )
        rc.db.flush()

    def _close_reviews(self, rc: RunContext) -> None:
        for item in rc.db.scalars(
            select(ReviewItem).where(
                ReviewItem.investigation_id == rc.investigation.id, ReviewItem.status == "open"
            )
        ):
            item.status, item.completed_at = "superseded", store.now()

    def _open_review(
        self,
        rc: RunContext,
        queue: str,
        role: str,
        stage: str,
        reasons: list[str],
        adjudication_id: str | None = None,
    ) -> ReviewItem:
        self._close_reviews(rc)
        item = ReviewItem(
            investigation_id=rc.investigation.id,
            adjudication_id=adjudication_id,
            queue=queue,
            required_role=role,
            stage=stage,
            reasons=json.dumps(reasons),
            assigned_to=rc.investigation.assigned_analyst_id if role == "credit_analyst" else None,
        )
        rc.db.add(item)
        rc.db.flush()
        store.emit(
            rc.db,
            rc.investigation,
            "review_requested",
            SYSTEM,
            "graph",
            {
                "review_item_id": item.id,
                "queue": queue,
                "stage": stage,
                "role": role,
                "reasons": reasons,
            },
        )
        return item


_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()


def start_run(app_state, investigation_id: str, trigger: str) -> None:
    """Run synchronously (tests) or in a background thread (default)."""
    graph = InvestigationGraph(
        app_state.session_factory, app_state.settings, getattr(app_state, "llm_client", None)
    )
    if not app_state.settings.investigations_async:
        graph.run(investigation_id, trigger)
        return

    def target():
        try:
            graph.run(investigation_id, trigger)
        finally:
            with _RUNNING_LOCK:
                _RUNNING.discard(investigation_id)

    with _RUNNING_LOCK:
        if investigation_id in _RUNNING:
            return
        _RUNNING.add(investigation_id)
    threading.Thread(target=target, name=f"investigation-{investigation_id}", daemon=True).start()
