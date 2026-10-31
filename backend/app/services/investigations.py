"""Human actions on investigations.

Every function here takes an authenticated ``User`` and re-checks permission, data
scope and the investigation's state itself, so the API layer is not the only guard.
Each consequential action is recorded as an append-only ``HumanDecision`` carrying the
user, role, previous and new state, reason, evidence references and policy version.
Agents have no access to this module: they never hold a ``User``.
"""

from __future__ import annotations

import json
import uuid

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.agents import store
from app.agents.contracts import CONTEXT_CATEGORIES, HYPOTHESES, SPECIALISTS, AgentId
from app.core.rbac import Permission, Role, rbac_enabled, role_of
from app.intelligence.data import load_customer_snapshot
from app.models.ews import Customer
from app.models.investigation import (
    Adjudication,
    AgentFinding,
    AgentTask,
    EvidenceChallenge,
    HumanDecision,
    HumanNote,
    Hypothesis,
    Investigation,
    ReviewItem,
)
from app.models.user import User
from app.services import access, audit
from app.services.policy import get_active_policy, get_policy_version

P = Permission
ACTIVE = {"investigating", "awaiting_analyst_review", "awaiting_approval", "incomplete"}
REVIEWABLE = ACTIVE | {"monitoring", "approved", "rejected"}
REASON_MIN = 5


def _human(user) -> User:
    if not isinstance(user, User) or not user.is_active:
        raise HTTPException(403, "A human decision requires an authenticated, active user")
    return user


def _reason(reason: str | None, required: bool = True) -> str:
    text = (reason or "").strip()
    if required and len(text) < REASON_MIN:
        raise HTTPException(422, "A reason of at least 5 characters is required")
    return text[:2000]


def _status(inv: Investigation, allowed: set[str], action: str) -> None:
    if inv.status not in allowed:
        raise HTTPException(409, f"Cannot {action} while investigation is {inv.status}")


def _open_review(db: Session, inv: Investigation, queue: str | None = None) -> ReviewItem | None:
    query = select(ReviewItem).where(
        ReviewItem.investigation_id == inv.id, ReviewItem.status == "open"
    )
    if queue:
        query = query.where(ReviewItem.queue == queue)
    return db.scalar(query.order_by(ReviewItem.created_at.desc()))


def latest_adjudication(db: Session, inv: Investigation) -> Adjudication | None:
    return db.scalar(
        select(Adjudication)
        .where(Adjudication.investigation_id == inv.id)
        .order_by(text("adjudications.rowid DESC"))
        .limit(1)
    )


def decide(
    db: Session,
    user: User,
    inv: Investigation,
    action: str,
    *,
    target_type: str = "investigation",
    target_id: str = "",
    previous: dict | None = None,
    new: dict | None = None,
    reason: str = "",
    evidence_refs: list | None = None,
    review_item: ReviewItem | None = None,
) -> HumanDecision:
    user = _human(user)
    decision = HumanDecision(
        investigation_id=inv.id,
        customer_id=inv.customer_id,
        review_item_id=review_item.id if review_item else None,
        user_id=user.id,
        role=user.role,
        action=action,
        target_type=target_type,
        target_id=target_id or inv.id,
        previous_state=json.dumps(previous or {}, default=str),
        new_state=json.dumps(new or {}, default=str),
        reason=reason,
        evidence_refs=json.dumps(evidence_refs or []),
        policy_version_id=inv.policy_version_id,
        created_at=store.now(),
    )
    db.add(decision)
    db.flush()
    store.emit(
        db,
        inv,
        "human_decision",
        "human",
        user.id,
        {
            "decision_id": decision.id,
            "action": action,
            "role": user.role,
            "target_type": target_type,
            "target_id": decision.target_id,
            "reason": reason,
        },
    )
    audit.record(
        db, user, f"decision:{action}", "allowed", "investigation", inv.id, decision_id=decision.id
    )
    return decision


# ── Initiation and context ──────────────────────────────────────────────


