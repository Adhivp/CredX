import time
import uuid

from fastapi import HTTPException
from sqlalchemy import case, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.core.security import (
    DUMMY_HASH,
    decode_token,
    encode_token,
    password_hasher,
    token_digest,
    unauthorized,
)
from app.models.auth_session import AuthSession
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest, TokenResponse


def register(db: Session, data: RegisterRequest) -> User:
    user = User(
        email=str(data.email),
        full_name=data.full_name,
        password_hash=password_hasher.hash(data.password.get_secret_value()),
    )
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Unable to register with these details") from None
    db.refresh(user)
    return user


def issue_tokens(settings: Settings, user_id: str, session: AuthSession) -> TokenResponse:
    now = int(time.time())
    lifetime = min(settings.access_token_minutes * 60, session.expires_at - now)
    if lifetime <= 0:
        raise unauthorized()
    refresh = encode_token(settings, user_id, session.id, "refresh", session.expires_at)
    session.refresh_digest = token_digest(refresh)
    return TokenResponse(
        access_token=encode_token(
            settings,
            user_id,
            session.id,
            "access",
            now + lifetime,
        ),
        refresh_token=refresh,
        expires_in=lifetime,
    )


def login(db: Session, settings: Settings, data: LoginRequest) -> TokenResponse:
    now = int(time.time())
    email = str(data.email).lower()
    # Reset only an elapsed lock, retaining failed attempts until success or lock expiry.
    db.execute(
        update(User)
        .where(User.email == email, User.locked_until > 0, User.locked_until <= now)
        .values(failed_attempts=0, locked_until=0)
    )
    db.commit()
    user = db.scalar(select(User).where(User.email == email))
    valid = password_hasher.verify(
        data.password.get_secret_value(), user.password_hash if user else DUMMY_HASH
    )
    if not user or not user.is_active or user.locked_until > now:
        raise unauthorized()
    if not valid:
        # Increment in SQL so parallel requests cannot lose failed-attempt updates.
        db.execute(
            update(User)
            .where(User.id == user.id, User.locked_until <= now)
            .values(
                failed_attempts=User.failed_attempts + 1,
                locked_until=case(
                    (
                        User.failed_attempts + 1 >= settings.login_max_attempts,
                        now + settings.login_lock_seconds,
                    ),
                    else_=0,
                ),
            )
        )
        db.commit()
        raise unauthorized()
    result = db.execute(
        update(User)
        .where(
            User.id == user.id,
            User.locked_until <= now,
            User.is_active.is_(True),
        )
        .values(failed_attempts=0, locked_until=0)
    )
    if result.rowcount != 1:
        db.rollback()
        raise unauthorized()
    session = AuthSession(
        id=str(uuid.uuid4()),
        user_id=user.id,
        expires_at=int(time.time()) + settings.refresh_token_days * 86400,
    )
    tokens = issue_tokens(settings, user.id, session)
    db.add(session)
    db.commit()
    return tokens


def active_session(db: Session, claims: dict) -> tuple[AuthSession, User]:
    session = db.get(AuthSession, claims["sid"])
    user = db.get(User, claims["sub"])
    if (
        not session
        or session.revoked
        or session.expires_at <= int(time.time())
        or not user
        or not user.is_active
        or session.user_id != user.id
    ):
        raise unauthorized()
    return session, user


def refresh(db: Session, settings: Settings, token: str) -> TokenResponse:
    claims = decode_token(settings, token, "refresh")
    session, user = active_session(db, claims)
    # Generate into a detached object: never dirty the row before the compare-and-swap.
    replacement = AuthSession(id=session.id, expires_at=session.expires_at)
    tokens = issue_tokens(settings, user.id, replacement)
    result = db.execute(
        update(AuthSession)
        .where(
            AuthSession.id == session.id,
            AuthSession.refresh_digest == token_digest(token),
            AuthSession.revoked.is_(False),
            AuthSession.expires_at > int(time.time()),
        )
        .values(refresh_digest=replacement.refresh_digest)
    )
    if result.rowcount != 1:
        # Reuse of any previously issued refresh token invalidates this entire login.
        db.execute(update(AuthSession).where(AuthSession.id == session.id).values(revoked=True))
        db.commit()
        raise unauthorized()
    db.commit()
    return tokens


def logout(db: Session, settings: Settings, token: str) -> None:
    claims = decode_token(settings, token, "access")
    session, _ = active_session(db, claims)
    session.revoked = True
    db.commit()


def authenticate_access(db: Session, settings: Settings, token: str) -> User:
    claims = decode_token(settings, token, "access")
    _, user = active_session(db, claims)
    return user
