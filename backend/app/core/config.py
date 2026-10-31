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
    nebius_api_key: str = Field(default="", repr=False)
    nebius_base_url: str = "https://api.tokenfactory.us-central1.nebius.com/v1/"
    nebius_model: str = "deepseek-ai/DeepSeek-V4-Flash-0731"
    # Agent reasoning is optional: when disabled or unavailable, deterministic EWS
    # functionality continues and agent narrative is marked unavailable.
    llm_enabled: bool = True
    llm_timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    llm_max_retries: int = Field(default=1, ge=0, le=3)
    # The default DeepSeek model reasons before answering; with thinking enabled, a small
    # output budget is consumed entirely by hidden reasoning and the content is empty.
    llm_disable_thinking: bool = True
    llm_max_output_tokens: int = Field(default=1500, ge=64, le=8192)
    # USD per million tokens, used only for budget enforcement and cost visibility.
    llm_input_cost_per_mtok: float = Field(default=0.0, ge=0)
    llm_output_cost_per_mtok: float = Field(default=0.0, ge=0)
    institution_id: str = Field(default="default", min_length=1, max_length=64)
    # Run investigations in a background thread after the HTTP response (False in tests).
    investigations_async: bool = True
    # Promotes this already-registered user to risk_admin at startup, only while no risk
    # administrator exists. Lets a deployment create its first administrator.
    bootstrap_admin_email: str = ""
