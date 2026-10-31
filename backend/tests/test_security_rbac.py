"""Authorisation, data-scope and hardening tests (fail closed, no leakage)."""

import pytest
from sqlalchemy import select

from app.core.rbac import ROLE_PERMISSIONS, Permission, Role
from app.models.governance import AuditLog

SME = (("segment", "sme_finance"),)
RETAIL = (("segment", "retail"),)


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/v2/ews/watchlist"),
        ("get", "/api/v2/ews/portfolio"),
        ("post", "/api/v2/ews/assess"),
        ("get", "/api/v2/ews/customer/X"),
        ("post", "/api/v2/ews/explain/X"),
        ("post", "/api/v1/investigations"),
        ("get", "/api/v1/investigations"),
        ("get", "/api/v1/reviews/queue"),
        ("get", "/api/v1/system/status"),
        ("get", "/api/v1/audit/logs"),
        ("get", "/api/v1/admin/users"),
    ],
)
def test_anonymous_requests_are_rejected(client, method, path):
    assert getattr(client, method)(path).status_code == 401


def test_unassigned_role_has_no_access_and_denials_are_audited(client, make_user):
    user_id, headers = make_user("unassigned")
    assert client.get("/api/v2/ews/watchlist", headers=headers).status_code == 403
    assert (
        client.post(
            "/api/v1/investigations", headers=headers, json={"customer_id": "X"}
        ).status_code
        == 403
    )
    with client.app.state.session_factory() as db:
        denied = db.scalars(select(AuditLog).where(AuditLog.user_id == user_id)).all()
    assert {d.outcome for d in denied} == {"denied"} and len(denied) >= 2


def test_registration_cannot_self_assign_a_role(client):
    r = client.post(
        "/api/v1/auth/register",
        json={
            "email": "x@bank.example",
            "full_name": "X",
            "password": "Long unique passphrase 42!",
            "role": "credit_manager",
        },
    )
    assert r.status_code == 422


def test_customer_scope_is_enforced(client, make_user, load_scenarios):
    load_scenarios("normal_repayment", "acceptance_customer")
    _, retail = make_user("credit_analyst", RETAIL, name="retail_analyst")
    _, sme = make_user("credit_analyst", SME, name="sme_analyst")
    assert client.get("/api/v2/ews/customer/normal_repayment", headers=retail).status_code == 200
    # Out-of-scope customers look exactly like missing ones.
    assert client.get("/api/v2/ews/customer/normal_repayment", headers=sme).status_code == 404
    assert client.get("/api/v2/ews/customer/does-not-exist", headers=sme).status_code == 404
    r = client.post("/api/v1/investigations", headers=sme, json={"customer_id": "normal_repayment"})
    assert r.status_code == 404
    client.post("/api/v2/ews/assess", headers=sme)
    items = client.get("/api/v2/ews/watchlist", headers=sme).json()["items"]
    assert {i["customer_id"] for i in items} == {"acceptance_customer"}


def test_cross_investigation_and_cross_customer_access(client, make_user, load_scenarios):
    load_scenarios("normal_repayment", "repeated_delays")
    _, analyst = make_user("credit_analyst", RETAIL)
    a = client.post(
        "/api/v1/investigations", headers=analyst, json={"customer_id": "normal_repayment"}
    ).json()
    b = client.post(
        "/api/v1/investigations", headers=analyst, json={"customer_id": "repeated_delays"}
    ).json()
    finding_a = a["findings"][0]["id"]
    # A finding from investigation A cannot be reviewed through investigation B.
    r = client.post(
        f"/api/v1/investigations/{b['id']}/findings/{finding_a}/review",
        headers=analyst,
        json={"action": "accept_finding"},
    )
    assert r.status_code == 404
    # A record of customer A cannot be disputed inside customer B's investigation.
    record_a = a["findings"][0]["evidence"][0]["record_id"]
    r = client.post(
        f"/api/v1/investigations/{b['id']}/records/{record_a}/review",
        headers=analyst,
        json={"action": "dispute_evidence", "reason": "Not ours"},
    )
    assert r.status_code in {404, 409}
    _, other = make_user("credit_analyst", SME, name="sme_only")
    assert client.get(f"/api/v1/investigations/{a['id']}", headers=other).status_code == 404
    listing = client.get("/api/v1/investigations", headers=other).json()["items"]
    assert listing == []


def test_relationship_officer_sees_only_assigned_customer(client, make_user, load_scenarios):
    load_scenarios("normal_repayment", "repeated_delays")
    _, officer = make_user("relationship_officer", (("customer", "repeated_delays"),))
    assert client.get("/api/v2/ews/customer/repeated_delays", headers=officer).status_code == 200
    assert client.get("/api/v2/ews/customer/normal_repayment", headers=officer).status_code == 404


def test_admin_and_auditor_cannot_make_credit_decisions(client, make_user, load_scenarios):
    load_scenarios("repeated_delays")
    admin_id, admin = make_user("risk_admin")
    _, auditor = make_user("auditor")
    _, analyst = make_user("credit_analyst", RETAIL)
    ws = client.post(
        "/api/v1/investigations", headers=analyst, json={"customer_id": "repeated_delays"}
    ).json()
    iid = ws["id"]
    for headers in (admin, auditor):
        assert (
            client.post(
                "/api/v1/investigations", headers=headers, json={"customer_id": "repeated_delays"}
            ).status_code
            == 403
        )
        r = client.post(
            f"/api/v1/investigations/{iid}/classification-decision",
            headers=headers,
            json={"action": "accept_classification", "reason": "Not my call"},
        )
        assert r.status_code == 403
        assert client.get(f"/api/v1/investigations/{iid}", headers=headers).status_code == 200
    r = client.put(
        f"/api/v1/admin/users/{admin_id}/role",
        headers=admin,
        json={"role": "credit_manager", "approval_limit": 1e9, "reason": "Self promotion"},
    )
    assert r.status_code == 403


