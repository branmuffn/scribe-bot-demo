"""M14 health (Status page): CPU, memory, temperature, fans, heat throttling, battery, disk SMART, Wi-Fi, uptime.

Numbers only, never content. Every reading comes from a built-in macOS command (no sudo) except temperature/fans,
which use Homebrew's osx-cpu-temp if it's installed. Each metric is a pure parse_* function (tested with canned text)
plus a small runner call with a timeout; nothing here raises: a missing tool or odd output gives None.

The worker stores one sample every 5 minutes (machine_sample table, 30 days kept) for the 24-hour history, the
Wi-Fi reading (system_profiler is slow, so it's never run on page load), alerts, and needs_attention().
"""
from __future__ import annotations

import math
import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scribe_bot.core import health
from scribe_bot.core.health import Runner
from scribe_bot.discord_bot import notify

TEMP_TOOL = "/usr/local/bin/osx-cpu-temp"
SAMPLE_EVERY = timedelta(minutes=5)
SAMPLE_SLACK = timedelta(seconds=20)       # the worker ticks every 30 s; don't skip a sample by a few seconds
KEEP_DAYS = 30
STALE_AFTER = timedelta(minutes=20)        # needs_attention ignores a sample older than this (worker down)

# Good / Watch / Problem ranges, in one place: the Status page colors, its legend, needs_attention and the alerts all use them.
# key -> (name, unit, Watch at, Problem at, higher is worse, why it matters). Tuned for the 2014 MacBook Pro.
RANGES = {
    "cpu_pct": ("CPU use", "%", 70, 90, True, "Busy for a while during transcription is normal; busy all the time is not."),
    "load": ("Load per thread", "", 0.7, 1.0, True, "Work waiting for the 8 threads. Over 1.0 means jobs are queuing up."),
    "temp_c": ("CPU temperature", " °C", 80, 90, True, "This chip slows itself down near 100 °C."),
    "speed_limit": ("Speed (heat)", "%", 99, 79, False, "100% means full speed; lower means macOS is slowing it to cool down."),
    "mem_free_pct": ("Memory free (pressure)", "%", 49, 19, False, "macOS's own memory measure. Low means apps are fighting for RAM."),
    "swap_mb": ("Swap", " MB", 1024, 2048, True, "Memory spilled onto the disk. A lot of it slows everything down."),
    "battery_pct": ("Battery health", "% of new", 79, 59, False, "The battery is a backup if the charger is bumped."),
    "disk_used_pct": ("Disk used", "%", 75, 85, True, "Recordings need room; alerts start at 85%."),
    "wifi_dbm": ("Wi-Fi signal", " dBm", -68, -76, False, "Closer to 0 is stronger. Weak signal causes dropped connections."),
}
LEVEL_WORDS = {"ok": "Good", "warn": "Watch", "bad": "Problem", "unknown": "Unknown"}
TEMP_NORMAL_C, TEMP_HOT_C = RANGES["temp_c"][2], RANGES["temp_c"][3]
SWAP_HEAVY_MB = RANGES["swap_mb"][3]
MIN_FAN_RPM = 500                          # below this the reading is mis-decoded (seen on the M14: "2 RPM")
FIELDS = ("cpu_pct", "load1", "mem_used_pct", "mem_free_pct", "swap_mb", "temp_c", "fan_rpm", "speed_limit",
          "wifi_dbm", "on_battery", "disk_used_pct", "smart_ok")


# ---------- parsers (pure) ----------

def _num(pattern: str, text: str, flags: int = 0) -> float | None:
    m = re.search(pattern, text or "", flags)
    try:
        return float(m.group(1)) if m else None
    except ValueError:
        return None


def parse_top_cpu(text: str) -> float | None:
    """`top -l 2 -n 0 -s 1`: the 2nd 'CPU usage' line (the 1st is since boot). Returns % busy (100 - idle)."""
    idle = re.findall(r"CPU usage:.*?([\d.]+)% idle", text or "")
    if not idle:
        return None
    try:
        return round(max(0.0, 100.0 - float(idle[-1])), 1)
    except ValueError:
        return None


