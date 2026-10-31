"""Security audit logging."""

from __future__ import annotations

import json

from sqlalchemy.orm import Session

from app.models.governance import AuditLog


def record(
    db: Session,
    user,
    action: str,
    outcome: str,
    resource_type: str = "",
    resource_id: str = "",
    **detail,
) -> None:
    """Stage an audit entry in the caller's transaction (committed with the operation)."""
    db.add(
        AuditLog(
            user_id=getattr(user, "id", None),
            role=getattr(user, "role", "") or "",
            action=action,
            resource_type=resource_type,
            resource_id=str(resource_id)[:64],
            outcome=outcome,
            detail=json.dumps(detail, default=str)[:4000],
        )
    )


def record_denied(
    db: Session, user, action: str, resource_type: str = "", resource_id: str = "", **detail
):
    """Persist a denial now, discarding the caller's uncommitted work (which is about to fail)."""
    db.rollback()
    record(db, user, action, "denied", resource_type, resource_id, **detail)
    db.commit()