def initiate(
    db: Session,
    user: User,
    settings,
    *,
    customer_ref: str | None,
    portfolio_segment: str | None,
    question: str,
    context: str | None,
    context_category: str | None,
    requested_agents: list[str],
) -> Investigation:
    policy_row, _ = get_active_policy(db, settings.institution_id)
    if customer_ref:
        customer = access.get_customer(db, user, customer_ref, P.INITIATE_INVESTIGATION)
        existing = db.scalar(
            select(Investigation).where(
                Investigation.customer_id == customer.id, Investigation.status.in_(ACTIVE)
            )
        )
        if existing is not None:
            raise HTTPException(409, f"Investigation {existing.id} is already active")
        inv = Investigation(scope_type="customer", customer_id=customer.id)
    else:
        access.require_permission(db, user, P.INITIATE_PORTFOLIO_INVESTIGATION, "portfolio")
        if not access.segment_permitted(access.user_scope(db, user), portfolio_segment):
            audit.record_denied(
                db,
                user,
                "initiate_portfolio",
                "portfolio",
                portfolio_segment or "all",
                reason="out_of_scope",
            )
            raise HTTPException(404, "Portfolio segment not found")
        inv = Investigation(scope_type="portfolio", portfolio_segment=portfolio_segment)
    inv.initiated_by = user.id
    inv.trigger = "analyst_request"
    inv.question = _reason(question, required=False)
    inv.policy_version_id = policy_row.id
    db.add(inv)
    db.flush()
    store.emit(
        db,
        inv,
        "investigation_created",
        "human",
        user.id,
        {"scope": inv.scope_type, "question": inv.question},
    )
    decide(db, user, inv, "initiate", new={"scope": inv.scope_type, "question": inv.question})
    if context:
        add_note(db, user, inv.id, "context", context, context_category or "other", None)
    for agent in requested_agents:
        request_task(
            db,
            user,
            inv.id,
            agent,
            question or "Analyst-requested investigation",
            None,
            resume=False,
        )
    return inv


def add_note(db, user, investigation_id, kind, content, category, document_name) -> HumanNote:
    inv = access.get_investigation(db, user, investigation_id, P.ADD_CONTEXT)
    _status(inv, REVIEWABLE | {"created"}, "add context")
    if kind not in {"context", "observation", "document", "correction"}:
        raise HTTPException(422, "Unknown note kind")
    if category is not None and category not in CONTEXT_CATEGORIES:
        raise HTTPException(422, "Unknown context category")
    note = HumanNote(
        investigation_id=inv.id,
        user_id=user.id,
        role=user.role,
        kind=kind,
        category=category if kind == "context" else None,
        content=content[:4000],
        document_name=(document_name or None) and document_name[:255],
        created_at=store.now(),
    )
    db.add(note)
    db.flush()
    decide(
        db,
        user,
        inv,
        "add_evidence" if kind == "document" else "add_note",
        target_type="note",
        target_id=note.id,
        new={"kind": kind, "category": note.category, "document": note.document_name},
    )
    return note


def _hypothesis_for(db: Session, inv: Investigation, code: str, user: User) -> Hypothesis:
    h = store.hypothesis_by_code(db, inv.id, code)
    if h is None:
        h = Hypothesis(
            investigation_id=inv.id,
            code=code,
            statement=HYPOTHESES[code].statement,
            status="open",
            priority=80,
            source=f"human:{user.id}",
            rationale="Requested by a human reviewer",
            round_created=inv.current_round,
        )
        db.add(h)
        db.flush()
        store.emit(
            db,
            inv,
            "hypothesis_created",
            "human",
            user.id,
            {"hypothesis_id": h.id, "code": code, "status": "open"},
        )
    return h


