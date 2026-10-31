from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.auth import router
from app.api.v1.ews import router as ews_router
from app.api.v1.users import router as users_router
from app.core.config import Settings
from app.core.http import configure_http
from app.db.session import Base, configure_database


def create_app() -> FastAPI:
    settings = Settings()
    engine, session_factory = configure_database(settings.database_url)

    import app.models.user  # noqa: F401
    import app.models.auth_session  # noqa: F401
    import app.models.ews  # noqa: F401
    Base.metadata.create_all(bind=engine)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        engine.dispose()

    app = FastAPI(title="CredX API", version="0.1.0", lifespan=lifespan)
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
    app.include_router(router, prefix="/api/v1")
    app.include_router(users_router, prefix="/api/v1")
    app.include_router(ews_router, prefix="/api/v1")

    @app.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
