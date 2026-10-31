from sqlalchemy import select


def test_login_returns_jwt_and_allows_private_profile(client, account):
    assert client.post("/api/v1/auth/register", json=account).status_code == 201
    response = client.post(
        "/api/v1/auth/login", json={"email": account["email"], "password": account["password"]}
    )
    assert response.status_code == 200
    tokens = response.json()
    assert tokens["token_type"] == "bearer"
    assert tokens["expires_in"] == 600
    assert len(tokens["access_token"].split(".")) == 3
    assert len(tokens["refresh_token"].split(".")) == 3
    profile = client.get(
        "/api/v1/users/me", headers={"Authorization": f"Bearer {tokens['access_token']}"}
    )
    assert profile.status_code == 200
    assert profile.json()["email"] == account["email"]


def login_tokens(client, account):
    assert client.post("/api/v1/auth/register", json=account).status_code == 201
    response = client.post(
        "/api/v1/auth/login", json={"email": account["email"], "password": account["password"]}
    )
    assert response.status_code == 200
    return response.json()


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_refresh_rotates_and_replay_revokes_the_session(client, account):
    first = login_tokens(client, account)
    response = client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert response.status_code == 200
    second = response.json()
    assert first["refresh_token"] != second["refresh_token"]
    assert client.get("/api/v1/users/me", headers=bearer(second["access_token"])).status_code == 200
    replay = client.post("/api/v1/auth/refresh", json={"refresh_token": first["refresh_token"]})
    assert replay.status_code == 401
    assert client.get("/api/v1/users/me", headers=bearer(second["access_token"])).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/refresh", json={"refresh_token": second["refresh_token"]}
        ).status_code
        == 401
    )


def test_logout_revokes_access_and_refresh_tokens(client, account):
    tokens = login_tokens(client, account)
    response = client.post("/api/v1/auth/logout", headers=bearer(tokens["access_token"]))
    assert response.status_code == 204
    assert response.content == b""
    assert client.get("/api/v1/users/me", headers=bearer(tokens["access_token"])).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        ).status_code
        == 401
    )


def test_lockout_blocks_correct_password_until_window_expires(client, account):
    import time

    from app.models.user import User

    login_tokens(client, account)
    credentials = {"email": account["email"], "password": "wrong password"}
    for _ in range(client.app.state.settings.login_max_attempts):
        assert client.post("/api/v1/auth/login", json=credentials).status_code == 401
    credentials["password"] = account["password"]
    assert client.post("/api/v1/auth/login", json=credentials).status_code == 401
    with client.app.state.session_factory() as db:
        user = db.scalar(select(User).where(User.email == account["email"]))
        user.locked_until = int(time.time()) - 1
        db.commit()
    assert client.post("/api/v1/auth/login", json=credentials).status_code == 200


def test_registration_stores_only_password_hash(client, account):
    response = client.post("/api/v1/auth/register", json=account)
    assert response.status_code == 201
    body = response.json()
    assert body["email"] == account["email"]
    assert body["full_name"] == account["full_name"]
    assert "password" not in body and "password_hash" not in body
    from app.models.user import User

    with client.app.state.session_factory() as db:
        user = db.scalar(select(User).where(User.email == account["email"]))
        assert user.password_hash.startswith("$argon2id$")
        assert account["password"] not in user.password_hash