def request_task(
    db, user, investigation_id, agent, question, hypothesis_code, resume: bool = True
) -> tuple[AgentTask, bool]:
    """Request a specialist investigation. Returns (task, whether a run should start)."""
    inv = access.get_investigation(db, user, investigation_id, P.REQUEST_INVESTIGATION)
    _status(inv, ACTIVE | {"created"}, "request an investigation")
    try:
        agent_id = AgentId(agent)
    except ValueError:
        raise HTTPException(422, "Unknown specialist") from None
    if agent_id not in SPECIALISTS:
        raise HTTPException(422, "Only specialists can be requested")
    code = hypothesis_code or _default_hypothesis(agent_id, inv)
    if code not in HYPOTHESES:
        raise HTTPException(422, "Unknown hypothesis")
    h = _hypothesis_for(db, inv, code, user)
    task = AgentTask(
        investigation_id=inv.id,
        hypothesis_id=h.id,
        agent_id=agent_id.value,
        question=_reason(question, required=False) or h.statement,
        requested_by=f"human:{user.id}",
        round=inv.current_round,
        status="requested",
        # Human requests are never deduplicated away: each one is an explicit instruction.
        dedup_key=store.dedup_key(agent_id.value, code, "human", uuid.uuid4().hex),
        priority=90,
    )
    db.add(task)
    db.flush()
    decide(
        db,
        user,
        inv,
        "request_investigation",
        target_type="task",
        target_id=task.id,
        new={"agent": agent_id.value, "hypothesis": code, "question": task.question},
    )
    should_run = resume and _resume(db, user, inv, "Human requested further investigation")
    return task, should_run


def _default_hypothesis(agent: AgentId, inv: Investigation) -> str:
    if inv.scope_type == "portfolio":
        return "portfolio_cluster"
    return {
        AgentId.REPAYMENT: "repayment_deterioration",
        AgentId.CASHFLOW: "cashflow_decline",
        AgentId.EXPOSURE: "obligation_pressure",
        AgentId.BASELINE: "temporary_disruption",
        AgentId.INTEGRITY: "data_quality_artefact",
        AgentId.PORTFOLIO: "segment_wide_stress",
    }[agent]


def _resume(db: Session, user: User, inv: Investigation, reason: str) -> bool:
    """Move a paused investigation back into the state machine. Returns True if it should run."""
    if inv.status == "investigating":
        return False  # the running graph picks requested tasks up at its next plan step
    if inv.status in {"awaiting_analyst_review", "awaiting_approval", "incomplete"}:
        item = _open_review(db, inv)
        if item is not None:
            item.status, item.completed_at, item.completed_by = "completed", store.now(), user.id
        store.transition(db, inv, "investigating", "human", user.id, reason)
        return True
    return inv.status == "created"


def challenge_hypothesis(db, user, investigation_id, hypothesis_id, statement, request_agent):
    inv = access.get_investigation(db, user, investigation_id, P.CHALLENGE_HYPOTHESIS)
    _status(inv, ACTIVE, "challenge a hypothesis")
    h = db.get(Hypothesis, hypothesis_id)
    if h is None or h.investigation_id != inv.id:
        raise HTTPException(404, "Hypothesis not found")
    challenge = EvidenceChallenge(
        investigation_id=inv.id,
        round=inv.current_round,
        target_type="hypothesis",
        target_id=h.id,
        challenge_type="human_challenge",
        statement=_reason(statement)[:600],
        materiality="material",
        status="open",
        raised_by=f"human:{user.id}",
    )
    db.add(challenge)
    db.flush()
    decide(
        db,
        user,
        inv,
        "challenge_hypothesis",
        target_type="hypothesis",
        target_id=h.id,
        previous={"status": h.status},
        new={"challenge_id": challenge.id},
        reason=challenge.statement,
    )
    should_run = False
    if request_agent:
        task, should_run = request_task(
            db,
            user,
            inv.id,
            request_agent,
            f"Address human challenge: {challenge.statement}",
            h.code,
        )
        challenge.followup_task_ids = json.dumps([task.id])
    return challenge, should_run


