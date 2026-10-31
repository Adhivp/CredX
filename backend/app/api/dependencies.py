from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from app.core.config import Settings
from app.db.session import get_db
from app.models.user import User
from app.services.auth import authenticate_access

Database = Annotated[Session, Depends(get_db)]
bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


Config = Annotated[Settings, Depends(get_settings)]


Credentials = Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]


def get_current_user(
    db: Database,
    settings: Config,
    credentials: Credentials,
):
    return authenticate_access(db, settings, credentials.credentials if credentials else "")


CurrentUser = Annotated[User, Depends(get_current_user)]
