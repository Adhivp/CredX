from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from app.db.session import Base
from app.models import auth_session, ews, governance, investigation, monitoring, user  # noqa: F401
from app.models.investigation import APPEND_ONLY_TABLES


def test_migrations_upgrade_match_models_and_downgrade(tmp_path, monkeypatch):
    assert Path("alembic.ini").is_file(), "Database migrations are missing"
    monkeypatch.setenv("JWT_SECRET", "migration-test-key-" * 4)
    url = f"sqlite:///{tmp_path / 'migration.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    engine = create_engine(url)
    assert set(inspect(engine).get_table_names()) == set(Base.metadata.tables) | {"alembic_version"}
    command.check(config)
    with engine.connect() as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                text("select name from sqlite_master where type='trigger'")
            )
        }
    for table in APPEND_ONLY_TABLES:
        assert {f"trg_{table}_no_update", f"trg_{table}_no_delete"} <= triggers
    command.downgrade(config, "base")
    assert inspect(engine).get_table_names() == ["alembic_version"]
    engine.dispose()


def test_upgrade_adopts_database_created_by_old_startup_create_all(tmp_path, monkeypatch):
    """Databases bootstrapped by the previous startup create_all can be stamped and upgraded."""
    monkeypatch.setenv("JWT_SECRET", "migration-test-key-" * 4)
    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    config = Config("alembic.ini")
    command.upgrade(config, "0001")
    engine = create_engine(url)
    legacy = [ews.Customer.__table__, ews.WarningAssessment.__table__]
    Base.metadata.create_all(engine, tables=legacy)  # what create_all used to leave behind
    with engine.begin() as connection:
        connection.execute(
            text("insert into customers values ('c1', 'LEGACY-1', 'individual', '', 'retail', 0)")
        )
    command.upgrade(config, "head")
    with engine.connect() as connection:
        assert connection.execute(text("select customer_id from customers")).scalar() == "LEGACY-1"
    command.check(config)
    engine.dispose()
