from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

Locale = Literal["ar", "en"]
Platform = Literal["matrix", "telegram", "discord"]


class RoomLinkConfig(BaseModel):
    categories: list[str] = Field(default_factory=list)
    allow_relay: bool = False
    full_content: bool = False


class FileConfig(BaseModel):
    rooms: dict[str, RoomLinkConfig] = Field(default_factory=dict)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(default="", alias="DATABASE_URL")
    matrix_homeserver_url: str = Field(default="", alias="MATRIX_HOMESERVER_URL")
    matrix_access_token: str | None = Field(default=None, alias="MATRIX_ACCESS_TOKEN")
    matrix_bot_mxid: str = Field(default="", alias="MATRIX_BOT_MXID")
    matrix_bot_username: str | None = Field(default=None, alias='MATRIX_BOT_USERNAME')
    matrix_bot_password: str | None = Field(default=None, alias='MATRIX_BOT_PASSWORD')
    discourse_base_url: str = Field(default="", alias="DISCOURSE_BASE_URL")
    discourse_api_key: str = Field(default="", alias="DISCOURSE_API_KEY")
    discourse_system_username: str = Field(default="", alias="DISCOURSE_SYSTEM_USERNAME")
    discourse_relay_matrix_username: str = Field(
        default="", alias="DISCOURSE_RELAY_MATRIX_USERNAME"
    )
    discourse_relay_telegram_username: str = Field(
        default="", alias="DISCOURSE_RELAY_TELEGRAM_USERNAME"
    )
    discourse_relay_discord_username: str = Field(
        default="", alias="DISCOURSE_RELAY_DISCORD_USERNAME"
    )
    poll_interval_seconds: int = Field(default=60, alias="POLL_INTERVAL_SECONDS")
    config_file: Path = Field(default=Path("config.yaml"), alias="CONFIG_FILE")
    default_locale: Locale = Field(default="ar", alias="DEFAULT_LOCALE")
    discourse_test_category_id: int | None = Field(default=None, alias="DISCOURSE_TEST_CATEGORY_ID")

    def load_file_config(self) -> FileConfig:
        if not self.config_file.exists():
            return FileConfig()
        payload = yaml.safe_load(self.config_file.read_text(encoding="utf-8")) or {}
        return FileConfig.model_validate(payload)

    def validate_runtime_requirements(self) -> None:
        required_fields = {
            "DATABASE_URL": self.database_url,
            "MATRIX_HOMESERVER_URL": self.matrix_homeserver_url,
            "DISCOURSE_BASE_URL": self.discourse_base_url,
            "DISCOURSE_API_KEY": self.discourse_api_key,
            "DISCOURSE_SYSTEM_USERNAME": self.discourse_system_username,
            "DISCOURSE_RELAY_MATRIX_USERNAME": self.discourse_relay_matrix_username,
            "DISCOURSE_RELAY_TELEGRAM_USERNAME": self.discourse_relay_telegram_username,
            "DISCOURSE_RELAY_DISCORD_USERNAME": self.discourse_relay_discord_username,
        }
        missing = [name for name, value in required_fields.items() if not value]
        if missing:
            joined = ", ".join(missing)
            raise ValueError(f"Missing required environment variables: {joined}")
        if not self.matrix_bot_mxid:
            if not self.matrix_bot_username:
                raise ValueError('Missing required environment variable: MATRIX_BOT_MXID or MATRIX_BOT_USERNAME')
            self.matrix_bot_mxid = self.matrix_bot_username
        if self.matrix_access_token is None and not self.matrix_bot_password:
            raise ValueError('Missing Matrix authentication: MATRIX_ACCESS_TOKEN or MATRIX_BOT_PASSWORD')


def load_settings() -> Settings:
    return Settings()
