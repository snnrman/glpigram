"""GLPI datetimes are UTC; the bot renders/compares them in its local zone."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from bot import timeutil

# Europe/Kaliningrad is UTC+2 year-round (no DST since 2014).
KGD = ZoneInfo("Europe/Kaliningrad")


def test_parse_glpi_utc_is_utc_aware():
    dt = timeutil.parse_glpi_utc("2026-07-06 12:00:00")
    assert dt is not None
    assert dt.tzinfo is UTC
    assert (dt.year, dt.month, dt.day, dt.hour) == (2026, 7, 6, 12)


def test_parse_glpi_utc_bad_input():
    assert timeutil.parse_glpi_utc(None) is None
    assert timeutil.parse_glpi_utc("") is None
    assert timeutil.parse_glpi_utc("not a date") is None


def test_hours_since_uses_utc_math():
    # created 12:00 UTC, "now" 17:30 UTC -> 5 whole hours (tz-independent).
    now = datetime(2026, 7, 6, 17, 30, tzinfo=UTC).timestamp()
    assert timeutil.hours_since("2026-07-06 12:00:00", now_ts=now) == 5


def test_hours_since_not_misread_as_local():
    # Regression: a UTC string must NOT be interpreted in the bot's local zone.
    # If it were read as Kaliningrad (UTC+2), the instant would shift by 2h and
    # this exact-hour delta would be wrong. now == created -> exactly 0.
    now = datetime(2026, 7, 6, 12, 0, tzinfo=UTC).timestamp()
    assert timeutil.hours_since("2026-07-06 12:00:00", now_ts=now) == 0


def test_hours_since_clamps_future_to_zero():
    now = datetime(2026, 7, 6, 12, 0, tzinfo=UTC).timestamp()
    assert timeutil.hours_since("2026-07-06 15:00:00", now_ts=now) == 0


def test_glpi_local_converts_to_working_hours():
    # 08:30 UTC -> 10:30 Kaliningrad: same day, hour 10 -> inside 9..18 working hours.
    local = timeutil.glpi_local("2026-07-06 08:30:00", KGD)
    assert (local.day, local.hour, local.minute) == (6, 10, 30)
    assert 9 <= local.hour < 18  # would be handled as "working hours"


def test_glpi_local_rolls_over_midnight():
    # 23:30 UTC -> 01:30 next day Kaliningrad: outside working hours.
    local = timeutil.glpi_local("2026-07-06 23:30:00", KGD)
    assert (local.day, local.hour) == (7, 1)
    assert not (9 <= local.hour < 18)
