from pathlib import Path

from pydantic import Field
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
