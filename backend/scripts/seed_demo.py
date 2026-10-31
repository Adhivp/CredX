"""Seed a local demo: benchmark customers, role-holding demo users and assessments.

    alembic upgrade head
    python -m scripts.seed_demo

Passwords come from CREDX_DEMO_PASSWORD or are generated and printed once. Demo users
are for local development only. Re-running skips users and customers that exist.
"""

import os
import secrets

from sqlalchemy import select

from app.core.config import Settings
from app.core.security import password_hasher
from app.db.session import configure_database
from app.intelligence.data import load_customer_snapshot
from app.intelligence.scoring import evaluate_snapshot
from app.models import auth_session, ews, governance, investigation, user  # noqa: F401
from app.models.ews import Customer, WarningAssessment
from app.models.governance import UserScope
from app.models.user import User
from app.services.ingestion import load_dataset
from app.services.policy import get_active_policy
from evaluation.scenarios import ACCEPTANCE, HELD_OUT, SCENARIOS, build_records

USERS = [
    ("analyst", "Asha Analyst", "credit_analyst", [("all", "")], None),
    ("manager", "Maya Manager", "credit_manager", [("all", "")], 5_000_000),
    ("manager2", "Omar Manager", "credit_manager", [("all", "")], 5_000_000),
    ("officer", "Rui Officer", "relationship_officer", [("customer", "ACCEPTANCE-001")], None),
    ("portfolio", "Pia Portfolio", "portfolio_manager", [("all", "")], None),
    ("admin", "Ari Admin", "risk_admin", [], None),
    ("auditor", "Ada Auditor", "auditor", [], None),
]
CATEGORY = {
    "low": "Low",
    "moderate": "Medium",
    "high": "High",
    "severe": "Critical",
    "undetermined": "Insufficient Data",
}


def main() -> None:
    settings = Settings()
    _, factory = configure_database(settings.database_url)
    password = os.environ.get("CREDX_DEMO_PASSWORD") or secrets.token_urlsafe(12)
    with factory() as db:
        created = []
        for handle, name, role, scopes, limit in USERS:
            email = f"demo-{handle}@credx.local"
            if db.scalar(select(User).where(User.email == email)):
                continue
            account = User(
                email=email,
                full_name=name,
                role=role,
                approval_limit=limit,
                password_hash=password_hasher.hash(password),
            )
            db.add(account)
            db.flush()
            for kind, value in scopes:
                db.add(UserScope(user_id=account.id, scope_type=kind, scope_value=value))
            created.append((email, role))
        refs = {"ACCEPTANCE-001": ACCEPTANCE}
        refs |= {s.key.upper().replace("_", "-"): s for s in [*SCENARIOS, *HELD_OUT]}
        for ref, scenario in refs.items():
            if db.scalar(select(Customer).where(Customer.customer_id == ref)) is None:
                load_dataset(db, build_records(scenario, ref))
        db.commit()
        _, policy = get_active_policy(db, settings.institution_id)
        for customer in db.scalars(select(Customer)):
            a = evaluate_snapshot(load_customer_snapshot(db, customer.id), policy).assessment
            db.add(
                WarningAssessment(
                    customer_id=customer.id,
                    warning_score=a.risk_score or 0.0,
                    risk_category=CATEGORY[a.severity],
                    rule_exposure_score=a.family_scores.get("exposure", 0.0),
                    cashflow_score=a.family_scores.get("cashflow", 0.0),
                    rule_delay_score=a.family_scores.get("repayment", 0.0),
                    top_indicators=f'["Classification: {a.classification.replace("_", " ")}"]',
                )
            )
        db.commit()
    print(f"Loaded {len(refs)} demo customers.")
    if created:
        print("Demo users (local development only):")
        for email, role in created:
            print(f"  {email:32s} {role}")
        if "CREDX_DEMO_PASSWORD" not in os.environ:
            print(f"  password for all demo users: {password}")


if __name__ == "__main__":
    main()
