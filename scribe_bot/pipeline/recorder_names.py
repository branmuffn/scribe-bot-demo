"""Google Recorder file names: when a recording started, and a readable fallback title.

Recorder names files by their start time, e.g. "Oct 16 at 09_29" or "Wednesday at 18_53", and Brian sometimes types
a prefix ("Intro call - Oct 14 at 11_30"). The year and the weekday's date aren't in the name, so they're worked out
from the file's modification time (when the recording ended). Times are local (the system zone).
"""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
WEEKDAYS = {d: i for i, d in enumerate(("mon", "tue", "wed", "thu", "fri", "sat", "sun"))}

_MONTH_DAY = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?\s+(\d{1,2}),?\s+at\s+"
                        r"(\d{1,2})[_:.](\d{2})\b", re.IGNORECASE)
_WEEKDAY = re.compile(r"\b(mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?\s+at\s+(\d{1,2})[_:.](\d{2})\b",
                      re.IGNORECASE)
_SLACK = timedelta(minutes=5)  # the start can't be after the file was saved, give or take clock drift


def _local_to_utc(naive_local: datetime) -> datetime:
    return naive_local.astimezone().astimezone(timezone.utc)  # naive -> system local zone (DST-aware)


def _clock(hh: str, mm: str) -> time | None:
    h, m = int(hh), int(mm)
    return time(h, m) if h < 24 and m < 60 else None


def parse_recorder_datetime(name: str, file_mtime: float) -> datetime | None:
    """Start time (UTC) from a Recorder name, or None if the name has no Recorder date."""
    saved = datetime.fromtimestamp(file_mtime)  # naive local
    if m := _MONTH_DAY.search(name):
        month, day, clock = MONTHS[m.group(1).lower()[:3]], int(m.group(2)), _clock(m.group(3), m.group(4))
        if clock is None:
            return None
        for year in (saved.year, saved.year - 1):  # the latest such date not after the save time
            try:
                start = datetime.combine(date(year, month, day), clock)
            except ValueError:
                continue
            if start <= saved + _SLACK:
                return _local_to_utc(start)
        return None
    if m := _WEEKDAY.search(name):
        weekday, clock = WEEKDAYS[m.group(1).lower()[:3]], _clock(m.group(2), m.group(3))
        if clock is None:
            return None
        for back in range(8):  # the most recent such weekday on or before the save date
            day = saved.date() - timedelta(days=back)
            start = datetime.combine(day, clock)
            if day.weekday() == weekday and start <= saved + _SLACK:
                return _local_to_utc(start)
    return None


def started_at(name: str, file_mtime: float, duration_s: float | None) -> str:
    """UTC ISO start time: from the name when it has a Recorder date, else save time minus the recording length."""
    parsed = parse_recorder_datetime(name, file_mtime)
    if parsed is None:
        parsed = datetime.fromtimestamp(file_mtime - (duration_s or 0), timezone.utc)
    return parsed.isoformat(timespec="seconds")


def fallback_title(name: str) -> str:
    """Title when there's no AI title: the name without its Recorder date, or "Conversation" if that's all it was."""
    rest = _WEEKDAY.sub("", _MONTH_DAY.sub("", name))
    rest = re.sub(r"[\s_\-–—·,|]+$", "", re.sub(r"^[\s_\-–—·,|]+", "", rest)).strip()
    return rest or "Conversation"
