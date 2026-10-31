from fastapi import APIRouter, Response

from app.api.dependencies import Config, Credentials, Database
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UserResponse,
)
from app.services import auth as service

router = APIRouter(prefix="/auth", tags=["authentication"])


@router.post("/register", response_model=UserResponse, status_code=201)
def register(data: RegisterRequest, db: Database):
    return service.register(db, data)


@router.post("/login", response_model=TokenResponse)
def login(data: LoginRequest, db: Database, settings: Config):
    return service.login(db, settings, data)


@router.post("/refresh", response_model=TokenResponse)
def refresh(data: RefreshRequest, db: Database, settings: Config):
    return service.refresh(db, settings, data.refresh_token.get_secret_value())


@router.post("/logout", status_code=204)
def logout(db: Database, settings: Config, credentials: Credentials):
    service.logout(db, settings, credentials.credentials if credentials else "")
    return Response(status_code=204)
