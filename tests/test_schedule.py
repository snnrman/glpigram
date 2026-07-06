"""Working-hours schedule + off-hours phrasing (quiet hours feature)."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from bot import texts
from bot.schedule import WorkSchedule

KGD = ZoneInfo("Europe/Kaliningrad")
SCHED = WorkSchedule.from_config("09:00-18:00", "1-5", tz_name="Europe/Kaliningrad")

# 2026-07-06 = Monday, 07 = Tue, 10 = Fri, 11 = Sat, 12 = Sun, 13 = next Mon.
MON_1255 = datetime(2026, 7, 6, 12, 55, tzinfo=KGD)
MON_0855 = datetime(2026, 7, 6, 8, 55, tzinfo=KGD)
MON_0900 = datetime(2026, 7, 6, 9, 0, tzinfo=KGD)
MON_1800 = datetime(2026, 7, 6, 18, 0, tzinfo=KGD)
MON_2300 = datetime(2026, 7, 6, 23, 0, tzinfo=KGD)
SAT_1200 = datetime(2026, 7, 11, 12, 0, tzinfo=KGD)


def test_is_working_boundaries():
    assert SCHED.is_working(MON_1255) is True
    assert SCHED.is_working(MON_0900) is True  # start inclusive
    assert SCHED.is_working(MON_1800) is False  # end exclusive
    assert SCHED.is_working(MON_0855) is False  # before opening
    assert SCHED.is_working(MON_2300) is False  # after close
    assert SCHED.is_working(SAT_1200) is False  # weekend


def test_next_open_today_before_opening():
    assert SCHED.next_open(MON_0855) == MON_0900  # same day 09:00


def test_next_open_after_close_is_next_workday():
    assert SCHED.next_open(MON_2300) == datetime(2026, 7, 7, 9, 0, tzinfo=KGD)  # Tue


def test_next_open_weekend_is_monday():
    assert SCHED.next_open(SAT_1200) == datetime(2026, 7, 13, 9, 0, tzinfo=KGD)  # next Mon


def test_next_open_friday_evening_is_monday():
    fri_1830 = datetime(2026, 7, 10, 18, 30, tzinfo=KGD)
    assert SCHED.next_open(fri_1830) == datetime(2026, 7, 13, 9, 0, tzinfo=KGD)


def test_is_working_converts_foreign_tz():
    # The schedule is Kaliningrad (UTC+2); a UTC-labelled instant must be
    # converted, not read verbatim.
    assert SCHED.is_working(datetime(2026, 7, 6, 7, 30, tzinfo=UTC)) is True  # 09:30 local
    assert SCHED.is_working(datetime(2026, 7, 6, 6, 55, tzinfo=UTC)) is False  # 08:55 local
    assert SCHED.is_working(datetime(2026, 7, 6, 16, 30, tzinfo=UTC)) is False  # 18:30 local


def test_next_open_from_utc_moment():
    # Mon 06:55 UTC == 08:55 local -> opens today 09:00 local.
    target = SCHED.next_open(datetime(2026, 7, 6, 6, 55, tzinfo=UTC))
    assert target == datetime(2026, 7, 6, 9, 0, tzinfo=KGD)


def test_from_config_rejects_empty_days():
    with pytest.raises(ValueError, match="no days"):
        WorkSchedule.from_config("09:00-18:00", "5-1", tz_name="Europe/Kaliningrad")


def test_from_config_rejects_inverted_hours():
    with pytest.raises(ValueError, match="start must be before end"):
        WorkSchedule.from_config("18:00-09:00", "1-5", tz_name="Europe/Kaliningrad")


def test_parse_days_list_and_range_mix():
    sched = WorkSchedule.from_config("09:00-18:00", "1-3,6", tz_name="Europe/Kaliningrad")
    assert sorted(sched.work_days) == [1, 2, 3, 6]


def test_next_work_phrase_today():
    # border 08:55 Monday -> "в 09:00" today
    assert texts.next_work_phrase(SCHED.next_open(MON_0855), MON_0855) == "сегодня в 09:00"


def test_next_work_phrase_named_weekday():
    # Saturday -> Monday 09:00
    phrase = texts.next_work_phrase(SCHED.next_open(SAT_1200), SAT_1200)
    assert phrase == "в понедельник в 09:00"


def test_quiet_hours_notice_uses_phrase():
    notice = texts.quiet_hours_notice(SCHED.next_open(SAT_1200), SAT_1200)
    assert "в понедельник в 09:00" in notice
    assert notice.startswith("🌙")


def test_deferred_header_plural():
    assert texts.deferred_batch_header(1).endswith("1 заявка:")
    assert texts.deferred_batch_header(3).endswith("3 заявки:")
    assert texts.deferred_batch_header(5).endswith("5 заявок:")
