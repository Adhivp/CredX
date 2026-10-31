"""Additive column upgrades for databases built by ``create_all`` before a model gained a column.

``create_all`` never alters an existing table, so a database created before ``users.role``
existed would fail every login query. Each column is added only when absent, with a
default, so existing rows and the existing pages are unaffected.
"""

from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

# table -> [(column, DDL fragment)]
_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "users": [
        ("role", "VARCHAR(32) NOT NULL DEFAULT 'unassigned'"),
        ("approval_limit", "FLOAT"),
    ],
}


def ensure_compat_columns(engine: Engine) -> None:
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    with engine.begin() as conn:
        for table, columns in _COLUMNS.items():
            if table not in tables:
                continue
            present = {c["name"] for c in insp.get_columns(table)}
            for name, ddl in columns:
                if name not in present:
                    conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))
