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
    # Runtime setting overrides (see core/settings.py); lives on the data volume.
    settings_path: str = Field(default="./data/settings.json")
    # Last seen credential states and the bot's login report, beside the settings store.
    credentials_path: str = Field(default="./data/credentials.json")

    # Health poll (stuck-download remediation). 0 disables the poll.
    health_poll_interval_seconds: float = Field(default=300.0)
    auto_resume_max: int = Field(default=3)
    auto_retry_max: int = Field(default=2)

    # Follow poll (auto-download for followed shows). 0 pauses the poll.
    follow_poll_interval_seconds: int = Field(default=21600)
    follow_max_submissions_per_tick: int = Field(default=3)
    follow_delay_hours: int = Field(default=12)
    follow_minimum_seeders: int = Field(default=50)
    # Season packs for the complete seasons of a follow, and the two retry profiles.
    follow_pack_minimum_seeders: int = Field(default=20)
    follow_pack_timeout_seconds: int = Field(default=30)
    follow_pack_retry_timeout_seconds: int = Field(default=90)
    follow_pack_retry_minimum_seeders: int = Field(default=5)

    # Discord channel webhook for follow notices; unset means no notice.
    discord_notify_webhook_url: str | None = Field(default=None)


config: AppConfig = AppConfig()
