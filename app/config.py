from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "Channel Radar"
    environment: str = "development"
    database_url: str = "sqlite:///./channel_radar.db"
    base_url: str = "http://localhost:8000"
    cron_secret: str = ""
    gemini_api_key: str = ""
    gemini_model: str = "gemini-3.8-flash"
    gemini_fallback_model: str = "gemini-3.5-flash-lite"
    telegram_timeout_seconds: float = Field(default=15.0, ge=3.0, le=60.0)
    collection_bucket_minutes: int = Field(default=60, ge=5, le=1440)
    scheduler_enabled: bool = False

    @property
    def sqlalchemy_database_url(self) -> str:
        url = self.database_url.strip()
        if url.startswith("postgres://"):
            return "postgresql+psycopg://" + url.removeprefix("postgres://")
        if url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
            return "postgresql+psycopg://" + url.removeprefix("postgresql://")
        return url


@lru_cache
def get_settings() -> Settings:
    return Settings()
