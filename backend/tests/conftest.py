from pathlib import Path

import pytest
from fastapi.testclient import TestClient

PASSWORD = "Long unique passphrase 42!"


@pytest.fixture
def client(tmp_path, monkeypatch):
    assert Path("app/db/session.py").is_file(), "Database layer is not implemented"
    from app.db.session import Base
    from app.main import create_app

    monkeypatch.setenv("JWT_SECRET", "test-only-secret-" * 4)
    monkeypatch.setenv("CORS_ORIGINS", "[]")
    monkeypatch.setenv("ACCESS_TOKEN_MINUTES", "10")
    # The security suites test the role controls, which are off by default.
    monkeypatch.setenv("RBAC_ENABLED", "true")
    # Tests never call the real LLM provider, even when backend/.env holds a key.
    monkeypatch.setenv("LLM_ENABLED", "false")
    monkeypatch.setenv("NEBIUS_API_KEY", "")
    monkeypatch.setenv("INVESTIGATIONS_ASYNC", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    app = create_app()
    Base.metadata.create_all(app.state.engine)
    with TestClient(app) as client:
        yield client


@pytest.fixture
def account():
    return {
        "email": "alice@example.com",
        "full_name": "Alice Example",
        "password": PASSWORD,
    }


@pytest.fixture
def make_user(client):
    """Create a user with a role and data scopes; return (user_id, auth headers)."""
    from app.models.governance import UserScope
    from app.models.user import User

    def factory(role, scopes=(("all", ""),), approval_limit=None, name=None):
        name = name or role
        email = f"{name}@bank.example"
        assert (
            client.post(
                "/api/v1/auth/register",
                json={
                    "email": email,
                    "full_name": name.replace("_", " ").title(),
                    "password": PASSWORD,
                },
            ).status_code
            == 201
        )
        with client.app.state.session_factory() as db:
            user = db.query(User).filter_by(email=email).one()
            user.role = role
            user.approval_limit = approval_limit
            for scope_type, value in scopes:
                db.add(UserScope(user_id=user.id, scope_type=scope_type, scope_value=value))
            db.commit()
            user_id = user.id
        tokens = client.post(
            "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
        ).json()
        return user_id, {"Authorization": f"Bearer {tokens['access_token']}"}

    return factory


@pytest.fixture
def load_scenarios(client):
    """Load benchmark scenarios (by key) into the test database."""
    from app.services.ingestion import load_dataset
    from evaluation.scenarios import ACCEPTANCE, SCENARIOS, build_records

    pool = {s.key: s for s in [*SCENARIOS, ACCEPTANCE]}

    def loader(*keys, prefix=""):
        with client.app.state.session_factory() as db:
            for key in keys:
                load_dataset(db, build_records(pool[key], f"{prefix}{key}"))
            db.commit()

    return loader
