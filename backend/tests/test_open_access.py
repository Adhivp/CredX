"""With RBAC_ENABLED off (the default) every signed-in account can use every feature."""


def test_unassigned_account_can_use_governed_features(client, account, monkeypatch):
    monkeypatch.setenv("RBAC_ENABLED", "false")
    assert client.post("/api/v1/auth/register", json=account).status_code == 201
    token = client.post(
        "/api/v1/auth/login", json={"email": account["email"], "password": account["password"]}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    for path in (
        "/api/v1/investigations",
        "/api/v1/reviews/queue",
        "/api/v1/admin/users",
        "/api/v1/admin/policies",
        "/api/v1/system/status",
        "/api/v1/audit/logs",
        "/api/v2/ews/watchlist",
        "/api/v2/ews/portfolio",
    ):
        assert client.get(path, headers=headers).status_code == 200, path
    queues = client.get("/api/v1/reviews/queue", headers=headers).json()["queues"]
    assert set(queues) == {"analyst_review", "authorised_approval", "portfolio_review"}


def test_anonymous_requests_are_still_rejected(client, monkeypatch):
    monkeypatch.setenv("RBAC_ENABLED", "false")
    assert client.get("/api/v1/investigations").status_code == 401
