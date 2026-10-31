"""Access scopes, versioned institutional policy and the security audit log."""

import time
import uuid

from sqlalchemy import ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.append_only import make_append_only
from app.db.session import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> int:
    return int(time.time())


class UserScope(Base):
    """Data a user may see. scope_type: 'all', 'segment' (portfolio_segment) or 'customer'."""

    __tablename__ = "user_scopes"
    __table_args__ = (
        UniqueConstraint("user_id", "scope_type", "scope_value", name="uq_user_scope"),
        Index("ix_user_scopes_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    scope_type: Mapped[str] = mapped_column(String(16))
    scope_value: Mapped[str] = mapped_column(String(64), default="")
    granted_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[int] = mapped_column(default=_now)


class InstitutionPolicy(Base):
    """Immutable policy versions. Activating a new version supersedes, never edits, the old."""

    __tablename__ = "institution_policies"
    __table_args__ = (UniqueConstraint("institution_id", "version", name="uq_policy_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    institution_id: Mapped[str] = mapped_column(String(64), default="default")
    version: Mapped[int] = mapped_column(Integer)
    config: Mapped[str] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(default=False)
    created_by: Mapped[str | None] = mapped_column(String(36), nullable=True)
    change_reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[int] = mapped_column(default=_now)


class AuditLog(Base):
    """Append-only (enforced by database triggers) record of security-sensitive operations."""

    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_resource", "resource_type", "resource_id"),
        Index("ix_audit_user", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    role: Mapped[str] = mapped_column(String(32), default="")
    action: Mapped[str] = mapped_column(String(64))
    resource_type: Mapped[str] = mapped_column(String(32), default="")
    resource_id: Mapped[str] = mapped_column(String(64), default="")
    outcome: Mapped[str] = mapped_column(String(16))
    detail: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[int] = mapped_column(default=_now)


make_append_only(AuditLog.__table__)
