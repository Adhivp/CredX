"""System status (LLM availability, ML readiness), audit trail and portfolio intelligence."""

import json
from pathlib import Path

from fastapi import APIRouter, Query
from sqlalchemy import func, select

from app.api.dependencies import Config, CurrentUser, Database
from app.core.rbac import Permission, has_permission
from app.intelligence.portfolio import portfolio_profile
from app.models.governance import AuditLog
from app.models.investigation import LLMCall
from app.services import access
from app.services.policy import get_active_policy

router = APIRouter(tags=["system"])

ML_AUDIT = {
    "status": "not_validated",
    "used_in_scoring": False,
    "summary": (
        "The repository's ML models are not used. Risk scores come from deterministic, "
        "versioned rules only."
    ),
    "findings": [
        "Warning-score model: the 'model_cashflow_score' feature is generated from the "
        "default label (TARGET) during training, which is direct target leakage.",
        "Cash-flow model: trained on synthetic series that were generated from the default "
        "label, so it learns the generator rather than observed behaviour.",
        "Both models use a random train/test split; there is no out-of-time validation, "
        "calibration or stability assessment.",
        "Training scripts reference absolute paths on another machine and the trained "
        "artifacts are not in the repository, so results are not reproducible.",
    ],
    "required_before_use": [
        "labelled monitoring data with outcome dates",
        "documented feature definitions computed only from data available at prediction time",
        "out-of-time validation, leakage checks, calibration and stability reports",
    ],
}


@router.get("/system/status")
def status(db: Database, user: CurrentUser, settings: Config, probe: bool = Query(default=False)):
    policy_row, policy = get_active_policy(db, settings.institution_id)
    llm_state = (
        "disabled"
        if not settings.llm_enabled
        else "configured"
        if settings.nebius_api_key
        else "not_configured"
    )
    last_ok = db.scalar(select(func.max(LLMCall.created_at)).where(LLMCall.status == "ok"))
    recent = dict(db.execute(select(LLMCall.status, func.count()).group_by(LLMCall.status)).all())
    probe_result = None
    if probe and has_permission(user, Permission.MANAGE_POLICY) and llm_state == "configured":
        try:
            from openai import OpenAI

            client = OpenAI(
                base_url=settings.nebius_base_url,
                api_key=settings.nebius_api_key,
                timeout=10,
                max_retries=0,
            )
            ids = {m.id for m in client.models.list().data}
            probe_result = {"reachable": True, "model_available": settings.nebius_model in ids}
        except Exception as exc:  # report, never raise
            probe_result = {"reachable": False, "error": type(exc).__name__}
    artifacts = Path(__file__).resolve().parents[4] / "core" / "models" / "saved"
    return {
        "deterministic_engine": {"status": "available", "scoring_version": "1.0"},
        "llm": {
            "provider": "nebius_token_factory",
            "status": llm_state,
            "model": settings.nebius_model,
            "base_url": settings.nebius_base_url,
            "last_successful_call_at": last_ok,
            "call_status_counts": recent,
            "probe": probe_result,
            "note": "If the provider is unavailable, deterministic investigation continues "
            "and agent narrative is marked unavailable.",
        },
        "ml": ML_AUDIT | {"artifacts_present": artifacts.is_dir() and any(artifacts.iterdir())},
        "policy": {"id": policy_row.id, "version": policy_row.version, "name": policy.name},
        "user": {"id": user.id, "role": user.role},
    }


@router.get("/audit/logs")
def audit_logs(
    db: Database,
    user: CurrentUser,
    resource_id: str | None = Query(default=None, max_length=64),
    outcome: str | None = Query(default=None, max_length=16),
    limit: int = Query(default=100, ge=1, le=500),
):
    access.require_permission(db, user, Permission.VIEW_AUDIT, "audit")
    query = select(AuditLog)
    if resource_id:
        query = query.where(AuditLog.resource_id == resource_id)
    if outcome:
        query = query.where(AuditLog.outcome == outcome)
    rows = db.scalars(query.order_by(AuditLog.created_at.desc()).limit(limit)).all()
    return {
        "items": [
            {
                "id": r.id,
                "user_id": r.user_id,
                "role": r.role,
                "action": r.action,
                "resource_type": r.resource_type,
                "resource_id": r.resource_id,
                "outcome": r.outcome,
                "detail": json.loads(r.detail),
                "created_at": r.created_at,
            }
            for r in rows
        ]
    }


@router.get("/portfolio/intelligence")
def portfolio_intelligence(
    db: Database,
    user: CurrentUser,
    settings: Config,
    segment: str | None = Query(default=None, max_length=64),
):
    access.require_permission(db, user, Permission.VIEW_PORTFOLIO, "portfolio")
    scope = access.user_scope(db, user)
    if segment and not access.segment_permitted(scope, segment):
        raise access.not_found("Segment")
    _, policy = get_active_policy(db, settings.institution_id)
    calc = portfolio_profile(db, access.customer_filter(scope), policy, segment)
    return {
        "calculation": {
            "name": calc.name,
            "version": calc.version,
            "input_digest": calc.input_digest,
        },
        **calc.output,
    }
