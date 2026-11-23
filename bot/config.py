"""Application configuration, loaded from environment variables only.

Fails fast at startup (pydantic raises) if any required secret is missing.
"""

from __future__ import annotations

import os

from pydantic import Field, SecretStr, StrictInt, model_validator
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

    solution_remind_minutes: int = Field(
        default=30,
        ge=0,
        le=1440,
        description="Working minutes between requester solution-confirmation nudges; 0 disables.",
    )

    # --- Unassigned-tickets reminder for the tech group ---
    unassigned_remind_hours: float = Field(
        default=2,
        description="Remind the group about New tickets older than this many WORKING hours.",
    )
    remind_interval_hours: float = Field(
        default=3,
        description="Min WORKING hours between repeated reminders about the same ticket.",
    )

    # --- Quiet hours (off-hours) for tech-group notifications ---
    # Only the dedicated "🔴 Срочно (прод)" level (URGENCY_URGENT) bypasses the
    # quiet queue; ordinary urgency (low/medium/high) is deferred to the morning.
    # There is intentionally no urgency-threshold knob — the breakthrough is tied
    # to the product-level urgent choice, not to a numeric GLPI urgency.
    work_hours: str = Field(default="09:00-18:00", description='Working hours, "HH:MM-HH:MM".')
    work_days: str = Field(default="1-5", description="Working ISO weekdays, Mon=1..Sun=7.")

    # --- Lead access approval (feature: access requests) ---
    lead_logins: str = Field(
        default="",
        description="Comma-separated AD logins of the leads who approve access requests.",
    )
    access_category_id: int | None = Field(
        default=None,
        description="ITILCategory id for access-request tickets (e.g. «Учетные записи»).",
    )
    claude_approver_glpi_ids: str = Field(
        default="",
        description="Comma-separated GLPI user ids allowed to give Claude's second approval.",
    )
    approval_remind_hours: float = Field(
        default=4,
        description="Re-ping a silent lead about a pending approval every N WORKING hours.",
    )

    # Optional observation only: never changes ticket routing or approvals.
    ai_shadow_enabled: bool = False
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-5-mini-2025-08-07"
    ai_shadow_timeout: float = Field(default=15, ge=1, le=60)
    ai_shadow_daily_limit: int = Field(default=100, ge=1, le=1000)
    ai_internal_services: str = Field(
        default="",
        max_length=1000,
        description="Comma-separated existing internal systems exempt from SaaS access approval.",
    )

    # Optional new intake workflow. Never enabled by merely configuring an API key.
    ai_intake_enabled: bool = False
    ai_intake_model: str = Field(
        default="", description="Blank inherits OPENAI_MODEL; does not change shadow settings."
    )
    ai_intake_timeout: float = Field(default=15, ge=1, le=60)
    ai_intake_daily_limit: int = Field(default=100, ge=1, le=1000)
    ai_intake_max_input_chars: int = Field(default=6000, ge=100, le=12000)
    ai_intake_category_ids: dict[str, StrictInt] = Field(
        default_factory=dict,
        description="JSON mapping of all seven intake category keys to distinct GLPI IDs.",
    )

    @model_validator(mode="after")
    def validate_intake_categories(self) -> Settings:
        expected = {
            "accounts",
            "network",
            "hardware",
            "software",
            "infrastructure",
            "purchases",
            "other",
        }
        mapping = self.ai_intake_category_ids
        if self.ai_intake_enabled or mapping:
            if set(mapping) != expected:
                raise ValueError("AI_INTAKE_CATEGORY_IDS must map all seven intake category keys")
            if any(value <= 0 for value in mapping.values()):
                raise ValueError("AI_INTAKE_CATEGORY_IDS values must be positive GLPI IDs")
            if len(set(mapping.values())) != len(mapping):
                raise ValueError("AI_INTAKE_CATEGORY_IDS values must be distinct GLPI IDs")
        return self

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
    def lead_login_list(self) -> list[str]:
        return [x.strip() for x in self.lead_logins.split(",") if x.strip()]

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
