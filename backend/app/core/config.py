from pathlib import Path

from pydantic import Field, field_validator
from sqlalchemy.engine import make_url
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=BACKEND_DIR / ".env", extra="ignore")

    jwt_secret: str = Field(min_length=48, repr=False)
    database_url: str = f"sqlite:///{BACKEND_DIR / 'data' / 'credx.db'}"
    jwt_issuer: str = "credx-api"
    jwt_audience: str = "credx-client"
    access_token_minutes: int = Field(default=10, ge=1, le=30)
    refresh_token_days: int = Field(default=7, ge=1, le=30)
    cors_origins: list[str] = []
    login_max_attempts: int = Field(default=5, ge=1, le=20)
    login_lock_seconds: int = Field(default=900, ge=1)
    nebius_api_key: str = ""
    nebius_base_url: str = "https://api.tokenfactory.us-central1.nebius.com/v1/"
    nebius_model: str = "deepseek-ai/DeepSeek-V4-Flash-0731"

    @field_validator("database_url")
    @classmethod
    def anchor_relative_sqlite(cls, v: str) -> str:
        # A relative path like sqlite:///./data/credx.db would otherwise resolve
        # against the shell's cwd, so starting the server from another directory
        # silently opens a new, empty database.
        url = make_url(v)
        if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
            path = Path(url.database)
            if not path.is_absolute():
                return url.set(database=str((BACKEND_DIR / path).resolve())).render_as_string(hide_password=False)
        return v
