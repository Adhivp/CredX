"""Create (or update the role of) a user. Self-registration never grants a role, so the
first risk administrator is bootstrapped with this script.

    python -m scripts.create_user --email admin@bank.example --name "Risk Admin" \\
        --role risk_admin
    python -m scripts.create_user --email m@bank.example --name "Credit Manager" \\
        --role credit_manager --scope segment:sme_finance --approval-limit 5000000

The password is read from CREDX_USER_PASSWORD or prompted for; it is never printed.
Run migrations first (alembic upgrade head).
"""

import argparse
import getpass
import os

from sqlalchemy import select

from app.core.config import Settings
from app.core.rbac import Role
from app.core.security import password_hasher
from app.db.session import configure_database
from app.models import auth_session, ews, governance, investigation, user  # noqa: F401
from app.models.governance import AuditLog, UserScope
from app.models.user import User


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a CredX user with a role")
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--role", required=True, choices=[r.value for r in Role])
    parser.add_argument(
        "--scope",
        action="append",
        default=[],
        help="all | segment:<name> | customer:<id> (repeatable)",
    )
    parser.add_argument("--approval-limit", type=float)
    args = parser.parse_args()

    password = os.environ.get("CREDX_USER_PASSWORD") or getpass.getpass("Password: ")
    if not 12 <= len(password) <= 128:
        raise SystemExit("Password must be 12-128 characters")
    _, factory = configure_database(Settings().database_url)
    with factory() as db:
        account = db.scalar(select(User).where(User.email == args.email.lower()))
        if account is None:
            account = User(
                email=args.email.lower(),
                full_name=args.name,
                password_hash=password_hasher.hash(password),
            )
            db.add(account)
        account.role = args.role
        account.approval_limit = args.approval_limit if args.role == "credit_manager" else None
        db.flush()
        for scope in args.scope:
            kind, _, value = scope.partition(":")
            if kind not in {"all", "segment", "customer"}:
                raise SystemExit(f"Invalid scope {scope}")
            exists = db.scalar(
                select(UserScope).where(
                    UserScope.user_id == account.id,
                    UserScope.scope_type == kind,
                    UserScope.scope_value == value,
                )
            )
            if exists is None:
                db.add(UserScope(user_id=account.id, scope_type=kind, scope_value=value))
        db.add(
            AuditLog(
                action="cli_create_user",
                resource_type="user",
                resource_id=account.id,
                outcome="allowed",
                detail=f'{{"role": "{args.role}"}}',
            )
        )
        db.commit()
        print(f"User {account.email} has role {account.role}")


if __name__ == "__main__":
    main()
