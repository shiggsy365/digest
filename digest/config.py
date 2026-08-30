from functools import lru_cache
from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="DIGEST_", extra="ignore")

    secret_key: str = "development-only-change-me"
    database_url: str = "sqlite:///./data/digest.db"
    library_root: Path = Path("./library")
    data_root: Path = Path("./data")
    public_url: str = "http://localhost:8000"
    scan_interval_seconds: int = 60
    metadata_refresh_hours: int = 168
    timezone: str = "Europe/London"
    session_days: int = 30
    max_kindle_attachment_mb: int = 25
    ereader_spa: bool = False

    @field_validator("library_root", "data_root", mode="before")
    @classmethod
    def expand_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser().resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings()
