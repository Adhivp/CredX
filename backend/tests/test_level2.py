"""Level 2 smoke tests on sample_data_v3: trends, correlation, prioritisation,
event reclassification, intervention approval and the portfolio dashboard."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

DATA = Path(__file__).resolve().parents[1] / "sample_data_v3"
API = "/api/v1/ews"


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    mp = pytest.MonkeyPatch()
    mp.setenv("JWT_SECRET", "test-only-secret-" * 4)
    mp.setenv("CORS_ORIGINS", "[]")
    mp.setenv("DATABASE_URL", f"sqlite:///{tmp_path_factory.mktemp('l2') / 'test.db'}")
    from app.main import create_app

    with TestClient(create_app()) as c:
        for t in ("customers", "loans", "repayments", "exposure", "cashflow"):
            with open(DATA / f"{t}.csv", "rb") as f:
                assert c.post(f"{API}/upload?data_type={t}", files={"file": (f"{t}.csv", f)}).json()["status"] == "success"
        assert c.post(f"{API}/assess").json()["history"]["snapshots"] > 300
        yield c
    mp.undo()


def _row(client, cid):
    items = client.get(f"{API}/monitoring/priority").json()["items"]
    return next(i for i in items if i["customer_id"] == cid)


def test_trend_correlation_and_priority(client):
    t7 = _row(client, "T3-007")
    assert t7["trend"] == "deteriorating" and t7["delinquency"]["bucket"] in ("SMA-1", "SMA-2", "NPA")
    assert t7["tier"] == "P1" and t7["probability_of_deterioration"] > 0
    # Same warning pattern, larger exposure -> higher expected loss -> higher priority.
    assert _row(client, "T3-003")["expected_loss"] > _row(client, "T3-004")["expected_loss"]
    corr = client.get(f"{API}/monitoring/customer/T3-003/trend").json()["correlation"]
    assert corr["families"] and corr["interactions"]  # SHAP family effects + learned combined effects


def test_processing_error_reclassification_recalculates(client):
    ws = client.get(f"{API}/workspace/T3-006").json()
    event = next(r for r in ws["repayments"] if r["dpd"] == 31)
    res = client.post(f"{API}/repayments/{event['id']}/classification",
                      json={"classification": "processing_error", "reason": "Auto-debit mandate failure", "actor": "qa"})
    assert res.status_code == 200
    body = res.json()
    assert body["excluded_from_scoring"] and body["score_after"] < body["score_before"]


def test_customer_impacting_intervention_needs_second_approver(client):
    users = []
    for i in (1, 2):
        acct = {"email": f"analyst{i}@example.com", "full_name": f"Analyst {i}", "password": "Long unique passphrase 42!"}
        client.post("/api/v1/auth/register", json=acct)
        tok = client.post("/api/v1/auth/login", json={"email": acct["email"], "password": acct["password"]}).json()
        users.append({"Authorization": f"Bearer {tok['access_token']}"})

    iv = client.post(f"{API}/interventions", headers=users[0],
                     json={"customer_id": "T3-007", "scenario": "sustained_high_risk"}).json()
    assert iv["status"] == "pending_approval" and iv["customer_impacting"]
    assert iv["contributing_indicators"]
    assert client.patch(f"{API}/interventions/{iv['id']}", json={"status": "initiated"}).status_code == 409
    approve = {"approve": True, "comment": "Agreed"}
    assert client.post(f"{API}/interventions/{iv['id']}/approve", json=approve).status_code == 401
    assert client.post(f"{API}/interventions/{iv['id']}/approve", json=approve, headers=users[0]).status_code == 403
    assert client.post(f"{API}/interventions/{iv['id']}/approve", json=approve, headers=users[1]).json()["status"] == "approved"
    assert client.patch(f"{API}/interventions/{iv['id']}", json={"status": "initiated"}).status_code == 200
    assert client.patch(f"{API}/interventions/{iv['id']}", json={"status": "completed"}).status_code == 422
    done = client.patch(f"{API}/interventions/{iv['id']}", json={"status": "completed", "outcome": "Handed to collections"})
    assert done.json()["status"] == "completed"


def test_dashboard_filters_and_delinquency_split(client):
    d = client.get(f"{API}/monitoring/dashboard").json()
    k = d["kpis"]
    assert k["delinquent_customers"] > 0 and k["early_warning_customers"] > 0
    assert d["trend_series"] and d["by_level"]
    sme = client.get(f"{API}/monitoring/dashboard", params={"portfolio": "sme_finance"}).json()
    assert 0 < sme["customers"] < d["customers"]
    assert all(g["name"] == "sme_finance" for g in sme["breakdown"]["portfolio"])
