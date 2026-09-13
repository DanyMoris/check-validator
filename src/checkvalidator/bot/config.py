"""Настройки бота из переменных окружения и файла .env."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent.parent.parent


def parse_user_ids(raw: str) -> set[int]:
    """Разбирает строку вида «123, 456» в множество идентификаторов Telegram."""
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        ids.add(int(part))
    return ids


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = Field(min_length=1, validation_alias="BOT_TOKEN")
    allowed_user_ids: str = Field(default="", validation_alias="ALLOWED_USER_IDS")
    rate_limit_seconds: float = Field(default=8.0, validation_alias="RATE_LIMIT_SECONDS")
    max_file_mb: float = Field(default=15.0, validation_alias="MAX_FILE_MB")
    data_dir: Path = Field(default=ROOT / "data", validation_alias="DATA_DIR")
    profiles_path: Path | None = Field(default=None, validation_alias="PROFILES_PATH")

    @property
    def allowed_ids(self) -> set[int]:
        return parse_user_ids(self.allowed_user_ids)

    @property
    def max_file_bytes(self) -> int:
        return int(self.max_file_mb * 1024 * 1024)

    @property
    def inbox_dir(self) -> Path:
        return self.data_dir / "inbox"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.db"
