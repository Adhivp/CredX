import time
import uuid

from sqlalchemy import Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.session import Base


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(100))
    password_hash: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(default=True)
    failed_attempts: Mapped[int] = mapped_column(default=0)
    locked_until: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[int] = mapped_column(default=lambda: int(time.time()))
    # Self-registration never grants a role; a risk administrator assigns one.
    role: Mapped[str] = mapped_column(String(32), default="unassigned", server_default="unassigned")
    # Maximum exposure this user may approve under institutional delegation (managers only).
    approval_limit: Mapped[float | None] = mapped_column(Float, nullable=True)
