"""How hard processing works (Brian, 2026-11-21: "heat OK", Auto + Gentle + Night only after 1 AM).

Speed setting 'processing_mode':
  auto   (default) full speed (all 4 cores) unless the Mac is busy with OTHER work (Brian, 2026-11-22: judge by real
         use, not by keyboard / RustDesk / open chats). Every minute: other apps' CPU (Scribe Bot's own work is skipped)
         and free memory. Other apps using more than 2 of the 8 cores, or under 20% memory free: half speed, until it
         has been calm for 3 minutes. More than 6 cores on 2 checks in a row: pause, until under 4 cores for 2 minutes.
         Overnight backlog (Brian, 2026-11-22): every night 1-7 AM the Inbox is worked through, oldest first, as
         Transcript only (no AI, no cost): see control.overnight_next.
  full   always all 4 cores.
  gentle always half the cores (about twice as long, cooler).
  night  new recordings wait until 1 AM (then full speed until done; nothing new starts after 7 AM).

Warm step-down (Brian, 2026-11-22: "full speed overnight, but throttle back if it heats up"): the hottest core at 90 °C
or more on 2 checks 5 minutes apart drops to half the cores ("Half speed: the Mac is warm") until it has stayed under
80 °C for 5 minutes. This comes before the heat guard's pause, in every speed setting.

Heat guard (separate from Brian's M14 health watcher, which never pauses Scribe Bot): the hottest core at 95 °C or more
on 2 checks 5 minutes apart pauses processing at the next safe point (the same safe points as ❚❚ Pause) and posts
"Paused: the Mac is hot". It resumes once the Mac has stayed under 80 °C for 5 minutes, or after 2 hours paused on
half the cores. Heat comes from the watcher's once-a-minute log (hottest core) when it's fresh, else osx-cpu-temp.

Nothing here reads content: only CPU and memory use per app and temperatures.
"""
from __future__ import annotations

import csv
import os
import re
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

MODES = ("auto", "full", "gentle", "night")
FULL, HALF = 4, 2                  # threads: the M14 has 4 physical cores
HALF_PCT = 200.0                   # other apps' CPU (100 = one core of 8) above this: half speed
PAUSE_PCT, RESUME_PCT = 600.0, 400.0   # above 6 cores twice in a row: pause; back under 4 cores for 2 min: resume
MEM_LOW = 20.0                     # % memory free under this: half speed
CALM_S = 3 * 60                    # Auto stays at half speed this long after the Mac calms down
BUSY_CHECK_S, BUSY_CALM_S = 60, 2 * 60
NIGHT_START, NIGHT_END = 1, 7      # Night only: 1 AM to 7 AM local
HOT_C, COOL_C = 95.0, 80.0
WARM_C = 90.0                      # step down to half the cores first
HOT_CHECK_S = 5 * 60               # two hot checks this far apart pause
COOL_FOR_S = 5 * 60
HEAT_MAX_PAUSE_S = 2 * 3600
WATCHER_LOG = Path.home() / "Library/Application Support/m14-health"
TEMP_TOOL = "/usr/local/bin/osx-cpu-temp"

_state = {"threads": FULL, "last_use": 0.0, "why": "", "busy_seen": None, "busy_calm": None, "hot_since": None, "hot_seen": None, "cool_since": None,
          "heat_paused_at": None, "warm_seen": None, "warm_cool": None}


def _get(conn: sqlite3.Connection, key: str) -> str | None:
    r = conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
    return r[0] if r else None


