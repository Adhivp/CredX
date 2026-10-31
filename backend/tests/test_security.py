import time
from concurrent.futures import ThreadPoolExecutor

import jwt
import pytest
from sqlalchemy import select

from app.models.auth_session import AuthSession
from app.models.user import User
from tests.test_auth import bearer, login_tokens


def test_duplicate_registration_is_case_insensitive(client, account):
    assert client.post("/api/v1/auth/register", json=account).status_code == 201
    account["email"] = account["email"].upper()
    assert client.post("/api/v1/auth/register", json=account).status_code == 409


@pytest.mark.parametrize(
    "changes",
    [
        {"password": "too-short"},
        {"password": "p" * 129},
        {"email": "not-an-email"},
        {"full_name": "   "},
        {"is_active": True},
    ],
)
def test_registration_validation_does_not_echo_secrets(client, account, changes):
    account.update(changes)
    response = client.post("/api/v1/auth/register", json=account)
    assert response.status_code == 422
    assert account["password"] not in response.text
    assert all("input" not in error for error in response.json()["detail"])


def test_auth_responses_are_not_cacheable(client, account):
    response = client.post("/api/v1/auth/register", json=account)
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_unknown_user_and_wrong_password_have_identical_errors(client, account):
    login_tokens(client, account)
    wrong = client.post(
        "/api/v1/auth/login", json={"email": account["email"], "password": "incorrect password"}
    )
    unknown = client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "incorrect password"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()


@pytest.mark.parametrize("token", ["", "invalid", "e30.e30.invalid"])
def test_missing_and_malformed_bearer_tokens_are_rejected(client, token):
    response = client.get("/api/v1/users/me", headers=bearer(token) if token else {})
    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "change",
    [
        "expired",
        "issuer",
        "audience",
        "signature",
        "algorithm",
        "missing-sub",
        "missing-exp",
        "future",
        "session",
    ],
)
def test_invalid_jwt_claims_are_rejected(client, account, change):
    tokens = login_tokens(client, account)
    settings = client.app.state.settings
    claims = jwt.decode(tokens["access_token"], options={"verify_signature": False})
    key = settings.jwt_secret
    algorithm = "HS256"
    if change == "expired":
        claims["exp"] = int(time.time()) - 1
    elif change == "issuer":
        claims["iss"] = "other-api"
    elif change == "audience":
        claims["aud"] = "other-client"
    elif change == "signature":
        key = "wrong-key-" * 8
    elif change == "algorithm":
        algorithm = "HS384"
    elif change.startswith("missing-"):
        del claims[change.removeprefix("missing-")]
    elif change == "future":
        claims["nbf"] = int(time.time()) + 3600
    elif change == "session":
        claims["sid"] = "nonexistent-session"
    token = jwt.encode(claims, key, algorithm=algorithm)
    assert client.get("/api/v1/users/me", headers=bearer(token)).status_code == 401


def test_access_and_refresh_tokens_are_not_interchangeable(client, account):
    tokens = login_tokens(client, account)
    assert (
        client.get("/api/v1/users/me", headers=bearer(tokens["refresh_token"])).status_code == 401
    )
    assert (
        client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["access_token"]}
        ).status_code
        == 401
    )


def test_inactive_users_cannot_login_or_use_existing_tokens(client, account):
    tokens = login_tokens(client, account)
    with client.app.state.session_factory() as db:
        user = db.scalar(select(User).where(User.email == account["email"]))
        user.is_active = False
        db.commit()
    assert client.get("/api/v1/users/me", headers=bearer(tokens["access_token"])).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login", json={"email": account["email"], "password": account["password"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )


def test_refresh_reports_actual_remaining_access_lifetime(client, account):
    tokens = login_tokens(client, account)
    with client.app.state.session_factory() as db:
        session = db.scalar(select(AuthSession))
        session.expires_at = int(time.time()) + 30
        db.commit()
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert response.status_code == 200
    assert 0 < response.json()["expires_in"] <= 30


def test_database_contains_only_refresh_digest(client, account):
    tokens = login_tokens(client, account)
    with client.app.state.session_factory() as db:
        session = db.scalar(select(AuthSession))
        assert len(session.refresh_digest) == 64
        assert session.refresh_digest != tokens["refresh_token"]


def test_concurrent_refresh_has_at_most_one_winner(client, account):
    tokens = login_tokens(client, account)

    def refresh(_):
        return client.post("/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]})

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(refresh, range(2)))
    assert sorted(response.status_code for response in responses) == [200, 401]
    winner = next(response.json() for response in responses if response.status_code == 200)
    assert client.get("/api/v1/users/me", headers=bearer(winner["access_token"])).status_code == 401


def test_cors_allows_explicitly_configured_origin(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    from app.main import create_app

    monkeypatch.setenv("JWT_SECRET", "cors-test-key-" * 4)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'cors.db'}")
    monkeypatch.setenv("CORS_ORIGINS", '["http://localhost:3000"]')
    with TestClient(create_app()) as client:
        response = client.options(
            "/api/v1/auth/login",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


def test_cors_denies_unlisted_origin(client):
    response = client.options(
        "/api/v1/auth/login",
        headers={"Origin": "https://untrusted.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in response.headers
