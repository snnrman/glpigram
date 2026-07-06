"""GLPI datetime handling.

GLPI (PHP) runs in **UTC** and its API returns naive datetime strings
("YYYY-MM-DD HH:MM:SS") with no timezone. The bot process runs in the timezone
from its environment (``TZ``, e.g. Europe/Kaliningrad). So every GLPI date must
be interpreted as UTC and only then converted to the bot's local zone for
display or hour-of-day logic. Centralised here so callers can't get it wrong.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, tzinfo

_GLPI_FMT = "%Y-%m-%d %H:%M:%S"


def parse_glpi_utc(value: str | None) -> datetime | None:
    """Parse a GLPI datetime string as a UTC-aware ``datetime`` (or None)."""
    if not value:
        return None
    try:
        return datetime.strptime(value, _GLPI_FMT).replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def to_local(dt: datetime, tz: tzinfo | None = None) -> datetime:
    """Convert an aware datetime to the bot's local zone (system ``TZ`` if tz=None)."""
    return dt.astimezone(tz)


def glpi_local(value: str | None, tz: tzinfo | None = None) -> datetime | None:
    """Parse a GLPI UTC string and return it in the bot's local zone."""
    dt = parse_glpi_utc(value)
    return to_local(dt, tz) if dt is not None else None


def hours_since(value: str | None, *, now_ts: float | None = None) -> int | None:
    """Whole hours elapsed since a GLPI (UTC) timestamp; None if unparseable.

    Timezone-safe: the duration is computed between two absolute instants, so it
    doesn't depend on the local zone — the only requirement is reading the GLPI
    string as UTC, which :func:`parse_glpi_utc` does.
    """
    dt = parse_glpi_utc(value)
    if dt is None:
        return None
    now = time.time() if now_ts is None else now_ts
    return max(0, int((now - dt.timestamp()) // 3600))
