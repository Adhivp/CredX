"""Human-in-the-loop behaviours beyond the acceptance scenario."""

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from app.models.investigation import HumanDecision, Investigation
from app.services import investigations as service


def start(client, headers, customer, **extra):
    r = client.post(
        "/api/v1/investigations", headers=headers, json={"customer_id": customer, **extra}
    )
    assert r.status_code == 201, r.text
    return r.json()


def test_rejecting_a_finding_requires_a_reason_and_changes_the_next_adjudication(
    client, make_user, load_scenarios
):
    load_scenarios("declining_inflows")
    _, analyst = make_user("credit_analyst")
    ws = start(client, analyst, "declining_inflows")
    iid = ws["id"]
    assert ws["status"] == "awaiting_analyst_review"
    first = ws["adjudications"][-1]
    cash = next(f for f in ws["findings"] if f["code"] == "cashflow_decline")
    url = f"/api/v1/investigations/{iid}/findings/{cash['id']}/review"
    assert client.post(url, headers=analyst, json={"action": "reject_finding"}).status_code == 422
    r = client.post(
        url,
        headers=analyst,
        json={
            "action": "reject_finding",
            "reason": "Inflow feed for the last quarter is known to be incomplete",
        },
    )
    assert r.status_code == 200
    ws = r.json()
    reviewed = next(f for f in ws["findings"] if f["id"] == cash["id"])
    assert reviewed["human_review"]["action"] == "reject_finding"
    assert reviewed["summary"] == cash["summary"]  # agent output untouched
    r = client.post(
        f"/api/v1/investigations/{iid}/tasks",
        headers=analyst,
        json={"specialist": "integrity_specialist"},
    )
    ws = r.json()
    latest = ws["adjudications"][-1]
    assert latest["calculation"]["output"]["excluded_families"] == ["cashflow"]
    assert latest["classification"] != first["classification"]
    assert ws["adjudications"][0] == first  # earlier adjudication preserved


def test_disputed_and_confirmed_records_are_traceable(client, make_user, load_scenarios):
    load_scenarios("contradictory_records")
    _, analyst = make_user("credit_analyst")
    ws = start(client, analyst, "contradictory_records")
    iid = ws["id"]
    contradictory = next(f for f in ws["findings"] if f["code"] == "contradictory_records")
    record_id = contradictory["evidence"][0]["record_id"]
    r = client.post(
        f"/api/v1/investigations/{iid}/records/{record_id}/review",
        headers=analyst,
        json={"action": "confirm_evidence", "reason": "Core banking shows this was 22 days late"},
    )
    assert r.status_code == 200, r.text
    ws = client.post(
        f"/api/v1/investigations/{iid}/complete-review",
        headers=analyst,
        json={"reason": "Re-adjudicate with confirmed record"},
    )
    decision = next(d for d in ws.json()["decisions"] if d["action"] == "confirm_evidence")
    assert decision["evidence_refs"] == [{"table": "repayment_records", "id": record_id}]


def test_relationship_officer_contributes_context_but_cannot_decide(
    client, make_user, load_scenarios
):
    load_scenarios("repeated_delays")
    _, officer = make_user("relationship_officer", (("customer", "repeated_delays"),))
    ws = start(
        client,
        officer,
        "repeated_delays",
        context="Customer reports payroll delays at employer",
        context_category="income_change",
    )
    iid = ws["id"]
    assert any(n["category"] == "income_change" for n in ws["notes"])
    assert any(h["code"] == "human_reported_context" for h in ws["hypotheses"])
    finding = ws["findings"][0]["id"]
    r = client.post(
        f"/api/v1/investigations/{iid}/findings/{finding}/review",
        headers=officer,
        json={"action": "accept_finding"},
    )
    assert r.status_code == 403
    r = client.post(
        f"/api/v1/investigations/{iid}/complete-review",
        headers=officer,
        json={"reason": "Looks fine to me"},
    )
    assert r.status_code == 403


def test_path_b_completion_and_escalation(client, make_user, load_scenarios):
    load_scenarios("increasing_utilisation", "partial_repayments")
    _, analyst = make_user("credit_analyst")
    ws = start(client, analyst, "increasing_utilisation")
    assert ws["pathway"] == "B" and ws["status"] == "awaiting_analyst_review"
    r = client.post(
        f"/api/v1/investigations/{ws['id']}/complete-review",
        headers=analyst,
        json={
            "reason": "Agree; monitor utilisation monthly",
            "recommended_classification": "emerging_deterioration",
        },
    )
    assert r.status_code == 200 and r.json()["status"] == "monitoring"
    ws = start(client, analyst, "partial_repayments")
    r = client.post(
        f"/api/v1/investigations/{ws['id']}/escalate",
        headers=analyst,
        json={"reason": "Partial payments warrant manager review"},
    )
    assert r.status_code == 200 and r.json()["status"] == "awaiting_approval"
    _, manager = make_user("credit_manager", approval_limit=10_000_000)
    queue = client.get("/api/v1/reviews/queue", headers=manager).json()["items"]
    assert any(
        i["queue"] == "authorised_approval" and i["investigation"]["id"] == ws["id"] for i in queue
    )


