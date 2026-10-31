"""Dynamic, bounded agent investigation loop."""

import json
import time

import pytest
from sqlalchemy import select

from app.agents import specialists
from app.agents.budget import Budget
from app.agents.contracts import AgentId, AgentResult, EvidenceRef, FindingSpec, TaskSpec
from app.agents.director import Director, TaskRequest
from app.agents.graph import InvestigationGraph
from app.agents.llm import LLMGateway
from app.agents.tools import CalculationCache, ToolContext, ToolPermissionError
from app.agents.view import InvestigationView
from app.models.investigation import AgentTask, Investigation
from app.services.policy import DEFAULT_POLICY, BudgetPolicy, PolicyConfig


def start(client, headers, customer):
    r = client.post("/api/v1/investigations", headers=headers, json={"customer_id": customer})
    assert r.status_code == 201, r.text
    return r.json()


def agents_run(ws):
    return {t["agent"] for t in ws["tasks"] if t["status"] == "completed"}


def test_low_risk_case_selects_minimal_specialists_and_is_autonomous(
    client, make_user, load_scenarios
):
    load_scenarios("normal_repayment")
    _, analyst = make_user("credit_analyst")
    ws = start(client, analyst, "normal_repayment")
    assert agents_run(ws) == {"repayment_specialist"}
    plan = next(e for e in ws["events"] if e["type"] == "plan_created")["payload"]
    assert "baseline_specialist" in plan["not_selected"]
    assert ws["status"] == "monitoring" and ws["pathway"] == "A"
    assert ws["reviews"] == []
    assert ws["round"] == 1


def test_challenger_reopens_the_loop_with_a_new_round(client, make_user, load_scenarios):
    load_scenarios("repeated_delays")
    _, analyst = make_user("credit_analyst")
    ws = start(client, analyst, "repeated_delays")
    raised = [e for e in ws["events"] if e["type"] == "challenge_raised"]
    assert any(e["payload"]["type"] == "sustainment_untested" for e in raised)
    rounds = [e["payload"] for e in ws["events"] if e["type"] == "round_started"]
    assert [r["reason"] for r in rounds] == ["initial", "re-investigation"]
    baseline = next(t for t in ws["tasks"] if t["agent"] == "baseline_specialist")
    assert baseline["requested_by"] == "evidence_challenger" and baseline["round"] == 2
    challenge = next(c for c in ws["challenges"] if c["type"] == "sustainment_untested")
    assert challenge["status"] == "addressed" and challenge["followup_task_ids"]


def test_insufficient_history_is_not_treated_as_low_risk(client, make_user, load_scenarios):
    load_scenarios("new_customer")
    _, analyst = make_user("credit_analyst")
    ws = start(client, analyst, "new_customer")
    assert agents_run(ws) == {"integrity_specialist"}
    adjudication = ws["adjudications"][-1]
    assert adjudication["classification"] == "insufficient_evidence"
    assert adjudication["severity"] == "undetermined" and adjudication["risk_score"] is None
    assert ws["status"] == "awaiting_analyst_review"


def _view(tasks=()):
    inv = Investigation(id="inv", scope_type="customer")
    return InvestigationView(inv, [], list(tasks), [], [], [], [], [])


def _task(agent, depth=0, parent=None, task_id=None):
    return AgentTask(
        id=task_id or f"t-{agent}-{depth}",
        agent_id=agent.value,
        delegation_depth=depth,
        parent_task_id=parent,
    )


def test_director_delegation_controls():
    director = Director()
    policy = DEFAULT_POLICY
    budget = Budget(policy.budgets)
    repay = _task(AgentId.REPAYMENT)

    def ask(target, parent, code="obligation_pressure", duplicate=None, view=None, b=budget):
        request = TaskRequest(target, code, "q", 50, f"agent:{parent.agent_id}", parent=parent)
        return director.authorise(
            request, view or _view([parent]), b, policy, "customer", "k", duplicate
        )

    assert ask(AgentId.EXPOSURE, repay)[:2] == (True, "authorised")
    # Edge not in the delegation graph.
    assert ask(AgentId.PORTFOLIO, repay, "segment_wide_stress")[1].startswith(
        "delegation_not_permitted"
    )
    # Circular: cashflow -> exposure -> cashflow.
    cash = _task(AgentId.CASHFLOW, task_id="c")
    exposure = _task(AgentId.EXPOSURE, depth=1, parent="c", task_id="e")
    assert (
        ask(AgentId.CASHFLOW, exposure, "cashflow_decline", view=_view([cash, exposure]))[1]
        == "circular_delegation"
    )
    # Depth limit.
    deep = _task(AgentId.REPAYMENT, depth=policy.budgets.max_delegation_depth)
    assert ask(AgentId.EXPOSURE, deep)[1] == "max_delegation_depth"
    # Duplicate.
    assert ask(AgentId.EXPOSURE, repay, duplicate=_task(AgentId.EXPOSURE))[1].startswith(
        "duplicate_of:"
    )
    # Budget.
    tight = Budget(BudgetPolicy(max_tasks=1))
    assert tight.reserve_task()
    assert ask(AgentId.EXPOSURE, repay, b=tight)[1] == "budget_exhausted:max_tasks"
    # Portfolio scope admits only portfolio hypotheses.
    request = TaskRequest(AgentId.REPAYMENT, "repayment_deterioration", "q", 50, "director")
    assert director.authorise(request, _view(), budget, policy, "portfolio", "k", None)[1] == (
        "hypothesis_not_applicable_to_scope"
    )