def parse_loadavg(text: str) -> float | None:
    """`sysctl -n vm.loadavg` -> '{ 3.30 3.14 3.32 }' -> 1-minute load."""
    return _num(r"\{?\s*([\d.]+)\s+[\d.]+\s+[\d.]+", text)


def parse_int(text: str) -> int | None:
    v = _num(r"^\s*(\d+)\s*$", text, re.M)
    return int(v) if v is not None else None


def parse_vm_stat(text: str, total_bytes: int | None) -> dict | None:
    """`vm_stat` + hw.memsize -> used ≈ active + wired + compressed pages."""
    page = _num(r"page size of (\d+) bytes", text)
    if not page or not total_bytes:
        return None
    pages = 0.0
    found = False
    for label in ("Pages active", "Pages wired down", "Pages occupied by compressor"):
        v = _num(rf"{label}:\s+(\d+)", text)
        if v is not None:
            pages += v
            found = True
    if not found:
        return None
    used = pages * page
    return {"used_gb": round(used / 2**30, 1), "total_gb": round(total_bytes / 2**30, 1),
            "used_pct": round(min(100.0, 100 * used / total_bytes), 1)}


def level(key: str, value: float | None) -> str:
    """'ok' | 'warn' | 'bad' | 'unknown' for one reading, from RANGES."""
    if value is None:
        return "unknown"
    _, _, watch, problem, higher_worse, _ = RANGES[key]
    if higher_worse:
        return "bad" if value >= problem else "warn" if value >= watch else "ok"
    return "bad" if value <= problem else "warn" if value <= watch else "ok"


def ranges_text(key: str) -> str:
    """'Good under 80 °C · Watch 80–89 · Problem 90 or more' (plain words for the legend and hover)."""
    _, unit, watch, problem, higher_worse, _ = RANGES[key]
    f = (lambda v: f"{v:g}{unit}")
    if higher_worse:
        return f"Good under {f(watch)} · Watch {f(watch)}–{f(problem)} · Problem {f(problem)} or more"
    return f"Good above {f(watch)} · Watch {f(problem)}–{f(watch)} · Problem {f(problem)} or less"


def pressure_level(free_pct: float | None) -> str:
    return {"ok": "green", "warn": "yellow", "bad": "red", "unknown": "unknown"}[level("mem_free_pct", free_pct)]


def parse_memory_pressure(text: str) -> float | None:
    """`memory_pressure -Q` -> 'System-wide memory free percentage: 82%' -> 82."""
    return _num(r"memory free percentage:\s*(\d+)%", text)


def parse_swap(text: str) -> float | None:
    """`sysctl -n vm.swapusage` -> 'total = 2048.00M  used = 512.25M ...' -> MB used."""
    m = re.search(r"used\s*=\s*([\d.]+)([MG])", text or "")
    if not m:
        return None
    return round(float(m.group(1)) * (1024 if m.group(2) == "G" else 1), 1)


def parse_temp(text: str) -> float | None:
    """`osx-cpu-temp -c -f`: '61.8°C' or 'CPU: 51.5°C'. 0.0 means the sensor isn't supported."""
    text = text or ""
    v = _num(r"CPU:\s*(-?[\d.]+)\s*°?C", text)
    if v is None:
        v = _num(r"^\s*(-?[\d.]+)\s*°C", text, re.M)
    return v if v is not None and v > 0 else None


def parse_fans(text: str) -> list[int]:
    """Fan lines 'Fan 0 - Left side   at 2161 RPM (35%)' -> [2161, ...]; readings under 500 RPM are mis-decoded."""
    rpms = [int(r) for r in re.findall(r"Fan \d+.*?at\s+(\d+)\s*RPM", text or "")]
    return [r for r in rpms if r >= MIN_FAN_RPM]


def parse_therm(text: str) -> int | None:
    """`pmset -g therm` -> CPU_Speed_Limit (100 = full speed). No limit recorded but no warning either = 100."""
    v = _num(r"CPU_Speed_Limit\s*=\s*(\d+)", text)
    if v is not None:
        return int(v)
    return 100 if "No thermal warning level has been recorded" in (text or "") else None


