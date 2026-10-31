"""Request bodies for the Level 2 monitoring, investigation and intervention API."""

from pydantic import BaseModel, ConfigDict, Field, field_validator

CASE_STATUSES = ["open", "under_investigation", "awaiting_information", "action_in_progress", "monitoring", "closed"]
DISPOSITIONS = ["pending", "accepted", "false_positive"]
OWNER_ROLES = ["relationship_manager", "risk_analyst", "collections", "credit_committee"]
RISK_LEVELS = ["Low", "Medium", "High", "Critical", "Insufficient Data"]
INTERVENTION_STATUSES = [
    "planned", "pending_approval", "approved", "initiated", "completed", "deferred", "escalated", "cancelled",
]
PRIORITIES = ["P1", "P2", "P3", "P4"]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str | None = Field(default=None, max_length=128)


def _one_of(allowed: list[str], v: str | None, name: str) -> str | None:
    if v is not None and v not in allowed:
        raise ValueError(f"{name} must be one of: {', '.join(allowed)}")
    return v


class CaseCreate(_Body):
    customer_id: str = Field(min_length=1, max_length=64)
    owner: str = Field(default="", max_length=128)
    owner_role: str = Field(default="risk_analyst")
    summary: str = Field(default="", max_length=4000)

    @field_validator("owner_role")
    @classmethod
    def _role(cls, v):
        return _one_of(OWNER_ROLES, v, "owner_role")


class CaseUpdate(_Body):
    status: str | None = None
    disposition: str | None = None
    owner: str | None = Field(default=None, max_length=128)
    owner_role: str | None = None
    note: str | None = Field(default=None, max_length=4000)

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        return _one_of(CASE_STATUSES, v, "status")

    @field_validator("disposition")
    @classmethod
    def _disp(cls, v):
        return _one_of(DISPOSITIONS, v, "disposition")

    @field_validator("owner_role")
    @classmethod
    def _role(cls, v):
        return _one_of(OWNER_ROLES, v, "owner_role")


class NoteCreate(_Body):
    body: str = Field(min_length=1, max_length=4000)
    kind: str = "note"

    @field_validator("kind")
    @classmethod
    def _kind(cls, v):
        return _one_of(["note", "decision", "evidence"], v, "kind")


class InfoRequestCreate(_Body):
    items: list[str] = Field(min_length=1, max_length=20)
    requested_from: str = Field(default="customer", max_length=128)
    due_date: str = Field(default="", max_length=10)
    message: str = Field(default="", max_length=2000)


class InfoRequestUpdate(_Body):
    status: str = "fulfilled"
    response: str = Field(default="", max_length=4000)

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        return _one_of(["open", "fulfilled", "cancelled"], v, "status")


class CategoryCorrection(_Body):
    corrected_category: str | None = None  # None clears the correction
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("corrected_category")
    @classmethod
    def _cat(cls, v):
        return _one_of(RISK_LEVELS, v, "corrected_category")


class EventClassificationCreate(_Body):
    classification: str
    reason: str = Field(min_length=1, max_length=2000)
    case_id: str | None = None

    @field_validator("classification")
    @classmethod
    def _cls(cls, v):
        from app.services.scoring import EVENT_CLASSIFICATIONS
        return _one_of(sorted(EVENT_CLASSIFICATIONS), v, "classification")


class InterventionCreate(_Body):
    customer_id: str = Field(min_length=1, max_length=64)
    scenario: str
    case_id: str | None = None
    facility_ref: str = Field(default="", max_length=64)
    owner: str = Field(default="", max_length=128)
    owner_role: str | None = None
    priority: str | None = None
    target_date: str | None = Field(default=None, max_length=10)
    notes: str = Field(default="", max_length=4000)

    @field_validator("scenario")
    @classmethod
    def _scenario(cls, v):
        from app.services.recommendations import PLAYBOOK
        return _one_of(sorted(PLAYBOOK), v, "scenario")

    @field_validator("owner_role")
    @classmethod
    def _role(cls, v):
        return _one_of(OWNER_ROLES, v, "owner_role")

    @field_validator("priority")
    @classmethod
    def _prio(cls, v):
        return _one_of(PRIORITIES, v, "priority")


class InterventionUpdate(_Body):
    status: str | None = None
    owner: str | None = Field(default=None, max_length=128)
    owner_role: str | None = None
    priority: str | None = None
    target_date: str | None = Field(default=None, max_length=10)
    notes: str | None = Field(default=None, max_length=4000)
    decision: str | None = Field(default=None, max_length=4000)
    outcome: str | None = Field(default=None, max_length=4000)
    evidence: str | None = Field(default=None, max_length=2000)  # appended to the evidence list

    @field_validator("status")
    @classmethod
    def _status(cls, v):
        return _one_of(INTERVENTION_STATUSES, v, "status")

    @field_validator("owner_role")
    @classmethod
    def _role(cls, v):
        return _one_of(OWNER_ROLES, v, "owner_role")

    @field_validator("priority")
    @classmethod
    def _prio(cls, v):
        return _one_of(PRIORITIES, v, "priority")


class ApprovalDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approve: bool
    comment: str = Field(min_length=1, max_length=2000)
