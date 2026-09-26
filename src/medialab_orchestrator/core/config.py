"""Application configuration loaded from environment variables and an optional .env file.

Every field is optional at import time (CI has no .env and no secrets) but the
service does not function without real downstream URLs/keys at runtime.
"""

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppConfig(BaseSettings):
    """Orchestrator configuration parameters."""

    model_config = SettingsConfigDict(env_file=".env")

    api_key: str | None = Field(default=None)
    api_host: str = Field(default="0.0.0.0")
    api_port: int = Field(default=8000)

    torrent_downloader_url: str | None = Field(default=None)
    torrent_downloader_api_key: str | None = Field(default=None)

    medialab_jellyfin_url: str | None = Field(default=None)
    medialab_jellyfin_api_key: str | None = Field(default=None)

    media_mount_path: str = Field(default="/media")
    db_path: str = Field(default="./data/orchestrator.db")

    # Health poll (stuck-download remediation). 0 disables the poll.
    health_poll_interval_seconds: float = Field(default=300.0)
    auto_resume_max: int = Field(default=3)
    auto_retry_max: int = Field(default=2)


config: AppConfig = AppConfig()