def parse_battery(text: str) -> dict | None:
    """`ioreg -rn AppleSmartBattery` -> {"cycles", "health_pct", "failed"}.
    Intel Macs report MaxCapacity in mAh; Apple silicon reports it as a % (then AppleRawMaxCapacity is the mAh)."""
    def key(name):
        v = _num(rf'^\s*"{name}"\s*=\s*(\d+)', text, re.M)
        return int(v) if v is not None else None
    cycles, design = key("CycleCount"), key("DesignCapacity")
    raw, maxcap, failure = key("AppleRawMaxCapacity"), key("MaxCapacity"), key("PermanentFailureStatus")
    if cycles is None and design is None:
        return None
    full = raw if raw else (maxcap if maxcap and maxcap > 100 else None)
    if full and design:
        hp = round(100 * full / design)
    elif maxcap is not None and maxcap <= 100:
        hp = maxcap
    else:
        hp = None
    return {"cycles": cycles, "health_pct": hp, "failed": bool(failure)}


def parse_smart(text: str) -> str | None:
    """`diskutil info disk0` -> 'Verified', 'Failing', 'Not Supported', ..."""
    m = re.search(r"SMART Status:\s*(.+)", text or "")
    return m.group(1).strip() if m else None


def parse_boottime(text: str, now: float) -> int | None:
    """`sysctl -n kern.boottime` -> '{ sec = 1789190058, usec = 936705 } ...' -> seconds since boot."""
    sec = _num(r"sec\s*=\s*(\d+)", text)
    return int(now - sec) if sec and sec <= now else None


def parse_wifi(text: str) -> int | None:
    """`system_profiler SPAirPortDataType`: signal (dBm) of the current network only, not the nearby list."""
    text = text or ""
    start = text.find("Current Network Information:")
    if start < 0:
        return None
    end = text.find("Other Local Wi-Fi Networks:", start)
    v = _num(r"Signal / Noise:\s*(-?\d+)\s*dBm", text[start:end if end > 0 else None])
    return int(v) if v is not None else None


def parse_procs(text: str) -> dict:
    """`ps -Ao pid,pcpu,rss,command` -> Scribe Bot's worker and web (the python processes launchd's uv starts)."""
    out = {}
    for name in ("worker", "web"):
        cpu = rss = 0.0
        n = 0
        for line in (text or "").splitlines():
            parts = line.split(None, 3)
            if len(parts) < 4 or "uv run" in parts[3] or not re.search(rf"-m scribe_bot\.{name}\b", parts[3]):
                continue
            try:
                cpu, rss, n = cpu + float(parts[1]), rss + float(parts[2]), n + 1
            except ValueError:
                continue
        out[name] = {"running": n > 0, "cpu_pct": round(cpu, 1), "mem_mb": round(rss / 1024)} if n else {"running": False}
    return out


# ---------- runners (never raise) ----------

def _out(run: Runner, args: list[str], timeout: float = 10) -> str | None:
    try:
        p = run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if isinstance(getattr(p, "stdout", None), str) else None


_HOST_PORT: list[int] = []                           # mach_host_self(), asked once per process
_LAST_TICKS: dict[str, tuple[float, tuple[int, int]]] = {}   # "sample": (time, ticks) at the previous 5-minute sample


def cpu_ticks() -> tuple[int, int] | None:
    """(busy, total) CPU ticks since boot over all cores, straight from the kernel (mach host_statistics, the counters
    top and Activity Monitor use). None where that isn't available (Linux CI)."""
    import ctypes
    try:
        lib = ctypes.CDLL("/usr/lib/libSystem.dylib")
        lib.mach_host_self.restype = ctypes.c_uint
        if not _HOST_PORT:
            _HOST_PORT.append(lib.mach_host_self())
        ticks, count = (ctypes.c_uint * 4)(), ctypes.c_uint(4)   # user, system, idle, nice
        if lib.host_statistics(_HOST_PORT[0], 3, ticks, ctypes.byref(count)) != 0:   # 3 = HOST_CPU_LOAD_INFO
            return None
    except (OSError, AttributeError):
        return None
    user, system, idle, nice = ticks
    return user + system + nice, user + system + idle + nice


