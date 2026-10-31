from sqlalchemy import select

from app.main import bootstrap_admin
from app.models.governance import AuditLog
from app.models.user import User


def test_bootstrap_promotes_only_while_no_admin_exists(client, account):
    client.post("/api/v1/auth/register", json=account)
    client.post("/api/v1/auth/register", json=account | {"email": "bob@example.com"})
    factory = client.app.state.session_factory
    bootstrap_admin(factory, "nobody@example.com")  # unknown users are ignored
    bootstrap_admin(factory, "ALICE@example.com")
    bootstrap_admin(factory, "bob@example.com")  # an admin already exists
    with factory() as db:
        roles = dict(db.execute(select(User.email, User.role)).all())
        assert roles == {"alice@example.com": "risk_admin", "bob@example.com": "unassigned"}
        assert db.scalar(select(AuditLog).where(AuditLog.action == "bootstrap_admin"))
