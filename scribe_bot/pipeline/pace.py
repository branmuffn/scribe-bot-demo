"""How hard processing works (Brian, 2026-11-21: "heat OK", Auto + Gentle + Night only after 1 AM).

Speed setting 'processing_mode':
  auto   (default) full speed while nobody uses the Mac; half the cores (Gentle) while someone does, and for 10 minutes
         after they stop. "Someone uses the Mac" = any of: keyboard or mouse in the last 5 min, an SSH session typed in
         within 30 min, a Screen Sharing or RustDesk connection, or a Claude / Codex chat that wrote in the last 10 min
         WHILE the Mac is busy with other work (Brian, 2026-11-22: a chat is almost always open; it only counts when it
         runs something heavy, like tests; same rule day and night).
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

Nothing here reads content: only idle time, session counts, file modification times and temperatures.
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
IDLE_S = 5 * 60                    # keyboard / mouse
AWAY_S = 10 * 60                   # Auto stays Gentle this long after the last sign of use
SSH_IDLE_S = 30 * 60
CHAT_S = 10 * 60                   # a Claude / Codex chat file written this recently = in use (only while BUSY_PCT)
BUSY_PCT = 150.0                   # other apps' CPU (100 = one core) above this = the Mac is busy
NIGHT_START, NIGHT_END = 1, 7      # Night only: 1 AM to 7 AM local
HOT_C, COOL_C = 95.0, 80.0
WARM_C = 90.0                      # step down to half the cores first
HOT_CHECK_S = 5 * 60               # two hot checks this far apart pause
COOL_FOR_S = 5 * 60
HEAT_MAX_PAUSE_S = 2 * 3600
WATCHER_LOG = Path.home() / "Library/Application Support/m14-health"
TEMP_TOOL = "/usr/local/bin/osx-cpu-temp"
HOME = Path.home()

_state = {"threads": FULL, "last_use": 0.0, "why": "", "hot_since": None, "hot_seen": None, "cool_since": None,
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

def parse_hid_idle(text: str) -> float | None:
    m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', text or "")
    return int(m.group(1)) / 1e9 if m else None


def parse_who(text: str) -> int:
    """SSH sessions typed in within 30 min. `who -u` lines: user tty month day time idle pid (host). Local terminals
    have no (host); idle is '.' (this minute), 'old' (a day+) or hh:mm."""
    n = 0
    for line in (text or "").splitlines():
        if "(" not in line or "console" in line:
            continue
        parts = line.split()
        idle = parts[5] if len(parts) > 5 else "old"
        if idle == ".":
            n += 1
        elif re.fullmatch(r"\d+:\d\d", idle):
            h, mi = idle.split(":")
            n += (int(h) * 3600 + int(mi) * 60) < SSH_IDLE_S
    return n


def parse_remote(ps_text: str, netstat_text: str) -> list[str]:
    out = []
    if re.search(r"RustDesk\S*\s+--cm\b", ps_text or ""):   # RustDesk opens its connection window per session
        out.append("RustDesk")
    if re.search(r"\.5900\s+\S+\s+ESTABLISHED", netstat_text or ""):
        out.append("Screen Sharing")
    return out


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


def _recent_file(root: Path, within_s: float, now: float, depth: int = 3) -> bool:
    """Any file under root modified within the window (only names and times are looked at, never contents)."""
    if not root.is_dir():
        return False
    stack = [(root, 0)]
    while stack:
        d, lv = stack.pop()
        try:
            for e in os.scandir(d):
                if e.is_dir(follow_symlinks=False):
                    if lv < depth:
                        stack.append((Path(e.path), lv + 1))
                elif e.name.endswith(".jsonl") and now - e.stat().st_mtime < within_s:
                    return True
        except OSError:
            continue
    return False


def _run(args: list[str], run=subprocess.run) -> str:
    try:
        return run(args, capture_output=True, text=True, timeout=10).stdout or ""
    except Exception:
        return ""


def in_use(run=subprocess.run, now: float | None = None, home: Path = HOME) -> str:
    """'' if nobody seems to be using the Mac, else a short reason ('keyboard or mouse', 'SSH', 'a busy Claude chat', ...)."""
    now = now or time.time()
    idle = parse_hid_idle(_run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], run))
    if idle is not None and idle < IDLE_S:
        return "keyboard or mouse"
    if parse_who(_run(["who", "-u"], run)):
        return "SSH"
    remote = parse_remote(_run(["ps", "-axo", "command"], run), _run(["netstat", "-an", "-p", "tcp"], run))
    if remote:
        return remote[0]
    chat = ("a busy Claude chat" if _recent_file(home / ".claude/projects", CHAT_S, now) else
            "a busy Codex chat" if _recent_file(home / ".codex/sessions", CHAT_S, now, depth=4) else "")
    if chat and parse_busy(_run(["ps", "-axo", "nice=,pcpu=,comm="], run)) >= BUSY_PCT:
        return chat
    return ""


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
    return HALF if now - _state["last_use"] < AWAY_S else FULL


def update(conn: sqlite3.Connection, run=subprocess.run, notify=None, now: float | None = None) -> dict:
    """Called about once a minute by the worker's background thread: heat check + how many cores. Saves a one-line
    'processing_pace' for the Processing page ("Full speed" / "Gentle: keyboard or mouse in use" / ...)."""
    now = now or time.time()
    change = heat_step(conn, hottest_core(run, now), now, notify)
    reason = in_use(run, now) if mode(conn) == "auto" else ""
    t = threads_for(conn, reason, now)
    _state["threads"] = t
    if heat_paused(conn):
        label = "Paused: the Mac is hot"
    elif _get(conn, "heat_warm") == "1":
        label = "Half speed: the Mac is warm"
    elif mode(conn) == "night" and not night_window():
        label = "Night only: waits until 1 AM"
    elif t == HALF:
        why = _state["why"] if mode(conn) == "auto" else ""
        label = f"Gentle (half the cores){': ' + why + ' in use' if why else ''}"
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
