from pathlib import Path

import pytest
from pydantic import ValidationError


def test_config_requires_a_strong_secret_and_uses_sqlite(monkeypatch):
    assert Path("app/core/config.py").is_file(), "Configuration module is missing"
    from app.core.config import Settings

    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
    with pytest.raises(ValidationError):
        Settings(_env_file=None, jwt_secret="weak")
    settings = Settings(_env_file=None, jwt_secret="x" * 48)
    assert settings.database_url.startswith("sqlite:///")
    assert settings.access_token_minutes == 10
    assert settings.cors_origins == []