def test_monitoring_instructions_trigger_reopening(client, make_user, load_scenarios):
    load_scenarios("isolated_late_payment")
    _, analyst = make_user("credit_analyst")
    ws = start(client, analyst, "isolated_late_payment")
    iid = ws["id"]
    assert ws["status"] == "monitoring"
    r = client.post(f"/api/v1/investigations/{iid}/monitoring/evaluate", headers=analyst)
    assert r.json()["monitoring_events"][-1]["decision"] == "no_change"
    r = client.put(
        f"/api/v1/investigations/{iid}/monitoring",
        headers=analyst,
        json={"reopen_if_dpd_over": 0, "reason": "Zero tolerance after a delay"},
    )
    assert r.json()["monitoring_instructions"] == {"reopen_if_dpd_over": 0, "note": ""}
    csv = (
        "customer_id,loan_id,due_date,payment_date,amount_due,amount_paid,dpd,"
        "repayment_status\nisolated_late_payment,isolated_late_payment-L1,2026-10-01,"
        "2026-10-04,20000,20000,3,late\n"
    )
    client.post(
        "/api/v1/ews/upload",
        headers=analyst,
        params={"data_type": "repayments"},
        files={"file": ("r.csv", csv.encode(), "text/csv")},
    )
    ws = client.get(f"/api/v1/investigations/{iid}", headers=analyst).json()
    assert ws["monitoring_events"][-1]["decision"] == "reopen"
    assert ws["status"] != "monitoring" or ws["round"] > 1
    assert any(
        e["type"] == "run_started" and e["payload"]["trigger"] == "monitoring_reopen"
        for e in ws["events"]
    )


def test_portfolio_investigation_and_decision(client, make_user, load_scenarios):
    load_scenarios(
        "repeated_delays",
        "combined_deterioration",
        "sustained_deterioration",
        "normal_repayment",
        "accelerating_deterioration",
    )
    _, pm = make_user("portfolio_manager", (("segment", "retail"),), name="pm")
    _, analyst = make_user("credit_analyst", (("segment", "retail"),))
    intel = client.get("/api/v1/portfolio/intelligence", headers=pm).json()
    assert intel["customers"] == 5 and intel["clusters"] == []
    assert "customer_id" not in str(intel)  # aggregates only
    r = client.post(
        "/api/v1/investigations",
        headers=pm,
        json={"scope": "portfolio", "portfolio_segment": "retail"},
    )
    assert r.status_code == 201, r.text
    ws = r.json()
    assert agents_only(ws) == {"portfolio_specialist"}
    assert ws["status"] == "awaiting_analyst_review"
    assert (
        client.post(
            f"/api/v1/investigations/{ws['id']}/portfolio-decision",
            headers=analyst,
            json={"action": "accept_portfolio_assessment", "reason": "Agree"},
        ).status_code
        == 403
    )
    r = client.post(
        f"/api/v1/investigations/{ws['id']}/portfolio-decision",
        headers=pm,
        json={
            "action": "accept_portfolio_assessment",
            "reason": "Retail deterioration noted for the risk committee",
        },
    )
    assert r.status_code == 200 and r.json()["status"] == "monitoring"
    small = client.get(
        "/api/v1/portfolio/intelligence", headers=pm, params={"segment": "retail"}
    ).json()
    assert all(not g["suppressed"] for g in small["segments"])
    _, sme_pm = make_user("portfolio_manager", (("segment", "sme_finance"),), name="pm2")
    r = client.post(
        "/api/v1/investigations",
        headers=sme_pm,
        json={"scope": "portfolio", "portfolio_segment": "retail"},
    )
    assert r.status_code == 404


def agents_only(ws):
    return {t["agent"] for t in ws["tasks"]}


def test_small_cells_are_suppressed(client, make_user, load_scenarios):
    load_scenarios("normal_repayment", "acceptance_customer")
    _, pm = make_user("portfolio_manager")
    intel = client.get("/api/v1/portfolio/intelligence", headers=pm).json()
    assert all(g["suppressed"] for g in intel["segments"])


def test_cancel_requires_reason_and_is_recorded(client, make_user, load_scenarios):
    load_scenarios("acceptance_customer")
    _, analyst = make_user("credit_analyst")
    ws = start(client, analyst, "acceptance_customer")
    url = f"/api/v1/investigations/{ws['id']}/cancel"
    assert client.post(url, headers=analyst, json={"reason": "no"}).status_code == 422
    r = client.post(url, headers=analyst, json={"reason": "Customer repaid in full"})
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert r.json()["reviews"][-1]["status"] == "cancelled"


def test_agents_cannot_record_human_decisions(client, load_scenarios):
    load_scenarios("normal_repayment")
    with client.app.state.session_factory() as db:
        inv = Investigation(scope_type="customer", initiated_by="x", policy_version_id="p")
        agent_principal = type(
            "Agent", (), {"id": "credit_risk_adjudicator", "role": "agent", "is_active": True}
        )()
        with pytest.raises(HTTPException) as excinfo:
            service.decide(db, agent_principal, inv, "accept_classification")
        assert excinfo.value.status_code == 403
        db.add(
            HumanDecision(
                investigation_id="missing",
                user_id="credit_risk_adjudicator",
                role="agent",
                action="approve_intervention",
                policy_version_id="p",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
