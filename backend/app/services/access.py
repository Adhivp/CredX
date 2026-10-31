"""Customer, portfolio and investigation-level authorisation.

Every service query for customer data is filtered through ``customer_filter``. A user
with no scope sees nothing (fail closed). Out-of-scope objects are reported as 404 so
their existence is not disclosed, and every denial is written to the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import HTTPException
from sqlalchemy import false, or_, select, true
from sqlalchemy.orm import Session

from app.core.rbac import GLOBAL_READ_ROLES, Permission, has_permission, role_of
from app.models.ews import Customer
from app.models.governance import UserScope
from app.models.investigation import Investigation
from app.services import audit


@dataclass(frozen=True)
class Scope:
    all: bool
    segments: frozenset[str]
    customers: frozenset[str]

    @property
    def empty(self) -> bool:
        return not (self.all or self.segments or self.customers)


def user_scope(db: Session, user) -> Scope:
    if role_of(user) in GLOBAL_READ_ROLES:
        return Scope(True, frozenset(), frozenset())
    rows = db.execute(
        select(UserScope.scope_type, UserScope.scope_value).where(UserScope.user_id == user.id)
    ).all()
    return Scope(
        any(kind == "all" for kind, _ in rows),
        frozenset(value for kind, value in rows if kind == "segment"),
        frozenset(value for kind, value in rows if kind == "customer"),
    )


def customer_filter(scope: Scope):
    if scope.all:
        return true()
    clauses = []
    if scope.segments:
        clauses.append(Customer.portfolio_segment.in_(scope.segments))
    if scope.customers:
        clauses.append(Customer.customer_id.in_(scope.customers))
    return or_(*clauses) if clauses else false()


def segment_permitted(scope: Scope, segment: str | None) -> bool:
    return scope.all or (segment is not None and segment in scope.segments)


def not_found(kind: str) -> HTTPException:
    return HTTPException(404, f"{kind} not found")


def require_permission(db: Session, user, permission: Permission, resource_type="", resource_id=""):
    if not has_permission(user, permission):
        audit.record_denied(
            db, user, permission.value, resource_type, resource_id, reason="missing_permission"
        )
        raise HTTPException(403, "Not permitted")


def get_customer(db: Session, user, external_id: str, permission: Permission) -> Customer:
    require_permission(db, user, permission, "customer", external_id)
    customer = db.scalar(
        select(Customer).where(
            Customer.customer_id == external_id, customer_filter(user_scope(db, user))
        )
    )
    if customer is None:
        audit.record_denied(
            db, user, permission.value, "customer", external_id, reason="not_found_or_out_of_scope"
        )
        raise not_found("Customer")
    return customer


def can_access_investigation(db: Session, user, investigation: Investigation) -> bool:
    scope = user_scope(db, user)
    if scope.all:
        return True
    if investigation.scope_type == "portfolio":
        return segment_permitted(scope, investigation.portfolio_segment)
    customer = db.get(Customer, investigation.customer_id)
    if customer is None:
        return False
    return customer.portfolio_segment in scope.segments or customer.customer_id in scope.customers


def get_investigation(
    db: Session, user, investigation_id: str, permission: Permission
) -> Investigation:
    require_permission(db, user, permission, "investigation", investigation_id)
    investigation = db.get(Investigation, investigation_id)
    if investigation is None or not can_access_investigation(db, user, investigation):
        audit.record_denied(
            db,
            user,
            permission.value,
            "investigation",
            investigation_id,
            reason="not_found_or_out_of_scope",
        )
        raise not_found("Investigation")
    return investigation


def investigation_filter(db: Session, user):
    """SQL condition limiting Investigation rows to the user's scope."""
    scope = user_scope(db, user)
    if scope.all:
        return true()
    customer_ids = select(Customer.id).where(customer_filter(scope))
    clauses = [Investigation.customer_id.in_(customer_ids)]
    if scope.segments:
        clauses.append(
            (Investigation.scope_type == "portfolio")
            & Investigation.portfolio_segment.in_(scope.segments)
        )
    return or_(*clauses)