def _set(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    if value is None:
        conn.execute("DELETE FROM setting WHERE key = ?", (key,))
    else:
        conn.execute("INSERT INTO setting (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                     (key, value))


def mode(conn: sqlite3.Connection) -> str:
    m = _get(conn, "processing_mode")
    return m if m in MODES else "auto"


def set_mode(conn: sqlite3.Connection, m: str) -> None:
    _set(conn, "processing_mode", m if m in MODES else "auto")


# ---------- is someone using the Mac? (pure parsers + one runner) ----------

def parse_busy(ps_text: str) -> float:
    """CPU % used by everything except Scribe Bot's own work. `ps -axo nice=,pcpu=,comm=` lines. Scribe Bot's worker and
    its speech engine run at nice 10 (skipped), and Docker's VM is skipped too (the speaker-labelling engine runs there)."""
    total = 0.0
    for line in (ps_text or "").splitlines():
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        try:
            nice, cpu = int(parts[0]), float(parts[1])
        except ValueError:
            continue
        if nice > 0 or re.search(r"docker|Virtualization", parts[2], re.I):
            continue
        total += cpu
    return total


def _run(args: list[str], run=subprocess.run) -> str:
    try:
        return run(args, capture_output=True, text=True, timeout=10).stdout or ""
    except Exception:
        return ""


def parse_mem_free(text: str) -> float | None:
    m = re.search(r"free percentage:\s*(\d+(?:\.\d+)?)%", text or "")
    return float(m.group(1)) if m else None


def load(run=subprocess.run) -> tuple[float, float | None]:
    """(other apps' CPU %, % memory free). 100 = one core of the 8."""
    return (parse_busy(_run(["ps", "-axo", "nice=,pcpu=,comm="], run)),
            parse_mem_free(_run(["memory_pressure", "-Q"], run)))


def in_use(run=subprocess.run, now: float | None = None) -> str:
    """'' if the Mac has room, else a short reason for half speed ('other apps using 2.4 of 8 cores', ...)."""
    busy, free = load(run)
    _state["busy"] = busy
    if busy > HALF_PCT:
        return f"other apps using {busy / 100:.1f} of 8 cores"
    if free is not None and free < MEM_LOW:
        return f"only {free:.0f}% memory free"
    return ""


def busy_step(conn: sqlite3.Connection, busy: float, now: float | None = None) -> str | None:
    """Auto only: pause while other apps use more than 6 cores (2 checks in a row), resume once under 4 for 2 min."""
    now = now or time.time()
    if _get(conn, "busy_paused") == "1":
        if busy < RESUME_PCT:
            _state["busy_calm"] = _state["busy_calm"] or now
            if now - _state["busy_calm"] >= BUSY_CALM_S:
                _set(conn, "busy_paused", None)
                _state["busy_calm"] = _state["busy_seen"] = None
                return "resumed"
        else:
            _state["busy_calm"] = None
        return None
    if busy > PAUSE_PCT:
        if _state["busy_seen"] is not None and now - _state["busy_seen"] >= BUSY_CHECK_S - 5:
            _set(conn, "busy_paused", "1")
            _state["busy_calm"] = None
            return "paused"
        _state["busy_seen"] = _state["busy_seen"] or now
    else:
        _state["busy_seen"] = None
    return None


# ---------- heat ----------

def hottest_core(run=subprocess.run, now: float | None = None, log_dir: Path = WATCHER_LOG) -> float | None:
    """Hottest core from the M14 health watcher's newest row if it's under 3 min old, else osx-cpu-temp's CPU reading."""
    now = now or time.time()
    try:
        f = sorted(log_dir.glob("samples-*.csv"))[-1]
        with f.open() as fh:
            rows = list(csv.DictReader(fh))
        last = rows[-1]
        if now - datetime.fromisoformat(last["ts"]).timestamp() < 180 and last.get("core_max"):
            return float(last["core_max"])
    except Exception:
        pass
    m = re.search(r"([\d.]+)\s*°?C", _run([TEMP_TOOL, "-c"], run))
    return float(m.group(1)) if m else None


def heat_step(conn: sqlite3.Connection, temp: float | None, now: float | None = None, notify=None) -> str | None:
    """One heat check. Returns 'paused' / 'resumed' / 'resumed_gentle' when it changes something, else None."""
    now = now or time.time()
    s = _state
    paused = _get(conn, "heat_paused") == "1"
    if temp is None:
        return None
    warm = _get(conn, "heat_warm") == "1"   # half speed first, before any pause
    if not warm and temp >= WARM_C:
        if s["warm_seen"] is None:
            s["warm_seen"] = now
        elif now - s["warm_seen"] >= HOT_CHECK_S:
            _set(conn, "heat_warm", "1")
            s["warm_seen"], s["warm_cool"] = None, None
            if not paused and temp < HOT_C:
                return "warm"
    elif not warm and temp < WARM_C - 5:
        s["warm_seen"] = None
    elif warm and temp < COOL_C:
        s["warm_cool"] = s["warm_cool"] or now
        if now - s["warm_cool"] >= COOL_FOR_S:
            _set(conn, "heat_warm", None)
            s["warm_cool"] = None
            if not paused:
                return "cool"
    elif warm:
        s["warm_cool"] = None
    if not paused:
        if temp >= HOT_C:
            if s["hot_seen"] is None:
                s["hot_seen"] = now
            elif now - s["hot_seen"] >= HOT_CHECK_S:
                _set(conn, "heat_paused", "1")
                _set(conn, "heat_paused_at", str(int(now)))
                s["hot_seen"], s["cool_since"] = None, None
                if notify:
                    notify(f"🌡️ Paused: the Mac is hot ({temp:.0f} °C). Processing carries on by itself once it cools.")
                return "paused"
        elif temp < HOT_C - 5:
            s["hot_seen"] = None   # a dip well below resets; hovering at 93-94 keeps counting
        return None
    since = float(_get(conn, "heat_paused_at") or now)
    if temp < COOL_C:
        s["cool_since"] = s["cool_since"] or now
        if now - s["cool_since"] >= COOL_FOR_S:
            _set(conn, "heat_paused", None)
            _set(conn, "heat_paused_at", None)
            s["cool_since"] = None
            if notify:
                notify("▶ Resumed: the Mac has cooled down.")
            return "resumed"
    else:
        s["cool_since"] = None
    if now - since >= HEAT_MAX_PAUSE_S:
        _set(conn, "heat_paused", None)
        _set(conn, "heat_paused_at", None)
        _set(conn, "heat_gentle_until", str(int(now + HEAT_MAX_PAUSE_S)))
        if notify:
            notify("▶ Resumed on half the cores: the Mac stayed warm for 2 hours.")
        return "resumed_gentle"
    return None


def heat_paused(conn: sqlite3.Connection) -> bool:
    return _get(conn, "heat_paused") == "1"


# ---------- the decision ----------

def night_window(now: datetime | None = None) -> bool:
    h = (now or datetime.now()).astimezone().hour
    return NIGHT_START <= h < NIGHT_END


def next_night(now: datetime | None = None) -> datetime:
    now = (now or datetime.now()).astimezone()
    at = now.replace(hour=NIGHT_START, minute=0, second=0, microsecond=0)
    return at if at > now else at + timedelta(days=1)


def threads_for(conn: sqlite3.Connection, use_reason: str, now: float | None = None) -> int:
    """Cores to use right now, from the setting, who's using the Mac and the heat guard's after-pause half speed."""
    now = now or time.time()
    if float(_get(conn, "heat_gentle_until") or 0) > now or _get(conn, "heat_warm") == "1":
        return HALF
    m = mode(conn)
    if m == "gentle":
        return HALF
    if m in ("full", "night"):
        return FULL
    if use_reason:
        _state["last_use"], _state["why"] = now, use_reason
    return HALF if now - _state["last_use"] < CALM_S else FULL


def update(conn: sqlite3.Connection, run=subprocess.run, notify=None, now: float | None = None) -> dict:
    """Called about once a minute by the worker's background thread: heat check + how many cores. Saves a one-line
    'processing_pace' for the Processing page ("Full speed" / "Gentle: keyboard or mouse in use" / ...)."""
    now = now or time.time()
    change = heat_step(conn, hottest_core(run, now), now, notify)
    reason = in_use(run, now) if mode(conn) == "auto" else ""
    if mode(conn) == "auto":
        busy_step(conn, _state.get("busy", 0.0), now)
    elif _get(conn, "busy_paused") == "1":
        _set(conn, "busy_paused", None)   # only Auto pauses for a busy Mac
    t = threads_for(conn, reason, now)
    _state["threads"] = t
    if heat_paused(conn):
        label = "Paused: the Mac is hot"
    elif _get(conn, "busy_paused") == "1":
        label = f"Paused: other apps are using {_state.get('busy', 0) / 100:.1f} of 8 cores"
    elif _get(conn, "heat_warm") == "1":
        label = "Half speed: the Mac is warm"
    elif mode(conn) == "night" and not night_window():
        label = "Night only: waits until 1 AM"
    elif t == HALF:
        why = _state["why"] if mode(conn) == "auto" else ""
        label = f"Half speed{': ' + why if why else ''}"
    else:
        label = "Full speed"
    _set(conn, "processing_pace", label)
    return {"threads": t, "label": label, "heat": change}


def threads() -> int:
    """For the speech engine: cores to use for the next piece (updated by update())."""
    return _state["threads"]


def holds_new_work(conn: sqlite3.Connection, now: datetime | None = None) -> bool:
    """Night only: outside 1-7 AM, nothing new starts (a job already running carries on)."""
    return mode(conn) == "night" and not night_window(now)
