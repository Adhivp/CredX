"""Risk administration: roles, data scopes and versioned institutional policy.

Administrators govern configuration only; they hold no credit-decision permissions and
there is no endpoint that edits or deletes historical decisions or evidence.
"""

import json

from fastapi import APIRouter, HTTPException
from pydantic import ValidationError
from sqlalchemy import delete, select

from app.api.dependencies import Config, CurrentUser, Database
from app.core.rbac import Permission
from app.models.governance import InstitutionPolicy, UserScope
from app.models.user import User
from app.schemas.investigation import PolicyIn, RoleAssignmentIn, ScopeIn
from app.services import access, audit
from app.services.policy import PolicyConfig, get_active_policy, publish_policy

router = APIRouter(prefix="/admin", tags=["administration"])


def _user_out(db, u: User) -> dict:
    scopes = db.execute(
        select(UserScope.scope_type, UserScope.scope_value).where(UserScope.user_id == u.id)
    ).all()
    return {
        "id": u.id,
        "email": u.email,
        "full_name": u.full_name,
        "role": u.role,
        "approval_limit": u.approval_limit,
        "is_active": u.is_active,
        "scopes": [{"scope_type": t, "scope_value": v} for t, v in scopes],
    }


@router.get("/users")
def list_users(db: Database, user: CurrentUser):
    access.require_permission(db, user, Permission.MANAGE_USERS, "user")
    return {"items": [_user_out(db, u) for u in db.scalars(select(User).order_by(User.email))]}


@router.put("/users/{user_id}/role")
def assign_role(user_id: str, body: RoleAssignmentIn, db: Database, user: CurrentUser):
    access.require_permission(db, user, Permission.MANAGE_USERS, "user", user_id)
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    if target.id == user.id:
        audit.record_denied(db, user, "assign_role", "user", user_id, reason="self_assignment")
        raise HTTPException(403, "Administrators cannot change their own role")
    previous = {"role": target.role, "approval_limit": target.approval_limit}
    target.role = body.role
    target.approval_limit = body.approval_limit if body.role == "credit_manager" else None
    audit.record(
        db,
        user,
        "assign_role",
        "allowed",
        "user",
        target.id,
        previous=previous,
        new={"role": target.role, "approval_limit": target.approval_limit},
        reason=body.reason,
    )
    db.commit()
    return _user_out(db, target)


@router.post("/users/{user_id}/scopes", status_code=201)
def grant_scope(user_id: str, body: ScopeIn, db: Database, user: CurrentUser):
    access.require_permission(db, user, Permission.MANAGE_USERS, "user", user_id)
    if db.get(User, user_id) is None:
        raise HTTPException(404, "User not found")
    if body.scope_type != "all" and not body.scope_value:
        raise HTTPException(422, "scope_value is required")
    exists = db.scalar(
        select(UserScope).where(
            UserScope.user_id == user_id,
            UserScope.scope_type == body.scope_type,
            UserScope.scope_value == body.scope_value,
        )
    )
    if exists is None:
        db.add(
            UserScope(
                user_id=user_id,
                scope_type=body.scope_type,
                scope_value=body.scope_value,
                granted_by=user.id,
            )
        )
    audit.record(
        db,
        user,
        "grant_scope",
        "allowed",
        "user",
        user_id,
        scope=body.model_dump(exclude={"reason"}),
        reason=body.reason,
    )
    db.commit()
    return _user_out(db, db.get(User, user_id))


@router.delete("/users/{user_id}/scopes")
def revoke_scope(user_id: str, body: ScopeIn, db: Database, user: CurrentUser):
    access.require_permission(db, user, Permission.MANAGE_USERS, "user", user_id)
    db.execute(
        delete(UserScope).where(
            UserScope.user_id == user_id,
            UserScope.scope_type == body.scope_type,
            UserScope.scope_value == body.scope_value,
        )
    )
    audit.record(
        db,
        user,
        "revoke_scope",
        "allowed",
        "user",
        user_id,
        scope=body.model_dump(exclude={"reason"}),
        reason=body.reason,
    )
    db.commit()
    target = db.get(User, user_id)
    if target is None:
        raise HTTPException(404, "User not found")
    return _user_out(db, target)


@router.get("/policies")
def list_policies(db: Database, user: CurrentUser, settings: Config):
    access.require_permission(db, user, Permission.VIEW_AUDIT, "policy")
    get_active_policy(db, settings.institution_id)
    rows = db.scalars(
        select(InstitutionPolicy)
        .where(InstitutionPolicy.institution_id == settings.institution_id)
        .order_by(InstitutionPolicy.version.desc())
    ).all()
    return {
        "items": [
            {
                "id": r.id,
                "version": r.version,
                "is_active": r.is_active,
                "config": json.loads(r.config),
                "created_by": r.created_by,
                "change_reason": r.change_reason,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    }


@router.post("/policies", status_code=201)
def create_policy(body: PolicyIn, db: Database, user: CurrentUser, settings: Config):
    access.require_permission(db, user, Permission.MANAGE_POLICY, "policy")
    try:
        config = PolicyConfig.model_validate(body.config)
    except ValidationError as exc:
        errors = [{"loc": e["loc"], "msg": e["msg"]} for e in exc.errors()]
        raise HTTPException(422, errors) from None
    row = publish_policy(db, settings.institution_id, config, user.id, body.reason)
    db.flush()
    audit.record(
        db,
        user,
        "publish_policy",
        "allowed",
        "policy",
        row.id,
        version=row.version,
        reason=body.reason,
    )
    db.commit()
    return {
        "id": row.id,
        "version": row.version,
        "is_active": row.is_active,
        "config": json.loads(row.config),
    }