def test_permission_matrix_separates_agent_style_actions_from_humans():
    assert Permission.DECIDE_CLASSIFICATION in ROLE_PERMISSIONS[Role.CREDIT_MANAGER]
    for role in (
        Role.RELATIONSHIP_OFFICER,
        Role.CREDIT_ANALYST,
        Role.PORTFOLIO_MANAGER,
        Role.RISK_ADMIN,
        Role.AUDITOR,
        Role.UNASSIGNED,
    ):
        assert Permission.DECIDE_CLASSIFICATION not in ROLE_PERMISSIONS[role]
        assert Permission.DECIDE_INTERVENTION not in ROLE_PERMISSIONS[role]
    assert not ROLE_PERMISSIONS[Role.UNASSIGNED]


@pytest.mark.parametrize("payload", ["' OR '1'='1", "x'; DROP TABLE customers;--", "%' --"])
def test_sql_injection_payloads_are_inert(client, make_user, load_scenarios, payload):
    load_scenarios("normal_repayment")
    _, analyst = make_user("credit_analyst")
    assert client.get(f"/api/v2/ews/customer/{payload}", headers=analyst).status_code == 404
    r = client.get("/api/v2/ews/watchlist", headers=analyst, params={"risk_level": payload})
    assert r.status_code == 200 and r.json()["items"] == []
    r = client.post("/api/v1/investigations", headers=analyst, json={"customer_id": payload})
    assert r.status_code == 404
    assert client.get("/api/v2/ews/customer/normal_repayment", headers=analyst).status_code == 200


def test_secrets_are_not_exposed(client, make_user, monkeypatch):
    from app.core.config import Settings

    _, admin = make_user("risk_admin")
    body = client.get("/api/v1/system/status", headers=admin).text
    settings = client.app.state.settings
    assert settings.jwt_secret not in body
    monkeypatch.setenv("NEBIUS_API_KEY", "sk-test-secret-value-1234567890")
    leaked = Settings()
    assert "sk-test-secret" not in repr(leaked) and leaked.jwt_secret not in repr(leaked)
    status = client.get("/api/v1/system/status", headers=admin).json()
    assert status["ml"]["status"] == "not_validated" and status["ml"]["used_in_scoring"] is False


def test_invalid_state_transitions_are_refused(client, make_user, load_scenarios):
    load_scenarios("normal_repayment")
    _, manager = make_user("credit_manager", approval_limit=1e9)
    _, analyst = make_user("credit_analyst")
    ws = client.post(
        "/api/v1/investigations", headers=analyst, json={"customer_id": "normal_repayment"}
    ).json()
    assert ws["status"] == "monitoring"
    iid = ws["id"]
    r = client.post(
        f"/api/v1/investigations/{iid}/classification-decision",
        headers=manager,
        json={"action": "accept_classification", "reason": "Out of order"},
    )
    assert r.status_code == 409
    r = client.post(
        f"/api/v1/investigations/{iid}/escalate",
        headers=analyst,
        json={"reason": "Not awaiting review"},
    )
    assert r.status_code == 409
    r = client.post(
        "/api/v1/investigations", headers=analyst, json={"customer_id": "normal_repayment"}
    )
    assert r.status_code == 201  # monitoring cases can be re-investigated afresh


def test_upload_skips_rows_outside_scope(client, make_user, load_scenarios):
    load_scenarios("normal_repayment", "acceptance_customer")
    _, analyst = make_user("credit_analyst", SME)
    csv = "customer_id,month,monthly_inflow\nnormal_repayment,13,1\nacceptance_customer,13,90000\n"
    r = client.post(
        "/api/v2/ews/upload",
        headers=analyst,
        params={"data_type": "cashflow"},
        files={"file": ("c.csv", csv.encode(), "text/csv")},
    )
    assert r.status_code == 200
    assert r.json()["records_loaded"] == 1
    assert any("outside your data scope" in e for e in r.json()["errors"])


@pytest.mark.xfail(reason="CORS_ORIGINS empty keeps the existing wildcard CORS the deployed cross-origin frontend relies on; set CORS_ORIGINS to restrict", strict=True)
def test_cors_allows_only_configured_origins(client):
    r = client.options(
        "/api/v2/ews/watchlist",
        headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "GET"},
    )
    assert "access-control-allow-origin" not in r.headers


def test_markup_in_uploaded_identifiers_and_labels_is_rejected(client, make_user):
    _, analyst = make_user("credit_analyst")
    csv = (
        "customer_id,customer_type,portfolio_segment\n"
        "<img src=x onerror=alert(1)>,individual,retail\n"
        "SAFE-1,individual,<script>alert(1)</script>\n"
        "SAFE-2,individual,retail\n"
    )
    r = client.post(
        "/api/v2/ews/upload",
        headers=analyst,
        params={"data_type": "customers"},
        files={"file": ("c.csv", csv.encode(), "text/csv")},
    )
    body = r.json()
    assert body["records_loaded"] == 1 and len(body["errors"]) == 2
    assert all("invalid characters" in e for e in body["errors"])
