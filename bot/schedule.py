"""Working-hours schedule for quiet-hours notification handling.

Configured from ``WORK_HOURS`` ("09:00-18:00"), ``WORK_DAYS`` ("1-5", ISO
weekdays Mon=1..Sun=7) and the bot timezone (``TZ`` env). All datetimes are
handled tz-aware and evaluated in the schedule's zone.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo
from zoneinfo import ZoneInfo


def _parse_hm(value: str) -> time:
    hh, mm = value.strip().split(":")
    return time(int(hh), int(mm))


def _parse_days(spec: str) -> frozenset[int]:
    days: set[int] = set()
    for token in spec.split(","):
        token = token.strip()
        if not token:
            continue
        if "-" in token:
            lo, hi = token.split("-")
            days.update(range(int(lo), int(hi) + 1))
        else:
            days.add(int(token))
    return frozenset(d for d in days if 1 <= d <= 7)


@dataclass(frozen=True, slots=True)
class WorkSchedule:
    start: time
    end: time
    work_days: frozenset[int]  # ISO weekday, Mon=1..Sun=7
    tz: tzinfo

    @classmethod
    def from_config(
        cls, work_hours: str, work_days: str, *, tz_name: str | None = None
    ) -> WorkSchedule:
        """Build from env-style strings; raises ``ValueError`` on nonsense config.

        Fail fast at startup (CLAUDE.md): an empty day set or inverted hours
        would silently defer every notification forever.
        """
        start_s, end_s = work_hours.split("-")
        start, end = _parse_hm(start_s), _parse_hm(end_s)
        days = _parse_days(work_days)
        if not days:
            raise ValueError(f"WORK_DAYS={work_days!r} selects no days")
        if start >= end:
            raise ValueError(f"WORK_HOURS={work_hours!r}: start must be before end")
        tz = ZoneInfo(tz_name) if tz_name else datetime.now().astimezone().tzinfo
        return cls(start, end, days, tz)

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def is_working(self, moment: datetime) -> bool:
        """True if ``moment`` falls on a work day within working hours."""
        local = moment.astimezone(self.tz)
        return local.isoweekday() in self.work_days and self.start <= local.time() < self.end

    def next_open(self, moment: datetime) -> datetime:
        """The next instant work opens at or after ``moment`` (in the schedule tz).

        If ``moment`` is a work day before opening, that's today's start; otherwise
        the start of the next work day.
        """
        local = moment.astimezone(self.tz)
        for offset in range(0, 8):
            day = (local + timedelta(days=offset)).date()
            candidate = datetime.combine(day, self.start, tzinfo=self.tz)
            if candidate.isoweekday() in self.work_days and candidate >= local:
                return candidate
        # work_days is non-empty in practice; fall back to the local moment.
        return local
