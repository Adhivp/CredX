"""Final acceptance scenario (specification section 14), end to end through the HTTP API.

Every step uses a distinct, role-holding user and asserts on persisted backend state,
so it demonstrates that humans steer the investigation without impersonating agents
and that agents cannot bypass human approval.
"""

import json

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DatabaseError

from app.models.investigation import Adjudication, HumanDecision, Investigation
from app.models.user import User

CUSTOMER = "ACC-1acceptance_customer"


def events(ws, kind):
    return [e for e in ws["events"] if e["type"] == kind]


@pytest.fixture
def actors(make_user):
    seg = (("segment", "sme_finance"),)
    return {
        "analyst": make_user("credit_analyst", seg, name="analyst"),
        "manager": make_user("credit_manager", seg, approval_limit=5_000_000, name="manager"),
        "junior_manager": make_user(
            "credit_manager", seg, approval_limit=100_000, name="junior_manager"
        ),
        "officer": make_user("relationship_officer", (("customer", CUSTOMER),), name="officer"),
        "auditor": make_user("auditor", name="auditor"),
    }


def test_acceptance_scenario_end_to_end(client, actors, load_scenarios):
    load_scenarios("acceptance_customer", prefix="ACC-1")
    analyst_id, analyst = actors["analyst"]
    manager_id, manager = actors["manager"]

    # 1-8. Analyst initiates; the agents investigate and pause on a material question.
    r = client.post(
        "/api/v1/investigations",
        headers=analyst,
        json={"customer_id": CUSTOMER, "question": "Assess recent deterioration"},
    )
    assert r.status_code == 201, r.text
    ws = r.json()
    iid = ws["id"]
    assert ws["status"] == "awaiting_analyst_review"

    codes = {h["code"] for h in ws["hypotheses"]}
    assert {"repayment_deterioration", "cashflow_decline", "temporary_disruption"} <= codes
    first_wave = events(ws, "wave_started")[0]["payload"]
    assert first_wave["parallel"] is True
    assert set(first_wave["agents"]) == {"repayment_specialist", "cashflow_specialist"}

    delegated = [e["payload"] for e in events(ws, "delegation_authorised")]
    assert any(
        d["agent"] == "exposure_specialist" and d["requested_by"] == "agent:repayment_specialist"
        for d in delegated
    )
    rejected = [e["payload"] for e in events(ws, "delegation_rejected")]
    assert any(r["decision"].startswith("duplicate_of:") for r in rejected)
    exposure_task = next(t for t in ws["tasks"] if t["agent"] == "exposure_specialist")
    assert exposure_task["parent_task_id"] and exposure_task["delegation_depth"] == 1
    assert any(f["agent"] == "exposure_specialist" and f["evidence"] for f in ws["findings"])

    challenges = {c["type"]: c for c in ws["challenges"]}
    assert "sustainment_untested" in challenges
    baseline_task = next(t for t in ws["tasks"] if t["agent"] == "baseline_specialist")
    assert baseline_task["requested_by"] == "evidence_challenger" and baseline_task["round"] == 2
    assert challenges["conflicting_evidence"]["status"] == "escalated"
    review = next(r for r in ws["reviews"] if r["status"] == "open")
    assert review["stage"] == "pre_adjudication" and review["required_role"] == "credit_analyst"
    assert ws["adjudications"] == []

    # Every evidence reference resolves to a real record of this customer.
    with client.app.state.session_factory() as db:
        for f in ws["findings"]:
            for e in f["evidence"]:
                if e["source_table"] == "portfolio_aggregates":
                    continue
                row = db.execute(
                    text(f"select id from {e['source_table']} where id = :id"),
                    {"id": e["record_id"]},
                ).first()
                assert row is not None

    # 9. Analyst adds customer context and requests one more investigation.
    r = client.post(
        f"/api/v1/investigations/{iid}/notes",
        headers=analyst,
        json={
            "kind": "context",
            "category": "temporary_disruption",
            "content": "RM reports the customer lost a major client in June and signed a "
            "replacement contract in August.",
        },
    )
    assert r.status_code == 200, r.text
    r = client.post(
        f"/api/v1/investigations/{iid}/tasks",
        headers=analyst,
        json={
            "specialist": "cashflow_specialist",
            "hypothesis_code": "human_reported_context",
            "question": "Do the latest inflows reflect the replacement contract?",
        },
    )
    assert r.status_code == 200, r.text
    ws = r.json()

    # 10-11. Agents incorporate the context; the Adjudicator classifies deterministically.
    human_task = next(t for t in ws["tasks"] if t["requested_by"] == f"human:{analyst_id}")
    assert human_task["status"] == "completed" and human_task["round"] == 3
    assert any(f["code"] == "human_context_assessment" for f in ws["findings"])
    assert any(f["code"] == "human_context_cashflow_check" for f in ws["findings"])
    assert all(f["agent"] != "human" for f in ws["findings"])  # humans never author findings
    assert ws["status"] == "awaiting_approval"
    adjudication = ws["adjudications"][-1]
    assert adjudication["pathway"] == "C"
    assert adjudication["classification"] in {
        "sustained_deterioration",
        "accelerating_deterioration",
    }
    assert any("unresolved" in r for r in adjudication["escalation_reasons"])

    # The adjudication's figures are reproducible from source records by trusted code.
    from app.intelligence.data import load_customer_snapshot
    from app.intelligence.scoring import evaluate_snapshot
    from app.services.policy import DEFAULT_POLICY

    with client.app.state.session_factory() as db:
        inv = db.get(Investigation, iid)
        recomputed = evaluate_snapshot(load_customer_snapshot(db, inv.customer_id), DEFAULT_POLICY)
    stored = adjudication["calculation"]["output"]["calculation_digests"]
    assert stored["repayment_profile"] == recomputed.calculations["repayment_profile"].input_digest
    assert adjudication["risk_score"] == recomputed.assessment.risk_score

    # Agents cannot approve: only role-holding humans pass the approval gate.
    _, officer = actors["officer"]
    _, junior = actors["junior_manager"]
    body = {"action": "accept_classification", "reason": "Agree with evidence"}
    assert (
        client.post(
            f"/api/v1/investigations/{iid}/classification-decision", headers=officer, json=body
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/investigations/{iid}/classification-decision", headers=analyst, json=body
        ).status_code
        == 403
    )
    r = client.post(
        f"/api/v1/investigations/{iid}/classification-decision", headers=junior, json=body
    )
    assert r.status_code == 403 and "authority" in r.json()["detail"]

    # 12. Authorised Credit Manager overrides with a recorded reason and decides actions.
    r = client.post(
        f"/api/v1/investigations/{iid}/classification-decision",
        headers=manager,
        json={"action": "override_classification", "reason": "x"},
    )
    assert r.status_code == 422  # reason too short / no new classification
    r = client.post(
        f"/api/v1/investigations/{iid}/classification-decision",
        headers=manager,
        json={
            "action": "override_classification",
            "classification": "emerging_deterioration",
            "severity": "high",
            "reason": "Replacement contract verified; treat as emerging, not "
            "sustained, pending two more months of data.",
        },
    )
    assert r.status_code == 200, r.text
    ws = r.json()
    assert ws["status"] == "awaiting_approval"  # interventions still undecided
    pending = [
        i for i in ws["adjudications"][-1]["proposed_interventions"] if i["requires_approval"]
    ]
    assert pending
    for i, intervention in enumerate(pending):
        action = "approve_intervention" if i == 0 else "reject_intervention"
        r = client.post(
            f"/api/v1/investigations/{iid}/interventions/{intervention['id']}/decision",
            headers=manager,
            json={"action": action, "reason": "Decision under delegated authority"},
        )
        assert r.status_code == 200, r.text
    ws = r.json()

    # 13. Decision, evidence and audit history are stored; the original is preserved.
    assert ws["status"] == "approved"
    effective = ws["effective_classification"]
    assert effective["source"] == "human_override"
    assert effective["classification"] == "emerging_deterioration"
    assert ws["adjudications"][-1]["classification"] == adjudication["classification"]
    approval = next(d for d in ws["decisions"] if d["action"] == "approve_intervention")
    assert approval["new_state"] == {
        "code": pending[0]["code"],
        "status": "approved_pending_downstream",
        "executed": False,
    }
    override = next(d for d in ws["decisions"] if d["action"] == "override_classification")
    assert override["user"]["id"] == manager_id and override["role"] == "credit_manager"
    assert override["previous_state"]["classification"] == adjudication["classification"]
    assert override["reason"] and override["policy_version_id"] == ws["policy"]["id"]
    with client.app.state.session_factory() as db:
        with pytest.raises(DatabaseError):
            db.execute(
                text("update adjudications set classification='low_risk' where id=:i"),
                {"i": adjudication["id"]},
            )
        db.rollback()
        with pytest.raises(DatabaseError):
            db.execute(text("delete from human_decisions"))
        db.rollback()
        user_ids = set(db.scalars(select(User.id)))
        assert {d.user_id for d in db.scalars(select(HumanDecision))} <= user_ids
    _, auditor = actors["auditor"]
    logs = client.get("/api/v1/audit/logs", headers=auditor, params={"resource_id": iid}).json()[
        "items"
    ]
    assert {"decision:override_classification", "decision:approve_intervention"} <= {
        log["action"] for log in logs
    }

    # 14. New repayment data arrives; monitoring decides whether to reopen.
    csv = (
        "customer_id,loan_id,due_date,payment_date,amount_due,amount_paid,dpd,repayment_status\n"
        f"{CUSTOMER},{CUSTOMER}-L1,2026-10-01,,35000,0,31,missed\n"
    )
    r = client.post(
        "/api/v1/ews/upload",
        headers=analyst,
        params={"data_type": "repayments"},
        files={"file": ("new.csv", csv.encode(), "text/csv")},
    )
    assert r.status_code == 200 and r.json()["records_loaded"] == 1, r.text
    ws = client.get(f"/api/v1/investigations/{iid}", headers=analyst).json()
    monitoring = ws["monitoring_events"][-1]
    assert monitoring["decision"] == "reopen"
    assert any("missed payment" in reason for reason in monitoring["evaluation"]["reasons"])
    assert ws["status"] in {"awaiting_approval", "awaiting_analyst_review"}
    assert len(ws["adjudications"]) == 2  # the earlier adjudication is preserved
    with client.app.state.session_factory() as db:
        assert db.get(Investigation, iid).reopen_count == 1
        assert db.scalar(select(Adjudication).where(Adjudication.id == adjudication["id"]))


def test_preparer_cannot_approve_own_case(client, make_user, load_scenarios):
    load_scenarios("acceptance_customer", prefix="ACC-1")
    seg = (("segment", "sme_finance"),)
    _, manager = make_user("credit_manager", seg, approval_limit=10_000_000, name="m1")
    ws = client.post(
        "/api/v1/investigations", headers=manager, json={"customer_id": CUSTOMER}
    ).json()
    iid = ws["id"]
    client.post(
        f"/api/v1/investigations/{iid}/complete-review",
        headers=manager,
        json={"reason": "Proceed to adjudication"},
    )
    ws = client.get(f"/api/v1/investigations/{iid}", headers=manager).json()
    assert ws["status"] == "awaiting_approval"
    r = client.post(
        f"/api/v1/investigations/{iid}/classification-decision",
        headers=manager,
        json={"action": "accept_classification", "reason": "Self approval attempt"},
    )
    assert r.status_code == 403 and "Four-eyes" in r.json()["detail"]
    decisions = json.loads(json.dumps(ws["decisions"]))
    assert not any(d["action"] == "accept_classification" for d in decisions)