def review_finding(db, user, investigation_id, finding_id, action, reason):
    inv = access.get_investigation(db, user, investigation_id, P.REVIEW_FINDING)
    _status(inv, REVIEWABLE, "review a finding")
    if action not in {"accept_finding", "reject_finding", "flag_unsupported"}:
        raise HTTPException(422, "Unknown finding review action")
    finding = db.get(AgentFinding, finding_id)
    if finding is None or finding.investigation_id != inv.id:
        raise HTTPException(404, "Finding not found")
    reason = _reason(reason, required=action != "accept_finding")
    # The finding row itself is immutable; the review is a separate decision.
    return decide(
        db,
        user,
        inv,
        action,
        target_type="finding",
        target_id=finding.id,
        previous={"code": finding.code, "direction": finding.direction, "agent": finding.agent_id},
        new={"review": action},
        reason=reason,
        review_item=_open_review(db, inv),
    )


def resolve_challenge(db, user, investigation_id, challenge_id, action, reason):
    inv = access.get_investigation(db, user, investigation_id, P.RESOLVE_CHALLENGE)
    _status(inv, ACTIVE, "resolve a challenge")
    challenge = db.get(EvidenceChallenge, challenge_id)
    if challenge is None or challenge.investigation_id != inv.id:
        raise HTTPException(404, "Challenge not found")
    status = {"dismiss": "dismissed", "uphold": "upheld"}.get(action)
    if status is None:
        raise HTTPException(422, "Action must be dismiss or uphold")
    reason = _reason(reason)
    previous = {"status": challenge.status}
    challenge.status, challenge.resolution = status, f"{status} by {user.role}: {reason}"
    challenge.resolved_at = store.now()
    return decide(
        db,
        user,
        inv,
        f"{action}_challenge",
        target_type="challenge",
        target_id=challenge.id,
        previous=previous,
        new={"status": status},
        reason=reason,
        review_item=_open_review(db, inv),
    )


def review_record(db, user, investigation_id, record_id, action, reason):
    """Dispute (exclude) or confirm (trust) a source record for this investigation."""
    inv = access.get_investigation(db, user, investigation_id, P.VALIDATE_EVIDENCE)
    _status(inv, ACTIVE, "review evidence")
    if action not in {"dispute_evidence", "confirm_evidence"}:
        raise HTTPException(422, "Unknown evidence action")
    if inv.scope_type != "customer":
        raise HTTPException(422, "Records can only be reviewed on customer investigations")
    snapshot = load_customer_snapshot(db, inv.customer_id)
    table = snapshot.source_table(record_id)
    if table is None:
        raise HTTPException(404, "Record not found for this customer")
    return decide(
        db,
        user,
        inv,
        action,
        target_type="record",
        target_id=record_id,
        new={
            "source_table": table,
            "effect": "excluded" if action == "dispute_evidence" else "trusted",
        },
        reason=_reason(reason),
        evidence_refs=[{"table": table, "id": record_id}],
        review_item=_open_review(db, inv),
    )


def validate_evidence(db, user, investigation_id, outcome, reason, flagged_finding_ids):
    inv = access.get_investigation(db, user, investigation_id, P.VALIDATE_EVIDENCE)
    _status(inv, ACTIVE, "validate evidence")
    if outcome not in {"complete", "incomplete"}:
        raise HTTPException(422, "Outcome must be complete or incomplete")
    for fid in flagged_finding_ids:
        review_finding(db, user, inv.id, fid, "flag_unsupported", reason)
    return decide(
        db,
        user,
        inv,
        "validate_evidence",
        new={"outcome": outcome, "flagged": flagged_finding_ids},
        reason=_reason(reason, outcome != "complete"),
        review_item=_open_review(db, inv),
    )


def request_more_evidence(db, user, investigation_id, description):
    inv = access.get_investigation(db, user, investigation_id, P.VALIDATE_EVIDENCE)
    _status(inv, REVIEWABLE, "request evidence")
    return decide(
        db,
        user,
        inv,
        "request_more_evidence",
        new={"request": description[:1000]},
        reason=_reason(description),
        review_item=_open_review(db, inv),
    )


