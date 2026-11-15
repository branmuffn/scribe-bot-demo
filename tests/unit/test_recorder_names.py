"""Recorder file names -> start time and fallback title. Synthetic names only."""
import time
from datetime import datetime

import pytest

from scribe_bot.pipeline.recorder_names import fallback_title, parse_recorder_datetime, started_at

# These tests switch the process time zone with time.tzset(), which some Python builds (e.g. some uv-managed ones)
# don't have. Skip cleanly there instead of erroring; python.org and Homebrew Pythons run them.
pytestmark = pytest.mark.skipif(not hasattr(time, "tzset"), reason="time.tzset() is not available on this Python build")


@pytest.fixture(autouse=True)
def boston_time(monkeypatch):
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def mtime(*args) -> float:
    """Local wall-clock time -> epoch seconds (the file's modification time)."""
    return datetime(*args).timestamp()


def utc(dt) -> str:
    return dt.isoformat(timespec="minutes")


def test_month_day_uses_the_save_year():
    got = parse_recorder_datetime("Oct 3 at 14_05", mtime(2026, 10, 3, 14, 40))
    assert utc(got) == "2026-10-03T18:05+00:00"  # 2:05 pm EDT


def test_prefix_typed_by_brian_is_ignored():
    got = parse_recorder_datetime("Weekly sync - Oct 3 at 14_05", mtime(2026, 10, 3, 15, 0))
    assert utc(got) == "2026-10-03T18:05+00:00"


def test_late_december_saved_in_january_is_last_year():
    got = parse_recorder_datetime("Dec 31 at 23_30", mtime(2027, 1, 1, 0, 20))
    assert utc(got) == "2027-01-01T04:30+00:00"  # 11:30 pm EST on 2026-12-31


def test_winter_dates_use_standard_time():
    assert utc(parse_recorder_datetime("Jan 15 at 09_00", mtime(2027, 1, 15, 9, 45))) == "2027-01-15T14:00+00:00"


def test_weekday_is_the_most_recent_one():
    # Saved Friday 2026-10-09; "Wednesday" means 2026-10-07.
    got = parse_recorder_datetime("Planning chat - Wednesday at 18_53", mtime(2026, 10, 9, 9, 0))
    assert utc(got) == "2026-10-07T22:53+00:00"


def test_weekday_same_day_and_a_week_back():
    friday = mtime(2026, 10, 9, 10, 30)
    assert utc(parse_recorder_datetime("Friday at 10_00", friday)) == "2026-10-09T14:00+00:00"  # earlier today
    assert utc(parse_recorder_datetime("Friday at 16_00", friday)) == "2026-10-02T20:00+00:00"  # later today -> last week


@pytest.mark.parametrize("name", ["Team sync", "notes 2026-10-03", "Oct 32 at 10_00", "Oct 3 at 25_00", "Sometime at 10_00"])
def test_names_without_a_valid_recorder_date(name):
    assert parse_recorder_datetime(name, mtime(2026, 10, 9, 12, 0)) is None


def test_started_at_falls_back_to_save_time_minus_length():
    saved = mtime(2026, 10, 9, 12, 0)
    assert started_at("Team sync", saved, 600).startswith("2026-10-09T15:50")  # noon EDT minus 10 min, in UTC
    assert started_at("Oct 9 at 11_45", saved, 600).startswith("2026-10-09T15:45")


@pytest.mark.parametrize("name, title", [
    ("Oct 16 at 09_29", "Conversation"),
    ("Wednesday at 18_53", "Conversation"),
    ("Weekly sync - Oct 3 at 14_05", "Weekly sync"),
    ("Project kickoff _ Phase 2 - Oct 14 at 11_30", "Project kickoff _ Phase 2"),
    ("Coffee chat", "Coffee chat"),
    ("", "Conversation"),
])
def test_fallback_title(name, title):
    assert fallback_title(name) == title