def average_cpu(prev: tuple[int, int] | None, cur: tuple[int, int] | None) -> float | None:
    """% busy between two cpu_ticks() readings (each counter is 32-bit and wraps; deltas mod 2^32 stay right)."""
    if not prev or not cur:
        return None
    busy, total = (cur[0] - prev[0]) % 2**32, (cur[1] - prev[1]) % 2**32
    return round(100 * busy / total, 1) if total else None


def cpu(run: Runner = subprocess.run) -> dict:
    return {"cpu_pct": parse_top_cpu(_out(run, ["top", "-l", "2", "-n", "0", "-s", "1"]) or ""),
            "load1": parse_loadavg(_out(run, ["sysctl", "-n", "vm.loadavg"]) or ""),
            "cpus": parse_int(_out(run, ["sysctl", "-n", "hw.logicalcpu"]) or "")}


def memory(run: Runner = subprocess.run) -> dict:
    total = parse_int(_out(run, ["sysctl", "-n", "hw.memsize"]) or "")
    vm = parse_vm_stat(_out(run, ["vm_stat"]) or "", total) or {}
    free = parse_memory_pressure(_out(run, ["memory_pressure", "-Q"]) or "")
    return {"mem_used_pct": vm.get("used_pct"), "mem_used_gb": vm.get("used_gb"), "mem_total_gb": vm.get("total_gb"),
            "mem_free_pct": free, "pressure": pressure_level(free),
            "swap_mb": parse_swap(_out(run, ["sysctl", "-n", "vm.swapusage"]) or "")}


def thermal(run: Runner = subprocess.run) -> dict:
    out = _out(run, [TEMP_TOOL, "-c", "-f"])
    fans = parse_fans(out or "")
    return {"temp_tool": out is not None, "temp_c": parse_temp(out or ""), "fan_rpm": max(fans) if fans else None,
            "fans": fans, "speed_limit": parse_therm(_out(run, ["pmset", "-g", "therm"]) or "")}


def wifi(run: Runner = subprocess.run) -> int | None:
    return parse_wifi(_out(run, ["system_profiler", "SPAirPortDataType"], timeout=30) or "")


def fast(run: Runner = subprocess.run, now: float | None = None) -> dict:
    """Everything except Wi-Fi (about 1 s, mostly top's 1-second CPU sample)."""
    smart = parse_smart(_out(run, ["diskutil", "info", "disk0"]) or "")
    return {**cpu(run), **memory(run), **thermal(run),
            "battery": parse_battery(_out(run, ["ioreg", "-rn", "AppleSmartBattery"]) or ""),
            "smart": smart, "smart_ok": None if smart is None else smart == "Verified",
            "uptime_s": parse_boottime(_out(run, ["sysctl", "-n", "kern.boottime"]) or "", now or time.time()),
            "procs": parse_procs(_out(run, ["ps", "-Ao", "pid,pcpu,rss,command"]) or ""),
            "disk_used_pct": _disk_pct()}


def _disk_pct() -> float | None:
    try:
        u = shutil.disk_usage(Path.home())
        return round(u.used / u.total * 100, 1)
    except OSError:
        return None


# ---------- samples, alerts, needs_attention ----------

