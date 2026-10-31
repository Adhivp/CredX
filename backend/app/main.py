from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.admin import router as admin_router
from app.api.v1.auth import router
from app.api.v1.ews import router as ews_router
from app.api.v1.investigations import router as investigations_router
from app.api.v1.reviews import router as reviews_router
from app.api.v1.system import router as system_router
from app.api.v1.users import router as users_router
from app.core.config import Settings
from app.core.http import configure_http
from app.db.session import configure_database
from app.models import auth_session, ews, governance, investigation, user  # noqa: F401


def create_app() -> FastAPI:
    settings = Settings()
    # Schema is owned by Alembic migrations (run `alembic upgrade head`); startup never
    # silently creates or alters tables.
    engine, session_factory = configure_database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        engine.dispose()

    app = FastAPI(title="CredX API", version="0.2.0", lifespan=lifespan)
    # Bearer tokens are sent in headers, not cookies, so credentials are not needed.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
    )
    configure_http(app)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    if settings.bootstrap_admin_email:
        bootstrap_admin(session_factory, settings.bootstrap_admin_email)
    app.include_router(router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")
    app.include_router(ews_router, prefix="/api/v1")
    app.include_router(investigations_router, prefix="/api/v1")
    app.include_router(reviews_router, prefix="/api/v1")
    app.include_router(admin_router, prefix="/api/v1")
    app.include_router(system_router, prefix="/api/v1")

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def bootstrap_admin(session_factory, email: str) -> None:
    from sqlalchemy import select
    from sqlalchemy.exc import OperationalError

    from app.models.governance import AuditLog
    from app.models.user import User

    try:
        with session_factory() as db:
            if db.scalar(select(User.id).where(User.role == "risk_admin").limit(1)):
                return
            account = db.scalar(select(User).where(User.email == email.lower()))
            if account is None or not account.is_active:
                return
            account.role = "risk_admin"
            db.add(
                AuditLog(
                    user_id=account.id,
                    role="risk_admin",
                    action="bootstrap_admin",
                    resource_type="user",
                    resource_id=account.id,
                    outcome="allowed",
                )
            )
            db.commit()
    except OperationalError:
        pass  # migrations not applied yet; nothing to bootstrap
