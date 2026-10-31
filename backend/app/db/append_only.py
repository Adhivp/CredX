"""SQLite triggers that make record tables append-only.

Installed by the Alembic migration and, for test databases built with
``create_all``, by an ``after_create`` listener on each table.
"""

from sqlalchemy import DDL, Table, event


def append_only_trigger_sql(table: str) -> list[str]:
    return [
        f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_update BEFORE UPDATE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END",
        f"CREATE TRIGGER IF NOT EXISTS trg_{table}_no_delete BEFORE DELETE ON {table} "
        f"BEGIN SELECT RAISE(ABORT, '{table} is append-only'); END",
    ]


def _install(target: Table, connection, **_) -> None:
    for statement in append_only_trigger_sql(target.name):
        connection.execute(DDL(statement))


def make_append_only(table: Table) -> None:
    event.listen(table, "after_create", _install)
