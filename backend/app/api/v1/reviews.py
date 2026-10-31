"""Human review queue, routed by role and limited to the user's data scope."""

import json

from fastapi import APIRouter
from sqlalchemy import select

from app.api.dependencies import CurrentUser, Database
from app.core.rbac import Permission, Role, role_of
from app.models.investigation import Investigation, ReviewItem
from app.services import access, read_models

router = APIRouter(prefix="/reviews", tags=["reviews"])

QUEUES_BY_ROLE = {
    Role.RELATIONSHIP_OFFICER: ("analyst_review",),
    Role.CREDIT_ANALYST: ("analyst_review",),
    Role.CREDIT_MANAGER: ("authorised_approval", "analyst_review"),
    Role.PORTFOLIO_MANAGER: ("portfolio_review",),
}


@router.get("/queue")
def queue(db: Database, user: CurrentUser):
    access.require_permission(db, user, Permission.VIEW_REVIEW_QUEUE, "review_queue")
    queues = QUEUES_BY_ROLE.get(role_of(user), ())
    rows = db.execute(
        select(ReviewItem, Investigation)
        .join(Investigation, Investigation.id == ReviewItem.investigation_id)
        .where(
            ReviewItem.status == "open",
            ReviewItem.queue.in_(queues),
            access.investigation_filter(db, user),
        )
        .order_by(ReviewItem.created_at)
    ).all()
    return {
        "role": user.role,
        "queues": list(queues),
        "items": [
            {
                "review_item_id": item.id,
                "queue": item.queue,
                "stage": item.stage,
                "required_role": item.required_role,
                "reasons": json.loads(item.reasons),
                "assigned_to_me": item.assigned_to == user.id,
                "assigned_to": item.assigned_to,
                "created_at": item.created_at,
                "investigation": read_models.summary(db, inv),
            }
            for item, inv in rows
        ],
    }
