from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    assert Path("app/db/session.py").is_file(), "Database layer is not implemented"
    from app.db.session import Base
    from app.main import create_app

    monkeypatch.setenv("JWT_SECRET", "test-only-secret-" * 4)
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
        "password": "Long unique passphrase 42!",
    }