def assign(db, user, investigation_id, analyst_id, reason):
    inv = access.get_investigation(db, user, investigation_id, P.ASSIGN_ANALYST)
    _status(inv, ACTIVE, "assign an analyst")
    analyst = db.get(User, analyst_id)
    if analyst is None or not analyst.is_active or (
        rbac_enabled() and role_of(analyst) != Role.CREDIT_ANALYST
    ):
        raise HTTPException(422, "Assignee must be an active credit analyst")
    if not access.can_access_investigation(db, analyst, inv):
        raise HTTPException(422, "Assignee has no data scope for this investigation")
    previous = {"assigned_analyst_id": inv.assigned_analyst_id}
    inv.assigned_analyst_id = analyst.id
    item = _open_review(db, inv)
    if item is not None and item.required_role == Role.CREDIT_ANALYST.value:
        item.assigned_to = analyst.id
    return decide(
        db,
        user,
        inv,
        "assign_analyst",
        target_type="user",
        target_id=analyst.id,
        previous=previous,
        new={"assigned_analyst_id": analyst.id},
        reason=_reason(reason, required=False),
        review_item=item,
    )


def escalate(db, user, investigation_id, reason):
    inv = access.get_investigation(db, user, investigation_id, P.ESCALATE)
    _status(inv, {"awaiting_analyst_review", "incomplete"}, "escalate")
    if inv.scope_type != "customer":
        raise HTTPException(422, "Use the portfolio decision endpoint for portfolio cases")
    adj = latest_adjudication(db, inv)
    if adj is None:
        raise HTTPException(409, "Escalation needs an adjudication; complete the review first")
    item = _open_review(db, inv)
    if item is not None:
        item.status, item.completed_at, item.completed_by = "completed", store.now(), user.id
    reason = _reason(reason)
    new_item = ReviewItem(
        investigation_id=inv.id,
        adjudication_id=adj.id,
        queue="authorised_approval",
        required_role="credit_manager",
        stage="approval",
        reasons=json.dumps([f"Escalated: {reason}"]),
    )
    db.add(new_item)
    previous = {"status": inv.status}
    store.transition(db, inv, "awaiting_approval", "human", user.id, "Escalated by analyst")
    return decide(
        db,
        user,
        inv,
        "escalate",
        previous=previous,
        new={"status": "awaiting_approval"},
        reason=reason,
        review_item=item,
    )


def complete_review(db, user, investigation_id, reason, recommended_classification):
    """Analyst completes their review stage. Returns (decision, whether a run should start)."""
    inv = access.get_investigation(db, user, investigation_id, P.COMPLETE_ANALYST_REVIEW)
    _status(inv, {"awaiting_analyst_review", "incomplete"}, "complete the review")
    item = _open_review(db, inv)
    if item is not None and item.queue == "portfolio_review" and rbac_enabled():
        raise HTTPException(403, "Portfolio reviews are decided by a portfolio risk manager")
    stage = item.stage if item else "post_adjudication"
    reason = _reason(reason)
    previous = {"status": inv.status, "stage": stage}
    if stage in {"pre_adjudication", "incomplete"}:
        decision = decide(
            db,
            user,
            inv,
            "complete_analyst_review",
            previous=previous,
            new={"next": "adjudicate"},
            reason=reason,
            review_item=item,
        )
        return decision, _resume(db, user, inv, "Analyst review completed")
    adj = latest_adjudication(db, inv)
    consequential = (
        [i for i in json.loads(adj.proposed_interventions) if i.get("requires_approval")]
        if adj
        else []
    )
    if consequential:
        raise HTTPException(409, "Proposed interventions need authorised approval: escalate")
    if item is not None:
        item.status, item.completed_at, item.completed_by = "completed", store.now(), user.id
    store.transition(db, inv, "monitoring", "human", user.id, "Analyst review completed")
    return decide(
        db,
        user,
        inv,
        "complete_analyst_review",
        previous=previous,
        new={"status": "monitoring", "recommended_classification": recommended_classification},
        reason=reason,
        review_item=item,
    ), False


# ── Authorised decisions ────────────────────────────────────────────────

