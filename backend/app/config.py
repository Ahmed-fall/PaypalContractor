"""Application settings, read once from the environment (plan Appendix A.1).

Startup fails loudly when a required value is missing or when ``PAYPAL_API_BASE_URL``
points anywhere but the PayPal sandbox, so no real money can ever move.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SANDBOX_API_BASE_URL = "https://api-m.sandbox.paypal.com"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "sqlite:///./fd.db"
    paypal_api_base_url: str = SANDBOX_API_BASE_URL
    paypal_client_id: str = Field(min_length=1)
    paypal_client_secret: SecretStr
    # Empty until the webhook is registered; the receiver then rejects every event.
    paypal_webhook_id: str = ""
    demo_key: SecretStr = Field(min_length=24)
    cors_origins: str = "http://localhost:5173"
    leash_enabled: bool = True
    payouts_poll_seconds: int = Field(default=10, gt=0)
    payouts_poll_max_seconds: int = Field(default=120, gt=0)
    seed_demo_data: bool = False
    log_level: str = "INFO"

    @field_validator("paypal_api_base_url")
    @classmethod
    def _sandbox_only(cls, value: str) -> str:
        url = value.strip().rstrip("/")
        if url != SANDBOX_API_BASE_URL:
            raise ValueError(
                f"refusing to start: PAYPAL_API_BASE_URL must be {SANDBOX_API_BASE_URL}"
            )
        return url

    @field_validator("database_url")
    @classmethod
    def _psycopg_driver(cls, value: str) -> str:
        # Render hands out postgres:// URLs; SQLAlchemy needs the psycopg 3 driver name.
        for prefix in ("postgres://", "postgresql://"):
            if value.startswith(prefix):
                return "postgresql+psycopg://" + value[len(prefix) :]
        return value

    @property
    def cors_origin_list(self) -> list[str]:
        return [
            origin.strip() for origin in self.cors_origins.split(",") if origin.strip()
        ]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]  # required values come from the env