def latest(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT * FROM machine_sample ORDER BY ts DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def history(conn: sqlite3.Connection, now: datetime | None = None, hours: int = 24) -> list[dict]:
    since = ((now or datetime.now(timezone.utc)) - timedelta(hours=hours)).isoformat(timespec="seconds")
    return [dict(r) for r in conn.execute("SELECT * FROM machine_sample WHERE ts >= ? ORDER BY ts", (since,))]


def sample(conn: sqlite3.Connection, data_dir: Path, run: Runner = subprocess.run,
           now: datetime | None = None, ticks=None) -> dict:
    """Take one sample, store it, prune rows older than 30 days, send any alerts. Returns the stored row.

    CPU is the average since the previous sample (the kernel's tick counters), not top's 1-second snapshot, so the
    chart shows how busy each 5 minutes really was. The first sample after a restart falls back to the snapshot."""
    now = now or datetime.now(timezone.utc)
    f = fast(run, now.timestamp())
    t, prev = (ticks or cpu_ticks)(), _LAST_TICKS.get("sample")
    avg = average_cpu(prev[1], t) if prev and 0 < now.timestamp() - prev[0] <= 3 * SAMPLE_EVERY.total_seconds() else None
    if t:
        _LAST_TICKS["sample"] = (now.timestamp(), t)
    if avg is not None:
        f["cpu_pct"] = avg
    try:
        disk_pct = health.disk(data_dir)["used_pct"]
    except OSError:
        disk_pct = None
    src = health.power(run)["source"]
    row = {k: f.get(k) for k in FIELDS}
    row.update(ts=now.isoformat(timespec="seconds"), wifi_dbm=wifi(run), disk_used_pct=disk_pct,
               on_battery=None if src == "unknown" else int(src == "battery"),
               smart_ok=None if f["smart_ok"] is None else int(f["smart_ok"]))
    cols = ("ts",) + FIELDS
    conn.execute(f"INSERT INTO machine_sample ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 [row[c] for c in cols])
    conn.execute("DELETE FROM machine_sample WHERE ts < ?",
                 ((now - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds"),))
    alerts(conn)
    return row


def maybe_sample(conn: sqlite3.Connection, data_dir: Path, run: Runner = subprocess.run,
                 now: datetime | None = None) -> dict | None:
    """Called every worker tick; samples when the last one is 5+ minutes old."""
    now = now or datetime.now(timezone.utc)
    last = latest(conn)
    if last and now - datetime.fromisoformat(last["ts"]) < SAMPLE_EVERY - SAMPLE_SLACK:
        return None
    return sample(conn, data_dir, run, now)


def _hot(r): return r["temp_c"] is not None and r["temp_c"] >= TEMP_HOT_C
def _throttled(r): return r["speed_limit"] is not None and r["speed_limit"] < 100
def _swap_heavy(r): return r["swap_mb"] is not None and r["swap_mb"] > SWAP_HEAVY_MB
def _pressure_red(r): return pressure_level(r["mem_free_pct"]) == "red"
def _smart_bad(r): return r["smart_ok"] == 0


# name -> (test, consecutive samples needed, alert title, all-clear title). Titles only, never content.
ALERTS = {
    "hot": (_hot, 2, "🔥 M14 CPU is very hot (90 °C or more). Check the vents and fans.", "✅ M14 CPU temperature is back to normal."),
    "throttle": (_throttled, 2, "⚠️ M14 is slowing itself down because of heat.", "✅ M14 is running at full speed again."),
    "swap": (_swap_heavy, 1, "⚠️ M14 is short on memory (over 2 GB swap in use).", "✅ M14 memory use is back to normal."),
    "pressure": (_pressure_red, 1, "🔴 M14 memory pressure is high.", "✅ M14 memory pressure is back to normal."),
    "smart": (_smart_bad, 1, "🔴 M14 disk health check (SMART) is not Verified. Back up and plan a replacement.",
              "✅ M14 disk health check (SMART) is Verified again."),
}


def alerts(conn: sqlite3.Connection) -> list[str]:
    """Alert once per problem (setting 'machine_alert:<name>'), and once when it clears. Returns names newly alerted."""
    rows = conn.execute("SELECT * FROM machine_sample ORDER BY ts DESC LIMIT 2").fetchall()
    fired = []
    for name, (test, need, title, clear) in ALERTS.items():
        key = f"machine_alert:{name}"
        on = bool(conn.execute("SELECT 1 FROM setting WHERE key = ?", (key,)).fetchone())
        bad = len(rows) >= need and all(test(r) for r in rows[:need])
        if bad and not on:
            conn.execute("INSERT INTO setting (key, value) VALUES (?, '1')", (key,))
            notify.enqueue(conn, "alerts", title, "/status")
            fired.append(name)
        elif on and rows and not test(rows[0]):
            conn.execute("DELETE FROM setting WHERE key = ?", (key,))
            notify.enqueue(conn, "alerts", clear, "/status")
    return fired


def needs_attention(conn: sqlite3.Connection, now: datetime | None = None) -> list[str]:
    """Short reasons the M14 needs Brian, from the latest sample (a DB read, no commands; safe on every page).
    Empty when all is well or there's no recent sample."""
    r = latest(conn)
    now = now or datetime.now(timezone.utc)
    if not r or now - datetime.fromisoformat(r["ts"]) > STALE_AFTER:
        return []
    out = []
    if _hot(r):
        out.append("hot")
    if _throttled(r):
        out.append("throttling")
    if r["on_battery"]:
        out.append("on battery")
    if r["disk_used_pct"] is not None and r["disk_used_pct"] > health.DISK_ALERT_PCT:
        out.append("disk nearly full")
    if _swap_heavy(r):
        out.append("swap heavy")
    if _pressure_red(r):
        out.append("memory pressure high")
    if _smart_bad(r):
        out.append("SMART failing")
    return out


# ---------- page helpers ----------

def card(data: dict | None) -> dict:
    """Same shape whether from a live reading or a stored sample (missing keys -> None)."""
    d = {k: None for k in (*FIELDS, "cpus", "mem_used_gb", "mem_total_gb", "fans", "battery", "smart", "uptime_s",
                           "procs", "temp_tool")}
    d.update(data or {})
    d["pressure"] = pressure_level(d["mem_free_pct"])
    b = d["battery"] or {}
    d["cpus"] = d["cpus"] or os.cpu_count()   # the web app runs on the M14, so this is the M14's thread count
    load = d["load1"] / d["cpus"] if d["load1"] is not None and d["cpus"] else None
    lv = {k: level(k, v) for k, v in (("cpu_pct", d["cpu_pct"]), ("load", load), ("temp_c", d["temp_c"]),
                                      ("speed_limit", d["speed_limit"]), ("mem_free_pct", d["mem_free_pct"]),
                                      ("swap_mb", d["swap_mb"]), ("battery_pct", b.get("health_pct")),
                                      ("disk_used_pct", d["disk_used_pct"]), ("wifi_dbm", d["wifi_dbm"]))}
    if b.get("failed"):
        lv["battery_pct"] = "bad"
    lv["smart"] = "unknown" if d["smart_ok"] is None and not d["smart"] else "ok" if d["smart_ok"] in (True, 1) else "bad"
    d["levels"] = lv
    known = [v for v in lv.values() if v != "unknown"]
    d["overall"] = "bad" if "bad" in known else "warn" if "warn" in known else "ok" if known else "unknown"
    return d


def uptime_label(seconds: int | None) -> str:
    if seconds is None:
        return "unknown"
    d, rem = divmod(seconds // 60, 1440)
    h, m = divmod(rem, 60)
    return f"{d} d {h} h" if d else f"{h} h {m} min"


def sparkline(values: list[float | None], width: int = 240, height: int = 36,
              lo: float | None = None, hi: float | None = None) -> str:
    """SVG path 'd' for a small line chart; gaps (None) break the line. Empty string if fewer than 2 points."""
    pts = [(i, v) for i, v in enumerate(values) if v is not None]
    if len(pts) < 2:
        return ""
    vs = [v for _, v in pts]
    lo = min(vs) if lo is None else min(lo, min(vs))
    hi = max(vs) if hi is None else max(hi, max(vs))
    span, n = (hi - lo) or 1, max(len(values) - 1, 1)
    d, prev = [], None
    for i, v in pts:
        x, y = width * i / n, height - 2 - (height - 4) * (v - lo) / span
        d.append(f"{'L' if prev is not None and i == prev + 1 else 'M'}{x:.1f},{y:.1f}")
        prev = i
    return " ".join(d)


# ---------- Status page: tiles and history charts ----------

def tiles(d: dict) -> list[dict]:
    """One tile per reading that has a value; readings this Mac can't give are left out, not shown as 'unknown'."""
    L, out = d["levels"], []

    def add(key, name, value, level, tip):
        if value is not None:
            out.append({"key": key, "name": name, "value": value, "level": level,
                        "tip": tip + (" " + ranges_text(key) + "." if key in RANGES else "")})
    add("cpu_pct", "CPU", d["cpu_pct"] is not None and f"{d['cpu_pct']:.0f}% busy" or None, L["cpu_pct"],
        "How hard the processor is working. Near 100% while transcribing is normal.")
    add("load", "Load", d["load1"] is not None and f"{d['load1']:.1f}" + (f" of {d['cpus']}" if d["cpus"] else "") or None,
        L["load"], "Work waiting for the processor's threads.")
    add("temp_c", "Temperature", d["temp_c"] is not None and f"{d['temp_c']:.0f} °C" or None, L["temp_c"], "How hot the processor is.")
    add("fans", "Fans", d["fan_rpm"] and f"{d['fan_rpm']:,} RPM" or None, "ok", "Fans speed up when the Mac is busy or warm.")
    add("speed_limit", "Heat slowdown", d["speed_limit"] is not None and
        ("None" if d["speed_limit"] >= 100 else f"Running at {d['speed_limit']}%") or None, L["speed_limit"],
        "macOS slows the processor down if it gets too hot.")
    add("mem_free_pct", "Memory", d["mem_free_pct"] is not None and f"{d['mem_free_pct']:.0f}% free" or None, L["mem_free_pct"],
        "Memory pressure, macOS's own measure (macOS fills spare RAM with cache on purpose, so GB used isn't a worry)."
        + (f" In use: {d['mem_used_gb']} of {d['mem_total_gb']} GB." if d["mem_used_gb"] is not None else ""))
    add("swap_mb", "Swap", d["swap_mb"] is not None and f"{d['swap_mb']:,.0f} MB" or None, L["swap_mb"],
        "Memory spilled onto the disk.")
    b = d["battery"] or {}
    add("battery_pct", "Battery", b.get("health_pct") is not None and f"{b['health_pct']}% health" or None, L["battery_pct"],
        "How much charge the battery holds compared with new." + (f" {b['cycles']} charge cycles." if b.get("cycles") else ""))
    add("disk_used_pct", "Disk space", d["disk_used_pct"] is not None and f"{d['disk_used_pct']:.0f}% full" or None,
        L["disk_used_pct"], "How full the M14's drive is.")
    add("smart", "Disk health", d["smart"] or None, L["smart"], "The drive's own self-check. Good = Verified, Problem = anything else.")
    add("wifi_dbm", "Wi-Fi", d["wifi_dbm"] is not None and f"{d['wifi_dbm']:.0f} dBm" or None, L["wifi_dbm"],
        "Signal strength, checked every 5 minutes.")
    return out


# key, title, unit, y-axis low, y-axis high (None = fit the data)
CHARTS = (("cpu_pct", "CPU use", "%", 0, 100), ("temp_c", "Temperature", " °C", 30, 100),
          ("mem_free_pct", "Memory free", "%", 0, 100), ("swap_mb", "Swap", " MB", 0, None),
          ("wifi_dbm", "Wi-Fi signal", " dBm", -90, -10))
# Drawn at about a phone card's width, so labels stay ~11 px on the phone and on the 2-column desktop grid alike
# (at 600 wide the phone shrank the whole chart, labels included, to about half size).
CHART_W, CHART_H, PAD_L, PAD_B = 340, 150, 34, 18


def nice_ticks(lo: float, hi: float, steps: int = 4) -> list[float]:
    """Round-number gridline values from lo to hi, about `steps` apart: 0/25/50/75/100 %, 40/60/80/100 °C."""
    raw = (hi - lo) / steps
    if raw <= 0:
        return [lo]
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    first = math.ceil(lo / step - 1e-9) * step
    return [first + k * step for k in range(int((hi - first) / step + 1e-9) + 1)]


def time_grid(start: datetime, hours: int) -> list[tuple[datetime, str]]:
    """Vertical gridlines on the local clock: every 3 hours over a day (labelled 00:00, 06:00, ...), each midnight over a
    week (Mon, Tue, ...), each Monday over a month (Oct 5, ...). Label '' = a line without a label."""
    s, end = start.astimezone(), (start + timedelta(hours=hours)).astimezone()
    if hours <= 24:
        top = s.replace(minute=0, second=0, microsecond=0)
        times = [t for t in (top + timedelta(hours=h) for h in range(1, hours + 1)) if t.hour % 3 == 0 and t < end]
        return [(t, t.strftime("%H:%M") if t.hour % 6 == 0 else "") for t in times]
    days = (datetime.combine(s.date() + timedelta(days=d), datetime.min.time()).astimezone() for d in range(1, hours // 24 + 1))
    days = [t for t in days if s < t < end]
    if hours <= 24 * 7:
        return [(t, t.strftime("%a")) for t in days]
    return [(t, t.strftime("%b %-d")) for t in days if t.weekday() == 0]


def chart(rows: list[dict], key: str, title: str, unit: str, lo: float, hi: float | None, hours: int,
          now: datetime | None = None) -> dict | None:
    """A server-drawn line chart over `hours`: samples averaged into ~200 buckets, gaps break the line,
    dashed Watch/Problem lines from RANGES. None when there's no data for this reading."""
    now = now or datetime.now(timezone.utc)
    start, span = now - timedelta(hours=hours), hours * 3600
    bucket = span / 200
    sums: dict[int, list[float]] = {}
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        t = (datetime.fromisoformat(r["ts"]) - start).total_seconds()
        if 0 <= t <= span:
            sums.setdefault(int(t // bucket), []).append(v)
    if not sums:
        return None
    last = max((r for r in rows if r.get(key) is not None), key=lambda r: r["ts"])   # the latest reading itself
    pts = sorted((i, sum(v) / len(v)) for i, v in sums.items())
    vals = [v for _, v in pts]
    if hi is None:   # fit the data, but keep the Problem line in view
        hi = max(max(vals) * 1.1, RANGES[key][3] * 1.1 if key in RANGES else 1)
    lo = min(lo, min(vals))
    hi = max(hi, max(vals))
    w, h = CHART_W - PAD_L, CHART_H - PAD_B
    X = lambda i: PAD_L + w * (i + 0.5) / 200
    Y = lambda v: 14 + (h - 18) * (1 - (v - lo) / ((hi - lo) or 1))   # room above the top label
    d, prev = [], None
    for i, v in pts:
        d.append(f"{'L' if prev is not None and i - prev <= 3 else 'M'}{X(i):.1f},{Y(v):.1f}")
        prev = i
    local = lambda i: (start + timedelta(seconds=bucket * (i + 0.5))).astimezone()
    rng = RANGES.get(key)
    lines = [] if not rng else [(Y(t), lvl) for t, lvl in ((rng[2], "warn"), (rng[3], "bad")) if lo < t < hi]
    grid = [(PAD_L + w * (t - start).total_seconds() / span, label) for t, label in time_grid(start, hours)]
    return {"key": key, "title": title, "path": " ".join(d), "w": CHART_W, "h": CHART_H, "pad_l": PAD_L, "base": h,
            "top": Y(hi), "bottom": Y(lo), "y_ticks": [(Y(v), f"{v:.0f}") for v in nice_ticks(lo, hi)],
            "x_grid": [round(x, 1) for x, _ in grid],
            "x_ticks": [(round(x, 1), label) for x, label in grid if label and PAD_L + 14 < x < CHART_W - 14],
            "thresholds": lines,
            "points": [[round(X(i), 1), round(Y(v), 1), local(i).strftime("%a %H:%M"), f"{v:.0f}{unit}"] for i, v in pts],
            "now": f"{last[key]:.0f}{unit}", "now_at": datetime.fromisoformat(last["ts"]).astimezone().strftime("%-I:%M %p").lower(),
            "low": f"{min(vals):.0f}{unit}", "avg": f"{sum(vals) / len(vals):.0f}{unit}",
            "high": f"{max(vals):.0f}{unit}"}