PREPARER_ACTIONS = (
    "initiate",
    "request_investigation",
    "complete_analyst_review",
    "escalate",
    "challenge_hypothesis",
)
CLASSIFICATIONS = {
    "low_risk",
    "emerging_deterioration",
    "sustained_deterioration",
    "accelerating_deterioration",
    "recovery",
    "insufficient_evidence",
}


def _approval_context(db: Session, user: User, inv: Investigation):
    _status(inv, {"awaiting_approval"}, "record an approval decision")
    item = _open_review(db, inv, "authorised_approval")
    adj = latest_adjudication(db, inv)
    if item is None or adj is None:
        raise HTTPException(409, "No open approval review for this investigation")
    if rbac_enabled() and (user.approval_limit is None or user.approval_limit < adj.exposure_at_risk):
        audit.record_denied(
            db,
            user,
            "approval",
            "investigation",
            inv.id,
            reason="exposure_exceeds_delegated_authority",
        )
        raise HTTPException(403, "Exposure exceeds your delegated approval authority")
    policy = get_policy_version(db, inv.policy_version_id)
    if policy.escalation.four_eyes and rbac_enabled():
        reviewers = set(
            db.scalars(
                select(HumanDecision.user_id).where(
                    HumanDecision.investigation_id == inv.id,
                    # Anyone who prepared the case cannot also approve it.
                    HumanDecision.action.in_(PREPARER_ACTIONS),
                )
            )
        )
        if user.id in reviewers:
            audit.record_denied(db, user, "approval", "investigation", inv.id, reason="four_eyes")
            raise HTTPException(403, "Four-eyes rule: a preparer of this case cannot approve it")
    return item, adj


def decide_classification(db, user, investigation_id, action, reason, classification, severity):
    inv = access.get_investigation(db, user, investigation_id, P.DECIDE_CLASSIFICATION)
    item, adj = _approval_context(db, user, inv)
    if action not in {"accept_classification", "override_classification"}:
        raise HTTPException(422, "Unknown classification action")
    reason = _reason(reason)
    previous = {
        "classification": adj.classification,
        "severity": adj.severity,
        "adjudication_id": adj.id,
    }
    if action == "override_classification":
        if (
            classification not in CLASSIFICATIONS
            or classification == adj.classification
            and (severity in (None, adj.severity))
        ):
            raise HTTPException(422, "Override needs a different, valid classification or severity")
        if severity is not None and severity not in {"low", "moderate", "high", "severe"}:
            raise HTTPException(422, "Unknown severity")
        new = {"classification": classification, "severity": severity or adj.severity}
    else:
        new = dict(previous)
    decision = decide(
        db,
        user,
        inv,
        action,
        target_type="adjudication",
        target_id=adj.id,
        previous=previous,
        new=new,
        reason=reason,
        review_item=item,
    )
    _finalise_approval(db, user, inv, item, adj)
    return decision


def decide_intervention(db, user, investigation_id, intervention_id, action, reason):
    inv = access.get_investigation(db, user, investigation_id, P.DECIDE_INTERVENTION)
    item, adj = _approval_context(db, user, inv)
    if action not in {"approve_intervention", "reject_intervention"}:
        raise HTTPException(422, "Unknown intervention action")
    proposals = {i["id"]: i for i in json.loads(adj.proposed_interventions)}
    proposal = proposals.get(intervention_id)
    if proposal is None:
        raise HTTPException(404, "Intervention not found on the current adjudication")
    decision = decide(
        db,
        user,
        inv,
        action,
        target_type="intervention",
        target_id=intervention_id,
        previous={"code": proposal["code"], "adjudication_id": adj.id},
        new={
            "code": proposal["code"],
            # CredX records the authorisation only; execution belongs to a separately
            # authorised downstream workflow and is never triggered from here.
            "status": "approved_pending_downstream"
            if action == "approve_intervention"
            else "rejected",
            "executed": False,
        },
        reason=_reason(reason),
        review_item=item,
    )
    _finalise_approval(db, user, inv, item, adj)
    return decision


