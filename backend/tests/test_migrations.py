from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_migrations_upgrade_match_models_and_downgrade(tmp_path, monkeypatch):
    assert Path("alembic.ini").is_file(), "Database migrations are missing"
    monkeypatch.setenv("JWT_SECRET", "migration-test-key-" * 4)
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    engine = create_engine(url)
    assert set(inspect(engine).get_table_names()) == {"users", "auth_sessions", "alembic_version"}
    command.check(config)
    command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    engine.dispose()
