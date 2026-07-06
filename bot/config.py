"""Application configuration, loaded from environment variables only.

Fails fast at startup (pydantic raises) if any required secret is missing.
"""

from __future__ import annotations

import os

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Telegram ---
    telegram_bot_token: str = Field(..., description="Token from @BotFather")
    tech_group_chat_id: int | None = Field(
        default=None,
        description=(
            "Chat id of the technician Telegram group. Account-linking requests are "
            "confirmed with a button there (membership is the trust boundary)."
        ),
    )

    # --- Account linking / technician rights ---
    tech_group_id: int | None = Field(
        default=None,
        description="GLPI group id whose members are technicians (is_tech); None => nobody.",
    )
    link_recheck_ttl: int = Field(
        default=300,
        description="Seconds between GLPI re-checks of a linked account (still active + is_tech).",
    )

    # --- Sync loop (feature 4) ---
    sync_interval: int = Field(
        default=45,
        description="GLPI polling interval in seconds (new tickets, status, followups).",
    )

    # --- Requester "remind" button (feature 3) ---
    remind_cooldown_hours: int = Field(
        default=4,
        description="Min hours between a requester's reminders about the same ticket.",
    )

    # --- GLPI legacy REST API (v1) ---
    glpi_api_url: str = Field(
        ...,
        description="Base URL of apirest.php, e.g. http://127.0.0.1/apirest.php",
    )
    glpi_app_token: str = Field(
        default="",
        description=(
            "GLPI App-Token. Optional: the localhost API client does not require "
            "one, so leave empty unless the GLPI instance enforces App-Tokens."
        ),
    )
    glpi_user_token: str = Field(..., description="GLPI user_token of the service account")
    glpi_timeout: float = Field(default=20.0, description="HTTP timeout for GLPI calls, seconds")

    # --- Behaviour ---
    category_cache_ttl: int = Field(
        default=600, description="ITILCategory cache lifetime, seconds (default 10 min)"
    )
    db_path: str = Field(
        default="/var/lib/glpi-tgbot/glpi-tgbot.sqlite3",
        description="Path to the SQLite database file",
    )
    log_level: str = Field(default="INFO")

    @property
    def glpi_front_base(self) -> str:
        """Web UI base URL, derived from the API URL, for building ticket links.

        ``http://host/apirest.php`` -> ``http://host``.
        """
        url = self.glpi_api_url.rstrip("/")
        for suffix in ("/apirest.php", "/apirest", "/api.php"):
            if url.endswith(suffix):
                return url[: -len(suffix)]
        return url

    @property
    def https_proxy(self) -> str | None:
        """Standard outbound proxy, honoured by httpx (trust_env) and aiogram.

        Read from the environment directly so operators can rely on the usual
        ``HTTPS_PROXY`` variable without a bespoke setting name.
        """
        return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")


def load_settings() -> Settings:
    """Instantiate settings; raises ``pydantic.ValidationError`` if incomplete."""
    return Settings()