def _finalise_approval(db, user, inv, item, adj) -> None:
    decisions = list(
        db.scalars(select(HumanDecision).where(HumanDecision.review_item_id == item.id))
    )
    classified = any(
        d.action in {"accept_classification", "override_classification"} for d in decisions
    )
    required = [i for i in json.loads(adj.proposed_interventions) if i.get("requires_approval")]
    decided = {d.target_id: d.action for d in decisions if d.target_type == "intervention"}
    if not classified or any(i["id"] not in decided for i in required):
        return
    approved = any(a == "approve_intervention" for a in decided.values())
    status = "approved" if approved or not required else "rejected"
    item.status, item.completed_at, item.completed_by = "completed", store.now(), user.id
    store.transition(db, inv, status, "human", user.id, "Authorised decision recorded")


def portfolio_decision(db, user, investigation_id, action, reason, classification):
    inv = access.get_investigation(db, user, investigation_id, P.DECIDE_PORTFOLIO)
    if inv.scope_type != "portfolio":
        raise HTTPException(422, "Not a portfolio investigation")
    _status(inv, {"awaiting_analyst_review"}, "record a portfolio decision")
    item = _open_review(db, inv, "portfolio_review")
    adj = latest_adjudication(db, inv)
    if item is None or adj is None:
        raise HTTPException(409, "No open portfolio review")
    if action not in {"accept_portfolio_assessment", "override_portfolio_assessment"}:
        raise HTTPException(422, "Unknown portfolio action")
    previous = {"classification": adj.classification, "severity": adj.severity}
    new = previous if action.startswith("accept") else {"classification": classification}
    decision = decide(
        db,
        user,
        inv,
        action,
        target_type="adjudication",
        target_id=adj.id,
        previous=previous,
        new=new,
        reason=_reason(reason),
        review_item=item,
    )
    item.status, item.completed_at, item.completed_by = "completed", store.now(), user.id
    store.transition(db, inv, "monitoring", "human", user.id, "Portfolio decision recorded")
    return decision


# ── Lifecycle ───────────────────────────────────────────────────────────


def reopen(db, user, investigation_id, reason) -> Investigation:
    inv = access.get_investigation(db, user, investigation_id, P.REOPEN_INVESTIGATION)
    _status(inv, store.MONITORED_STATUSES, "reopen")
    reason = _reason(reason)
    previous = {"status": inv.status}
    store.transition(db, inv, "reopened", "human", user.id, reason)
    inv.reopen_count += 1
    inv.trigger = "human_reopen"
    decide(db, user, inv, "reopen", previous=previous, new={"status": "reopened"}, reason=reason)
    return inv


def set_monitoring(db, user, investigation_id, instructions: dict, reason):
    inv = access.get_investigation(db, user, investigation_id, P.SET_MONITORING)
    _status(inv, REVIEWABLE, "change monitoring instructions")
    previous = json.loads(inv.monitoring or "{}")
    inv.monitoring = json.dumps(instructions)
    return decide(
        db,
        user,
        inv,
        "set_monitoring",
        previous=previous,
        new=instructions,
        reason=_reason(reason, required=False),
    )


def cancel(db, user, investigation_id, reason):
    inv = access.get_investigation(db, user, investigation_id, P.CANCEL_INVESTIGATION)
    _status(inv, ACTIVE | {"created", "reopened"}, "cancel")
    reason = _reason(reason)
    previous = {"status": inv.status}
    if inv.run_state == "running":
        inv.cancel_requested = True
    else:
        for item in db.scalars(
            select(ReviewItem).where(
                ReviewItem.investigation_id == inv.id, ReviewItem.status == "open"
            )
        ):
            item.status = "cancelled"
        store.transition(db, inv, "cancelled", "human", user.id, reason)
    return decide(
        db, user, inv, "cancel", previous=previous, new={"cancel_requested": True}, reason=reason
    )


def customer_ref(db: Session, inv: Investigation) -> str | None:
    if inv.customer_id is None:
        return None
    customer = db.get(Customer, inv.customer_id)
    return customer.customer_id if customer else None
