"""Investigation workspace and human-in-the-loop actions.

Routes authenticate the caller and delegate to ``services.investigations``, which
re-checks permission, data scope and investigation state before acting.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from sqlalchemy import select

from app.agents.graph import start_run
from app.api.dependencies import Config, CurrentUser, Database
from app.core.rbac import Permission
from app.models.investigation import Investigation, InvestigationEvent
from app.schemas import investigation as schema
from app.services import access, monitoring, read_models
from app.services import investigations as service

router = APIRouter(prefix="/investigations", tags=["investigations"])
P = Permission


def _finish(request: Request, db, investigation_id: str, run: bool, trigger: str) -> dict:
    db.commit()
    if run:
        start_run(request.app.state, investigation_id, trigger)
    db.expire_all()
    inv = db.get(Investigation, investigation_id)
    return read_models.workspace(db, inv)


@router.post("", status_code=201)
def initiate(
    body: schema.InitiateInvestigation,
    request: Request,
    db: Database,
    user: CurrentUser,
    settings: Config,
):
    inv = service.initiate(
        db,
        user,
        settings,
        customer_ref=body.customer_id if body.scope == "customer" else None,
        portfolio_segment=body.portfolio_segment if body.scope == "portfolio" else None,
        question=body.question,
        context=body.context,
        context_category=body.context_category,
        requested_agents=body.requested_specialists,
    )
    return _finish(request, db, inv.id, True, "initiated")


@router.get("")
def list_investigations(
    db: Database,
    user: CurrentUser,
    status: str | None = Query(default=None, max_length=32),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    access.require_permission(db, user, P.VIEW_INVESTIGATION, "investigation")
    query = select(Investigation).where(access.investigation_filter(db, user))
    if status:
        query = query.where(Investigation.status == status)
    rows = db.scalars(
        query.order_by(Investigation.updated_at.desc()).offset(offset).limit(limit)
    ).all()
    return {"items": [read_models.summary(db, inv) for inv in rows]}


@router.get("/{investigation_id}")
def get_workspace(investigation_id: str, db: Database, user: CurrentUser):
    inv = access.get_investigation(db, user, investigation_id, P.VIEW_INVESTIGATION)
    return read_models.workspace(db, inv)


@router.get("/{investigation_id}/events")
def events(
    investigation_id: str, db: Database, user: CurrentUser, after_seq: int = Query(default=0, ge=0)
):
    inv = access.get_investigation(db, user, investigation_id, P.VIEW_INVESTIGATION)
    rows = db.scalars(
        select(InvestigationEvent)
        .where(InvestigationEvent.investigation_id == inv.id, InvestigationEvent.seq > after_seq)
        .order_by(InvestigationEvent.seq)
        .limit(500)
    ).all()
    return {
        "status": inv.status,
        "run_state": inv.run_state,
        "events": [
            {
                "seq": e.seq,
                "type": e.event_type,
                "actor_type": e.actor_type,
                "actor_id": e.actor_id,
                "round": e.round,
                "task_id": e.task_id,
                "payload": read_models.J(e.payload),
                "created_at": e.created_at,
            }
            for e in rows
        ],
    }


@router.post("/{investigation_id}/notes")
def add_note(
    investigation_id: str, body: schema.NoteIn, request: Request, db: Database, user: CurrentUser
):
    service.add_note(
        db, user, investigation_id, body.kind, body.content, body.category, body.document_name
    )
    return _finish(request, db, investigation_id, False, "")


@router.post("/{investigation_id}/tasks")
def request_task(
    investigation_id: str,
    body: schema.TaskRequestIn,
    request: Request,
    db: Database,
    user: CurrentUser,
):
    _, run = service.request_task(
        db, user, investigation_id, body.specialist, body.question, body.hypothesis_code
    )
    return _finish(request, db, investigation_id, run, "human_request")


@router.post("/{investigation_id}/hypotheses/{hypothesis_id}/challenge")
def challenge(
    investigation_id: str,
    hypothesis_id: str,
    body: schema.ChallengeIn,
    request: Request,
    db: Database,
    user: CurrentUser,
):
    _, run = service.challenge_hypothesis(
        db, user, investigation_id, hypothesis_id, body.statement, body.request_specialist
    )
    return _finish(request, db, investigation_id, run, "human_challenge")


@router.post("/{investigation_id}/findings/{finding_id}/review")
def review_finding(
    investigation_id: str,
    finding_id: str,
    body: schema.FindingReviewIn,
    request: Request,
    db: Database,
    user: CurrentUser,
):
    service.review_finding(db, user, investigation_id, finding_id, body.action, body.reason)
    return _finish(request, db, investigation_id, False, "")


@router.post("/{investigation_id}/challenges/{challenge_id}/resolve")
def resolve_challenge(
    investigation_id: str,
    challenge_id: str,
    body: schema.ChallengeResolutionIn,
    request: Request,
    db: Database,
    user: CurrentUser,
):
    service.resolve_challenge(db, user, investigation_id, challenge_id, body.action, body.reason)
    return _finish(request, db, investigation_id, False, "")


@router.post("/{investigation_id}/records/{record_id}/review")
def review_record(
    investigation_id: str,
    record_id: str,
    body: schema.RecordReviewIn,
    request: Request,
    db: Database,
    user: CurrentUser,
):
    service.review_record(db, user, investigation_id, record_id, body.action, body.reason)
    return _finish(request, db, investigation_id, False, "")


@router.post("/{investigation_id}/validate-evidence")
def validate_evidence(
    investigation_id: str,
    body: schema.EvidenceValidationIn,
    request: Request,
    db: Database,
    user: CurrentUser,
):
    service.validate_evidence(
        db, user, investigation_id, body.outcome, body.reason, body.flagged_finding_ids
    )
    return _finish(request, db, investigation_id, False, "")


@router.post("/{investigation_id}/evidence-requests")
def request_evidence(
    investigation_id: str,
    body: schema.EvidenceRequestIn,
    request: Request,
    db: Database,
    user: CurrentUser,
):
    service.request_more_evidence(db, user, investigation_id, body.description)
    return _finish(request, db, investigation_id, False, "")


@router.post("/{investigation_id}/assign")
def assign(
    investigation_id: str, body: schema.AssignIn, request: Request, db: Database, user: CurrentUser
):
    service.assign(db, user, investigation_id, body.analyst_user_id, body.reason)
    return _finish(request, db, investigation_id, False, "")


@router.post("/{investigation_id}/escalate")
def escalate(
    investigation_id: str, body: schema.ReasonIn, request: Request, db: Database, user: CurrentUser
):
    service.escalate(db, user, investigation_id, body.reason)
    return _finish(request, db, investigation_id, False, "")


@router.post("/{investigation_id}/complete-review")
def complete_review(
    investigation_id: str,
    body: schema.CompleteReviewIn,
    request: Request,
    db: Database,
    user: CurrentUser,
):
    _, run = service.complete_review(
        db, user, investigation_id, body.reason, body.recommended_classification
    )
    return _finish(request, db, investigation_id, run, "analyst_review_completed")


@router.post("/{investigation_id}/classification-decision")
def classification_decision(
    investigation_id: str,
    body: schema.ClassificationDecisionIn,
    request: Request,
    db: Database,
    user: CurrentUser,
):
    service.decide_classification(
        db, user, investigation_id, body.action, body.reason, body.classification, body.severity
    )
    return _finish(request, db, investigation_id, False, "")


@router.post("/{investigation_id}/interventions/{intervention_id}/decision")
def intervention_decision(
    investigation_id: str,
    intervention_id: str,
    body: schema.InterventionDecisionIn,
    request: Request,
    db: Database,
    user: CurrentUser,
):
    service.decide_intervention(
        db, user, investigation_id, intervention_id, body.action, body.reason
    )
    return _finish(request, db, investigation_id, False, "")


@router.post("/{investigation_id}/portfolio-decision")
def portfolio_decision(
    investigation_id: str,
    body: schema.PortfolioDecisionIn,
    request: Request,
    db: Database,
    user: CurrentUser,
):
    service.portfolio_decision(
        db, user, investigation_id, body.action, body.reason, body.classification
    )
    return _finish(request, db, investigation_id, False, "")


@router.post("/{investigation_id}/reopen")
def reopen(
    investigation_id: str, body: schema.ReasonIn, request: Request, db: Database, user: CurrentUser
):
    service.reopen(db, user, investigation_id, body.reason)
    return _finish(request, db, investigation_id, True, "human_reopen")


@router.put("/{investigation_id}/monitoring")
def set_monitoring(
    investigation_id: str,
    body: schema.MonitoringIn,
    request: Request,
    db: Database,
    user: CurrentUser,
):
    instructions = body.model_dump(exclude={"reason"}, exclude_none=True)
    service.set_monitoring(db, user, investigation_id, instructions, body.reason)
    return _finish(request, db, investigation_id, False, "")


@router.post("/{investigation_id}/monitoring/evaluate")
def evaluate_monitoring(investigation_id: str, request: Request, db: Database, user: CurrentUser):
    inv = access.get_investigation(db, user, investigation_id, P.SET_MONITORING)
    decision = monitoring.evaluate_investigation(db, inv, "manual")
    return _finish(request, db, inv.id, decision == "reopen", "monitoring_reopen")


@router.post("/{investigation_id}/cancel")
def cancel(
    investigation_id: str, body: schema.ReasonIn, request: Request, db: Database, user: CurrentUser
):
    service.cancel(db, user, investigation_id, body.reason)
    return _finish(request, db, investigation_id, False, "")