def _publish(client, make_user, **budgets):
    _, admin = make_user("risk_admin")
    config = PolicyConfig(budgets=BudgetPolicy(**budgets)).model_dump()
    r = client.post(
        "/api/v1/admin/policies",
        headers=admin,
        json={"config": config, "reason": "Tight budget for test"},
    )
    assert r.status_code == 201, r.text


def test_exhausted_budget_returns_incomplete_not_a_forced_conclusion(
    client, make_user, load_scenarios
):
    load_scenarios("combined_deterioration")
    _publish(client, make_user, max_tasks=1)
    _, analyst = make_user("credit_analyst")
    ws = start(client, analyst, "combined_deterioration")
    assert ws["status"] == "incomplete"
    assert ws["adjudications"] == []
    assert ws["outcome"] == "budget_exhausted"
    assert any(r["stage"] == "incomplete" and r["status"] == "open" for r in ws["reviews"])
    rejected = [
        e["payload"]["decision"] for e in ws["events"] if e["type"] == "delegation_rejected"
    ]
    assert "budget_exhausted:max_tasks" in rejected


def test_task_timeout_is_retried_then_failed_without_conclusion(
    client, make_user, load_scenarios, monkeypatch
):
    load_scenarios("normal_repayment")
    _publish(client, make_user, task_timeout_seconds=0.3, max_task_retries=1)
    original = specialists.REGISTRY[AgentId.REPAYMENT].run

    def slow(ctx):
        time.sleep(1.0)
        return original(ctx)

    monkeypatch.setattr(specialists.REGISTRY[AgentId.REPAYMENT], "run", slow)
    _, analyst = make_user("credit_analyst")
    ws = start(client, analyst, "normal_repayment")
    task = ws["tasks"][0]
    assert task["status"] == "timeout" and task["attempts"] == 2
    assert any(e["type"] == "task_retry_scheduled" for e in ws["events"])
    assert ws["status"] == "incomplete" and ws["adjudications"] == []


class _Fabricator(specialists.Specialist):
    agent_id = AgentId.REPAYMENT

    def analyse(self, ctx):
        finding = FindingSpec(
            code="repayment_stable",
            hypothesis_code="repayment_deterioration",
            summary="fabricated",
            direction="contradicts",
            confidence=0.9,
            evidence=[
                EvidenceRef(source_table="repayment_records", record_id="not-a-real-id"),
                EvidenceRef(
                    source_table="cashflow_records", record_id=ctx.cache.snapshot.repayments[0].id
                ),
            ],
        )
        return [finding], [], [], "complete"


def test_fabricated_and_mislabelled_evidence_ids_are_rejected(
    client, make_user, load_scenarios, monkeypatch
):
    load_scenarios("normal_repayment")
    monkeypatch.setitem(specialists.REGISTRY, AgentId.REPAYMENT, _Fabricator())
    _, analyst = make_user("credit_analyst")
    ws = start(client, analyst, "normal_repayment")
    finding = next(f for f in ws["findings"] if f["summary"] == "fabricated")
    assert finding["evidence"] == []
    flag = next(
        e
        for e in ws["events"]
        if e["type"] == "security_flag" and e["payload"]["type"] == "evidence_reference_rejected"
    )
    assert {i["record_id"] for i in flag["payload"]["items"]} >= {"not-a-real-id"}
    assert len(flag["payload"]["items"]) == 2  # wrong source table is rejected too


class _Overreacher(specialists.Specialist):
    agent_id = AgentId.REPAYMENT

    def analyse(self, ctx):
        ctx.call("exposure_snapshots")  # not on the repayment allowlist
        raise AssertionError("unreachable")


