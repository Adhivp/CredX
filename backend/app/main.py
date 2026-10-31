from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.admin import router as admin_router
from app.api.v1.auth import router
from app.api.v1.ews import router as ews_router
from app.api.v1.investigations import router as investigations_router
from app.api.v1.monitoring import router as monitoring_router
from app.api.v1.reviews import router as reviews_router
from app.api.v1.system import router as system_router
from app.api.v1.users import router as users_router
from app.api.v2.ews import router as ews_agent_router
from app.core.config import Settings
from app.core.http import configure_http
from app.db.compat import ensure_compat_columns
from app.db.session import Base, configure_database
from app.models import auth_session, ews, governance, investigation, monitoring, user  # noqa: F401


def create_app() -> FastAPI:
    settings = Settings()
    engine, session_factory = configure_database(settings.database_url)

    # Existing EWS pages rely on startup creating any missing tables. Alembic (run by the
    # container before Uvicorn) owns the schema in deployments; both paths are idempotent.
    Base.metadata.create_all(bind=engine)
    ensure_compat_columns(engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        engine.dispose()

    app = FastAPI(title="CredX API", version="0.2.0", lifespan=lifespan)
    if settings.cors_origins:
        # Explicit allow-list configured: only those origins may call the API.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Accept"],
        )
    else:
        # Unchanged default: the deployed frontend calls the API from another origin.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    configure_http(app)
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    if settings.bootstrap_admin_email:
        bootstrap_admin(session_factory, settings.bootstrap_admin_email)

    # v1: the existing EWS + Level 2 monitoring APIs used by the current pages (unchanged).
    app.include_router(router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")
    app.include_router(ews_router, prefix="/api/v1")
    app.include_router(monitoring_router, prefix="/api/v1")
    # Agent investigation platform (RBAC-governed), on its own paths.
    app.include_router(investigations_router, prefix="/api/v1")
    app.include_router(reviews_router, prefix="/api/v1")
    app.include_router(admin_router, prefix="/api/v1")
    app.include_router(system_router, prefix="/api/v1")
    # v2: the agent-backed, RBAC-governed EWS endpoints. Kept apart from /api/v1/ews so
    # the existing pages keep their current contract.
    app.include_router(ews_agent_router, prefix="/api/v2")

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
