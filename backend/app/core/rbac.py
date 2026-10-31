"""Role-based permissions for human users.

The matrix is enforced in the service layer (``require``) and every API route also
declares the permission it needs, so a missing route-level check still fails closed.
Agents never hold a ``User`` principal and therefore can never satisfy these checks.
"""

import os
from enum import StrEnum

from fastapi import HTTPException


class Role(StrEnum):
    UNASSIGNED = "unassigned"
    RELATIONSHIP_OFFICER = "relationship_officer"
    CREDIT_ANALYST = "credit_analyst"
    CREDIT_MANAGER = "credit_manager"
    PORTFOLIO_MANAGER = "portfolio_manager"
    RISK_ADMIN = "risk_admin"
    AUDITOR = "auditor"


class Permission(StrEnum):
    VIEW_CUSTOMER = "view_customer"
    UPLOAD_DATA = "upload_data"
    RUN_ASSESSMENT = "run_assessment"
    VIEW_INVESTIGATION = "view_investigation"
    INITIATE_INVESTIGATION = "initiate_investigation"
    ADD_CONTEXT = "add_context"
    REQUEST_INVESTIGATION = "request_investigation"
    CHALLENGE_HYPOTHESIS = "challenge_hypothesis"
    REVIEW_FINDING = "review_finding"
    RESOLVE_CHALLENGE = "resolve_challenge"
    VALIDATE_EVIDENCE = "validate_evidence"
    COMPLETE_ANALYST_REVIEW = "complete_analyst_review"
    ESCALATE = "escalate"
    ASSIGN_ANALYST = "assign_analyst"
    DECIDE_CLASSIFICATION = "decide_classification"
    DECIDE_INTERVENTION = "decide_intervention"
    REOPEN_INVESTIGATION = "reopen_investigation"
    CANCEL_INVESTIGATION = "cancel_investigation"
    SET_MONITORING = "set_monitoring"
    VIEW_PORTFOLIO = "view_portfolio"
    INITIATE_PORTFOLIO_INVESTIGATION = "initiate_portfolio_investigation"
    DECIDE_PORTFOLIO = "decide_portfolio"
    VIEW_REVIEW_QUEUE = "view_review_queue"
    MANAGE_POLICY = "manage_policy"
    MANAGE_USERS = "manage_users"
    VIEW_AUDIT = "view_audit"


P = Permission

_OFFICER = {
    P.VIEW_CUSTOMER,
    P.VIEW_INVESTIGATION,
    P.INITIATE_INVESTIGATION,
    P.ADD_CONTEXT,
    P.REQUEST_INVESTIGATION,
    P.VIEW_REVIEW_QUEUE,
}
_ANALYST = _OFFICER | {
    P.UPLOAD_DATA,
    P.RUN_ASSESSMENT,
    P.CHALLENGE_HYPOTHESIS,
    P.REVIEW_FINDING,
    P.RESOLVE_CHALLENGE,
    P.VALIDATE_EVIDENCE,
    P.COMPLETE_ANALYST_REVIEW,
    P.ESCALATE,
    P.REOPEN_INVESTIGATION,
    P.CANCEL_INVESTIGATION,
    P.SET_MONITORING,
    P.VIEW_PORTFOLIO,
}
_MANAGER = _ANALYST | {
    P.ASSIGN_ANALYST,
    P.DECIDE_CLASSIFICATION,
    P.DECIDE_INTERVENTION,
}
_PORTFOLIO = {
    P.VIEW_CUSTOMER,
    P.VIEW_INVESTIGATION,
    P.VIEW_PORTFOLIO,
    P.INITIATE_PORTFOLIO_INVESTIGATION,
    P.DECIDE_PORTFOLIO,
    P.ADD_CONTEXT,
    P.REQUEST_INVESTIGATION,
    P.VIEW_REVIEW_QUEUE,
    P.REOPEN_INVESTIGATION,
}
# Administrators govern configuration and read history; they make no credit decisions.
_ADMIN = {P.MANAGE_POLICY, P.MANAGE_USERS, P.VIEW_AUDIT, P.VIEW_INVESTIGATION, P.VIEW_PORTFOLIO}
_AUDITOR = {P.VIEW_AUDIT, P.VIEW_INVESTIGATION}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.UNASSIGNED: frozenset(),
    Role.RELATIONSHIP_OFFICER: frozenset(_OFFICER),
    Role.CREDIT_ANALYST: frozenset(_ANALYST),
    Role.CREDIT_MANAGER: frozenset(_MANAGER),
    Role.PORTFOLIO_MANAGER: frozenset(_PORTFOLIO),
    Role.RISK_ADMIN: frozenset(_ADMIN),
    Role.AUDITOR: frozenset(_AUDITOR),
}

# Roles whose customer visibility is all-in-scope rather than per-assignment.
GLOBAL_READ_ROLES = {Role.RISK_ADMIN, Role.AUDITOR}


def role_of(user) -> Role:
    try:
        return Role(getattr(user, "role", Role.UNASSIGNED))
    except ValueError:
        return Role.UNASSIGNED


def rbac_enabled() -> bool:
    """Role controls are off unless RBAC_ENABLED is set: every active, signed-in
    account may then use every feature and see every customer. Set RBAC_ENABLED=true
    to enforce roles, data scopes, approval limits and the four-eyes rule."""
    return os.environ.get("RBAC_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def has_permission(user, permission: Permission) -> bool:
    if user is None or not getattr(user, "is_active", False):
        return False
    if not rbac_enabled():
        return True
    return permission in ROLE_PERMISSIONS[role_of(user)]


def forbidden(detail: str = "Not permitted") -> HTTPException:
    return HTTPException(403, detail)


def require(user, permission: Permission) -> None:
    if not has_permission(user, permission):
        raise forbidden()
