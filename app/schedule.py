"""SQP reporting weeks and the single-run lock for the scheduled job.

Brand Analytics weekly reports run Sunday through Saturday. A requested
period is used exactly as given; it is never shifted to another week.
"""
from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

from .exceptions import Locked, PeriodError

SUNDAY = 6  # date.weekday()
SATURDAY = 5


def parse_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        raise PeriodError(f"Invalid date {value!r}; expected YYYY-MM-DD") from None


def validate_week(start: dt.date, end: dt.date) -> None:
    if start.weekday() != SUNDAY:
        raise PeriodError(f"SQP weeks start on Sunday; {start} is a {start:%A}")
    if end != start + dt.timedelta(days=6):
        raise PeriodError(f"SQP weeks are Sunday-Saturday; expected end {start + dt.timedelta(days=6)}, got {end}")


def today_in(timezone: dt.tzinfo) -> dt.date:
    return dt.datetime.now(timezone).date()


def check_available(end: dt.date, today: dt.date, lag_days: int) -> str | None:
    """Reject incomplete weeks. Return a warning if the week may not be published yet."""
    if end >= today:
        raise PeriodError(f"Week ending {end} is not complete yet (today is {today})")
    ready_on = end + dt.timedelta(days=lag_days)
    if today < ready_on:
        return (f"Week ending {end} may not be published yet (expected from {ready_on}); "
                "Amazon will reject the report if so, and no other week will be used")
    return None


def latest_completed_week(today: dt.date, lag_days: int) -> tuple[dt.date, dt.date]:
    """The newest Sunday-Saturday week that ended at least ``lag_days`` ago."""
    cutoff = min(today - dt.timedelta(days=lag_days), today - dt.timedelta(days=1))
    end = cutoff - dt.timedelta(days=(cutoff.weekday() - SATURDAY) % 7)
    return end - dt.timedelta(days=6), end


class AlreadyRunning(Locked):
    """Another scheduled run holds the lock file."""


class RunLock:
    """An OS-level exclusive lock on a file; released automatically if the process dies."""

    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def __enter__(self) -> "RunLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise AlreadyRunning(f"another run holds {self.path}") from None
        self._handle = handle
        return self

    def __exit__(self, *exc) -> None:
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None
