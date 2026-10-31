import hashlib
import time
import uuid

import jwt
from fastapi import HTTPException
from pwdlib import PasswordHash

from app.core.config import Settings

password_hasher = PasswordHash.recommended()
DUMMY_HASH = password_hasher.hash("not-a-real-user-password")


def unauthorized() -> HTTPException:
    return HTTPException(
        401, "Invalid credentials or token", headers={"WWW-Authenticate": "Bearer"}
    )


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def encode_token(settings: Settings, user_id: str, session_id: str, kind: str, expires: int) -> str:
    now = int(time.time())
    return jwt.encode(
        {
            "sub": user_id,
            "sid": session_id,
            "jti": str(uuid.uuid4()),
            "type": kind,
            "iat": now,
            "nbf": now,
            "exp": expires,
            "iss": settings.jwt_issuer,
            "aud": settings.jwt_audience,
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def decode_token(settings: Settings, token: str, kind: str) -> dict:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={"require": ["sub", "sid", "jti", "type", "iat", "nbf", "exp", "iss", "aud"]},
        )
        if claims["type"] != kind or not isinstance(claims["sid"], str):
            raise unauthorized()
        return claims
    except jwt.InvalidTokenError:
        raise unauthorized() from None