def test_tool_allowlist_violation_fails_the_task(client, make_user, load_scenarios, monkeypatch):
    load_scenarios("normal_repayment")
    monkeypatch.setitem(specialists.REGISTRY, AgentId.REPAYMENT, _Overreacher())
    _, analyst = make_user("credit_analyst")
    ws = start(client, analyst, "normal_repayment")
    assert ws["tasks"][0]["status"] == "failed"
    assert ws["tasks"][0]["attempts"] == 1  # permission failures are not retried
    assert any(
        e["type"] == "security_flag" and e["payload"]["type"] == "tool_permission_denied"
        for e in ws["events"]
    )
    assert ws["status"] == "incomplete"


def test_tool_gateway_enforces_data_scope():
    spec = TaskSpec(
        task_id="t",
        investigation_id="i",
        scope="customer",
        hypothesis_id=None,
        hypothesis_code="x",
        question="q",
        requested_evidence=[],
        permitted_data=["loan_facilities"],
        assigned_agent=AgentId.REPAYMENT,
        priority=1,
        depends_on=[],
        deadline_seconds=1,
        budget={},
        round=1,
        delegation_depth=0,
        requested_by="director",
    )
    ctx = ToolContext(
        AgentId.REPAYMENT,
        spec,
        CalculationCache(None, DEFAULT_POLICY),
        LLMGateway(object(), Budget(BudgetPolicy())),
    )
    with pytest.raises(ToolPermissionError):
        ctx.call("repayment_history")  # allowlisted tool, but table outside the task scope
    with pytest.raises(ToolPermissionError):
        ctx.call("sql")
    assert [c["allowed"] for c in ctx.calls] == [False, False]


class _WrongContract(specialists.Specialist):
    agent_id = AgentId.REPAYMENT

    def run(self, ctx):
        return AgentResult(
            task_id="someone-else",
            agent_id=AgentId.REPAYMENT,
            findings=[],
            calculation_names=[],
            completion_status="complete",
        )


def test_results_violating_the_contract_are_rejected(
    client, make_user, load_scenarios, monkeypatch
):
    load_scenarios("normal_repayment")
    monkeypatch.setitem(specialists.REGISTRY, AgentId.REPAYMENT, _WrongContract())
    _, analyst = make_user("credit_analyst")
    ws = start(client, analyst, "normal_repayment")
    assert ws["tasks"][0]["status"] == "failed"
    assert ws["tasks"][0]["error"] == "Result failed contract validation"


def _new_investigation(client, customer_ref):
    from app.models.ews import Customer
    from app.services.policy import get_active_policy

    with client.app.state.session_factory() as db:
        policy, _ = get_active_policy(db, "default")
        customer = db.scalar(select(Customer).where(Customer.customer_id == customer_ref))
        inv = Investigation(
            scope_type="customer",
            customer_id=customer.id,
            initiated_by="test",
            policy_version_id=policy.id,
        )
        db.add(inv)
        db.commit()
        return inv.id


def test_cancellation_and_single_runner_lock(client, load_scenarios):
    load_scenarios("normal_repayment")
    state = client.app.state
    graph = InvestigationGraph(state.session_factory, state.settings)
    iid = _new_investigation(client, "normal_repayment")
    with state.session_factory() as db:
        db.get(Investigation, iid).run_state = "running"
        db.commit()
    assert graph.run(iid) == "already_running"
    with state.session_factory() as db:
        inv = db.get(Investigation, iid)
        inv.run_state, inv.cancel_requested = "idle", True
        db.commit()
    assert graph.run(iid) == "cancelled"


def test_identical_inputs_reproduce_identical_results(client, load_scenarios):
    load_scenarios("accelerating_deterioration")
    state = client.app.state
    graph = InvestigationGraph(state.session_factory, state.settings)
    outputs = []
    for _ in range(2):
        iid = _new_investigation(client, "accelerating_deterioration")
        graph.run(iid)
        with state.session_factory() as db:
            from app.models.investigation import Adjudication

            adj = db.scalar(select(Adjudication).where(Adjudication.investigation_id == iid))
            from app.models.investigation import CalculationRecord

            calc = db.get(CalculationRecord, adj.calculation_id)
            outputs.append(
                (
                    adj.classification,
                    adj.severity,
                    adj.risk_score,
                    adj.confidence,
                    json.loads(calc.output)["calculation_digests"],
                )
            )
    assert outputs[0] == outputs[1]
