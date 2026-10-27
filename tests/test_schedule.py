"""SQP weeks: Sunday-Saturday only, the latest completed week, availability lag."""
from __future__ import annotations

from datetime import date

import pytest

from app.schedule import PeriodError, check_available, latest_completed_week, parse_date, validate_week


def test_initial_backfill_week_is_valid():
    validate_week(date(2026, 10, 11), date(2026, 10, 17))


@pytest.mark.parametrize(
    "start,end",
    [
        (date(2026, 10, 12), date(2026, 10, 18)),  # Monday start
        (date(2026, 10, 11), date(2026, 10, 18)),  # 8 days
        (date(2026, 10, 11), date(2026, 10, 16)),  # ends Friday
        (date(2026, 10, 17), date(2026, 10, 11)),  # reversed
    ],
)
def test_invalid_weeks_rejected(start, end):
    with pytest.raises(PeriodError):
        validate_week(start, end)


def test_parse_date_rejects_garbage():
    with pytest.raises(PeriodError):
        parse_date("2026-13-01")


@pytest.mark.parametrize(
    "today,lag,expected_start",
    [
        (date(2026, 10, 24), 3, date(2026, 10, 11)),  # Saturday: current week incomplete
        (date(2026, 10, 20), 3, date(2026, 10, 11)),  # Tuesday: lag just satisfied
        (date(2026, 10, 19), 3, date(2026, 10, 4)),   # Monday: previous week not ready
        (date(2026, 10, 18), 0, date(2026, 10, 11)),  # Sunday, no lag
        (date(2026, 10, 17), 0, date(2026, 10, 4)),   # Saturday itself never counts
        (date(2027, 1, 5), 3, date(2026, 12, 27)),    # crosses year boundary
    ],
)
def test_latest_completed_week(today, lag, expected_start):
    start, end = latest_completed_week(today, lag)
    assert start == expected_start
    validate_week(start, end)
    assert end < today


def test_incomplete_week_refused():
    with pytest.raises(PeriodError):
        check_available(date(2026, 10, 24), today=date(2026, 10, 24), lag_days=3)


def test_recent_week_warns_but_allowed():
    assert check_available(date(2026, 10, 17), today=date(2026, 10, 19), lag_days=3)
    assert check_available(date(2026, 10, 17), today=date(2026, 10, 24), lag_days=3) is None
