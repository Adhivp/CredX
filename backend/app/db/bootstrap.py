"""Prepare the SQLite database before `alembic upgrade head` runs.

Run as `python -m app.db.bootstrap` (the container CMD does this). Never deletes data:

1. Seed: if the database file is missing or empty and a seed file exists, copy it.
   The image ships the tracked backend/data/credx.db at /app/seed/credx.db, so a
   fresh container starts from the repository's data instead of an empty database.
   Copied login sessions are revoked; user accounts are kept.
2. Stamp: databases built by Base.metadata.create_all already have the tables from
   migration 0001 but no alembic_version row. Stamp them at 0001 so the upgrade
   does not fail with "table users already exists".
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from pathlib import Path

from sqlalchemy.engine import make_url

DEFAULT_SEED = Path("/app/seed/credx.db")
BASELINE_REVISION = "0001"


def _database_path() -> Path | None:
    from app.core.config import BACKEND_DIR, Settings

    raw = os.environ.get("DATABASE_URL") or Settings.model_fields["database_url"].default
    url = make_url(Settings.anchor_relative_sqlite(raw))
    if url.get_backend_name() != "sqlite" or not url.database or url.database == ":memory:":
        return None
    path = Path(url.database)
    return path if path.is_absolute() else (BACKEND_DIR / path).resolve()


def _tables(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def seed(path: Path, seed_path: Path) -> bool:
    if (path.exists() and path.stat().st_size > 0) or not seed_path.is_file():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".seeding")
    shutil.copyfile(seed_path, tmp)
    with sqlite3.connect(tmp) as conn:
        if "auth_sessions" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}:
            conn.execute("DELETE FROM auth_sessions")
    tmp.replace(path)
    return True


def stamp_if_unversioned(path: Path) -> bool:
    if not path.exists():
        return False
    tables = _tables(path)
    if "alembic_version" in tables or "users" not in tables:
        return False
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        conn.execute("INSERT INTO alembic_version VALUES (?)", (BASELINE_REVISION,))
    return True


def main() -> None:
    path = _database_path()
    if path is None:
        return
    seed_path = Path(os.environ.get("DB_SEED_PATH", DEFAULT_SEED))
    if seed(path, seed_path):
        print(f"bootstrap: seeded {path} from {seed_path}", flush=True)
    if stamp_if_unversioned(path):
        print(f"bootstrap: stamped {path} at revision {BASELINE_REVISION}", flush=True)


if __name__ == "__main__":
    main()
