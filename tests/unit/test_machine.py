"""M14 health: parsers with canned (synthetic) command output, sampler, alerts, needs_attention, Status page.
No real commands run: every test passes a fake runner, and the disk reading is pinned."""
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from scribe_bot.core import db, machine
from scribe_bot.core.settings import Settings
from scribe_bot.web.app import create_app

TOP = ("Processes: 400 total\nLoad Avg: 3.30, 3.14, 3.32\nCPU usage: 20.0% user, 10.0% sys, 70.00% idle\n"
       "Processes: 400 total\nLoad Avg: 3.30, 3.14, 3.32\nCPU usage: 6.10% user, 2.72% sys, 91.16% idle\n")
VM_STAT = ("Mach Virtual Memory Statistics: (page size of 4096 bytes)\nPages free:      224812.\n"
           "Pages active:     1000000.\nPages inactive:   500000.\nPages wired down:  472864.\n"
           "Pages occupied by compressor:  100000.\n")
MEM = str(16 * 2**30)
TEMP = "CPU: 51.5°C\nGPU: 50.5°C\nNum fans: 2\nFan 0 - Left side   at 2161 RPM (35%)\nFan 1 - Right side  at 2001 RPM (34%)\n"
TEMP_BAD_FANS = "CPU: 51.5°C\nNum fans: 2\nFan 0 - Left side   at 2 RPM (0%)\nFan 1 - Right side  at 0 RPM (0%)\n"
THERM = ("Note: No thermal warning level has been recorded\n2026-10-19 22:36:33 -0400 CPU Power notify\n"
         "\tCPU_Scheduler_Limit \t= 100\n\tCPU_Available_CPUs \t= 8\n\tCPU_Speed_Limit \t= 100\n")
IOREG = ('+-o AppleSmartBattery\n    {\n      "AppleRawMaxCapacity" = 8599\n      "PermanentFailureStatus" = 0\n'
         '      "MaxCapacity" = 8599\n      "CycleCount" = 371\n      "DesignCapacity" = 8880\n    }\n')
WIFI = ("Wi-Fi:\n  Interfaces:\n    en0:\n      Current Network Information:\n        HomeNet:\n"
        "          Signal / Noise: -55 dBm / -90 dBm\n      Other Local Wi-Fi Networks:\n        Next:\n"
        "          Signal / Noise: -20 dBm / 0 dBm\n")
PS = ("  PID  %CPU    RSS COMMAND\n 10   0.0  22192 /u/.local/bin/uv run --project /u/scribe-bot python -m scribe_bot.worker\n"
      " 11   2.5  32768 /u/scribe-bot/.venv/bin/python -m scribe_bot.worker\n"
      " 12   0.1  65536 /u/scribe-bot/.venv/bin/python -m scribe_bot.web\n 1 1.3 22960 /sbin/launchd\n")
PMSET = "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t90%; charged; 0:00 remaining present: true\n"

OUTPUTS = {
    ("top",): TOP, ("sysctl", "-n", "vm.loadavg"): "{ 3.30 3.14 3.32 }\n", ("sysctl", "-n", "hw.logicalcpu"): "8\n",
    ("sysctl", "-n", "hw.memsize"): MEM, ("vm_stat",): VM_STAT,
    ("memory_pressure",): "The system has 17179869184 (4194304 pages with a page size of 4096).\nSystem-wide memory free percentage: 82%\n",
    ("sysctl", "-n", "vm.swapusage"): "total = 2048.00M  used = 512.25M  free = 1535.75M  (encrypted)\n",
    (machine.TEMP_TOOL,): TEMP, ("pmset", "-g", "therm"): THERM, ("pmset", "-g", "batt"): PMSET,
    ("ioreg",): IOREG, ("diskutil",): "   SMART Status:              Verified\n",
    ("sysctl", "-n", "kern.boottime"): "{ sec = 1000000, usec = 0 } Mon Jan 12 1970\n",
    ("system_profiler",): WIFI, ("ps",): PS,
}


@pytest.fixture(autouse=True)
def half_full_disk(monkeypatch):
    # The disk is read with shutil, not the runner; CI runners' disks are 75%+ full, which turned Status to Watch.
    monkeypatch.setattr(shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 50, 50))


def fake(overrides=None, missing=()):
    outs = {**OUTPUTS, **(overrides or {})}

    def run(args, **kw):
        assert kw.get("timeout"), "every command needs a timeout"
        for key, out in outs.items():
            if tuple(args[:len(key)]) == key:
                if key in missing or args[0] in missing:
                    raise FileNotFoundError(args[0])
                return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
        raise FileNotFoundError(args[0])
    return run


def garbage(args, **kw):
    return subprocess.CompletedProcess(args, 0, stdout="\x00 nonsense ### 12", stderr="")


# ---------- parsers ----------

def test_cpu_parsers():
    assert machine.parse_top_cpu(TOP) == 8.8          # the 2nd sample, not since-boot
    assert machine.parse_top_cpu("") is None
    assert machine.parse_loadavg("{ 3.30 3.14 3.32 }") == 3.3
    assert machine.parse_loadavg("junk") is None
    assert machine.parse_int("8\n") == 8 and machine.parse_int("x") is None


def test_memory_parsers():
    vm = machine.parse_vm_stat(VM_STAT, 16 * 2**30)
    assert vm == {"used_gb": 6.0, "total_gb": 16.0, "used_pct": 37.5}
    assert machine.parse_vm_stat(VM_STAT, None) is None
    assert machine.parse_vm_stat("garbage", 16 * 2**30) is None
    assert machine.parse_memory_pressure("System-wide memory free percentage: 82%") == 82
    assert machine.parse_memory_pressure("") is None
    assert [machine.pressure_level(v) for v in (82, 50, 30, 19, None)] == ["green", "green", "yellow", "red", "unknown"]
    assert machine.parse_swap("total = 2048.00M  used = 512.25M  free = 1535.75M") == 512.2
    assert machine.parse_swap("total = 3.00G  used = 2.50G  free = 0.50G") == 2560.0
    assert machine.parse_swap("??") is None


def test_temp_and_fans():
    assert machine.parse_temp("61.8°C\n") == 61.8
    assert machine.parse_temp(TEMP) == 51.5            # "CPU:" prefix, GPU line ignored
    assert machine.parse_temp("0.0°C") is None         # unsupported sensor
    assert machine.parse_temp("") is None and machine.parse_temp("garbage") is None
    assert machine.parse_fans(TEMP) == [2161, 2001]
    assert machine.parse_fans(TEMP_BAD_FANS) == []     # mis-decoded on the M14 -> unreadable
    assert machine.parse_fans("") == []


def test_therm():
    assert machine.parse_therm(THERM) == 100
    assert machine.parse_therm("CPU_Speed_Limit = 70") == 70
    assert machine.parse_therm("Note: No thermal warning level has been recorded\n") == 100
    assert machine.parse_therm("garbage") is None


def test_battery():
    assert machine.parse_battery(IOREG) == {"cycles": 371, "health_pct": 97, "failed": False}
    apple_silicon = '  "MaxCapacity" = 100\n  "DesignCapacity" = 6249\n  "CycleCount" = 343\n  "AppleRawMaxCapacity" = 5670\n'
    assert machine.parse_battery(apple_silicon)["health_pct"] == 91
    assert machine.parse_battery('  "CycleCount" = 5\n  "PermanentFailureStatus" = 1\n')["failed"] is True
    assert machine.parse_battery("") is None


def test_smart_uptime_wifi_procs():
    assert machine.parse_smart("   SMART Status:              Verified\n") == "Verified"
    assert machine.parse_smart("   SMART Status:   Failing\n") == "Failing"
    assert machine.parse_smart("") is None
    assert machine.parse_boottime("{ sec = 1000000, usec = 0 } Mon", now=1003600) == 3600
    assert machine.parse_boottime("junk", now=5) is None
    assert machine.parse_wifi(WIFI) == -55             # current network, not the nearby list
    assert machine.parse_wifi("Other Local Wi-Fi Networks:\n Signal / Noise: -20 dBm / 0 dBm") is None
    assert machine.parse_wifi("") is None
    procs = machine.parse_procs(PS)
    assert procs["worker"] == {"running": True, "cpu_pct": 2.5, "mem_mb": 32}
    assert procs["web"]["running"] and machine.parse_procs("")["web"] == {"running": False}


def test_runners_never_raise():
    f = machine.fast(fake(), now=1003600)
    assert f["cpu_pct"] == 8.8 and f["cpus"] == 8 and f["temp_c"] == 51.5 and f["fan_rpm"] == 2161
    assert f["smart_ok"] is True and f["uptime_s"] == 3600 and f["pressure"] == "green" and f["temp_tool"]
    gone = machine.fast(fake(missing=(machine.TEMP_TOOL,)))
    assert gone["temp_tool"] is False and gone["temp_c"] is None and gone["fan_rpm"] is None
    for run in (fake(missing=tuple(k[0] for k in OUTPUTS)), garbage):
        r = machine.fast(run)
        assert r["cpu_pct"] is None and r["mem_used_pct"] is None and r["smart_ok"] is None
        assert machine.wifi(run) is None

    def slow(args, **kw):
        raise subprocess.TimeoutExpired(args, kw["timeout"])
    assert machine.fast(slow)["speed_limit"] is None


# ---------- sampler, alerts, needs_attention ----------

T0 = datetime(2026, 10, 19, 12, 0, tzinfo=timezone.utc)
REAL_CPU_TICKS = machine.cpu_ticks   # tests otherwise get a stand-in (conftest)


def alerts_sent(conn):
    return [r[0] for r in conn.execute("SELECT title FROM notification WHERE channel = 'alerts' ORDER BY id")]


def test_sample_insert_throttle_and_prune(conn, tmp_path):
    conn.execute("INSERT INTO machine_sample (ts, cpu_pct) VALUES (?, 1)", ((T0 - timedelta(days=31)).isoformat(),))
    row = machine.maybe_sample(conn, tmp_path, fake(), T0)
    assert row["wifi_dbm"] == -55 and row["temp_c"] == 51.5 and row["on_battery"] == 0 and row["smart_ok"] == 1
    assert conn.execute("SELECT count(*) FROM machine_sample").fetchone()[0] == 1   # 31-day-old row pruned
    assert machine.maybe_sample(conn, tmp_path, fake(), T0 + timedelta(minutes=2)) is None
    assert machine.maybe_sample(conn, tmp_path, fake(), T0 + timedelta(minutes=5)) is not None
    assert len(machine.history(conn, T0 + timedelta(minutes=5))) == 2
    assert alerts_sent(conn) == []


def test_sample_survives_missing_tools(conn, tmp_path):
    row = machine.sample(conn, tmp_path, fake(missing=tuple(k[0] for k in OUTPUTS)), T0)
    assert row["cpu_pct"] is None and row["on_battery"] is None and row["disk_used_pct"] is not None


def test_problems_show_on_status_but_send_no_discord_alert(conn, tmp_path):
    # Brian 2026-11-19: #m14-health (the watcher) covers these; Scribe Bot no longer posts them to #scribe-alerts
    conn.execute("INSERT INTO setting (key, value) VALUES ('machine_alert:hot', '1')")   # left from the old alerts
    bad = fake({(machine.TEMP_TOOL,): "CPU: 95.0°C\n", ("pmset", "-g", "therm"): "CPU_Speed_Limit = 60\n",
                ("sysctl", "-n", "vm.swapusage"): "total = 4.00G  used = 3.00G  free = 1.00G",
                ("memory_pressure",): "System-wide memory free percentage: 10%",
                ("diskutil",): "   SMART Status:   Failing\n",
                ("pmset", "-g", "batt"): "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t80%; discharging; 3:00 remaining\n"})
    for i in range(3):
        machine.sample(conn, tmp_path, bad, T0 + timedelta(minutes=5 * i))
    assert alerts_sent(conn) == []
    assert not conn.execute("SELECT 1 FROM setting WHERE key LIKE 'machine_alert:%'").fetchone()
    assert machine.needs_attention(conn, T0 + timedelta(minutes=10)) == [
        "hot", "throttling", "on battery", "swap heavy", "memory pressure high", "SMART failing"]


def test_needs_attention_empty_or_stale(conn, tmp_path):
    assert machine.needs_attention(conn) == []
    machine.sample(conn, tmp_path, fake(), T0)
    assert machine.needs_attention(conn, T0) == []
    conn.execute("UPDATE machine_sample SET on_battery = 1, disk_used_pct = 92")
    assert machine.needs_attention(conn, T0) == ["on battery", "disk nearly full"]
    assert machine.needs_attention(conn, T0 + timedelta(hours=1)) == []   # stale sample ignored


def test_uptime_label():
    assert machine.uptime_label(90061) == "1 d 1 h" and machine.uptime_label(None) == "unknown"


# ---------- web ----------

def make_client(tmp_path, run):
    cfg = Settings(tmp_path)
    c = db.connect(cfg.db_path)
    db.migrate(c)
    return TestClient(create_app(cfg, run=run)), c


def test_status_page_and_fragment(tmp_path):
    client, c = make_client(tmp_path, fake())
    now = datetime.now(timezone.utc)
    for i in range(3):
        machine.sample(c, tmp_path, fake(), now - timedelta(minutes=10 - 5 * i))
    r = client.get("/status")
    assert r.status_code == 200 and 'http-equiv="refresh"' not in r.text            # no auto-refresh, no polling
    assert 'hx-trigger="load"' in r.text and "every 10s" not in r.text
    assert "M14 health" in r.text and 'class="series" d="M' in r.text and "data-points=" in r.text
    assert "What's normal?" in r.text and "Good under 80 °C" in r.text           # the ranges legend
    assert 'class="th warn"' in r.text                                           # Watch line on the temperature chart
    assert client.get("/status?range=7d").status_code == 200 and 'href="/status?range=30d#history"' in r.text
    f = client.get("/status/machine")
    assert f.status_code == 200 and "live reading" in f.text and "every 10s [document.visibilityState" in f.text
    for text in ("52 °C", "2,161 RPM", "Heat slowdown", ">None<", "% free", "97% health", "Verified", "-55 dBm",
                 "Worker 2.5% CPU", 'class="light ok"', ">Good<", "Good under 80 °C", "Everything looks normal"):
        assert text in f.text, text
    h = client.get("/health").json()
    assert h["machine"]["sample"]["wifi_dbm"] == -55 and h["machine"]["needs_attention"] == []
    c.close()


def test_unreadable_readings_are_left_out(tmp_path):
    client, c = make_client(tmp_path, fake(missing=(machine.TEMP_TOOL,)))
    page = client.get("/status/machine").text
    assert "Temperature" not in page and "Fans" not in page and "Wi-Fi" not in page   # no tool, no reading, not measured
    client2, c2 = make_client(tmp_path / "b", fake({(machine.TEMP_TOOL,): TEMP_BAD_FANS}))
    page = client2.get("/status/machine").text
    assert "Temperature" in page and "Fans" not in page
    c.close()
    c2.close()


def test_status_page_with_no_samples_and_garbage(tmp_path):
    client, c = make_client(tmp_path, garbage)
    r = client.get("/status")
    assert r.status_code == 200 and "History appears after" in r.text
    assert client.get("/status/machine").status_code == 200
    c.close()


def test_chart_buckets_gaps_and_thresholds():
    now = datetime(2026, 10, 19, 12, tzinfo=timezone.utc)
    rows = [{"ts": (now - timedelta(minutes=5 * i)).isoformat(), "temp_c": 50 + i} for i in range(12)]
    rows += [{"ts": (now - timedelta(hours=20)).isoformat(), "temp_c": 85}]          # long gap before the rest
    ch = machine.chart(rows, "temp_c", "Temperature", " °C", 30, 100, hours=24, now=now)
    assert ch["path"].count("M") == 2 and ch["high"] == "85 °C" and ch["now"] == "50 °C"
    assert [lv for _, lv in ch["thresholds"]] == ["warn", "bad"]
    assert machine.chart([], "temp_c", "T", "", 0, 100, hours=24, now=now) is None
    sw = machine.chart([{"ts": now.isoformat(), "swap_mb": 100}], "swap_mb", "Swap", " MB", 0, None, hours=24, now=now)
    assert any(lv == "bad" for _, lv in sw["thresholds"])                            # Problem line kept in view


def test_levels_ranges_and_overall():
    assert [machine.level("temp_c", t) for t in (60, 80, 95, None)] == ["ok", "warn", "bad", "unknown"]
    assert [machine.level("speed_limit", v) for v in (100, 90, 70)] == ["ok", "warn", "bad"]
    assert [machine.level("wifi_dbm", v) for v in (-55, -70, -80)] == ["ok", "warn", "bad"]
    assert machine.ranges_text("temp_c") == "Good under 80 °C · Watch 80 °C–90 °C · Problem 90 °C or more"
    c = machine.card({"cpu_pct": 20, "temp_c": 85, "smart": "Verified", "smart_ok": 1})
    assert c["levels"]["cpu_pct"] == "ok" and c["levels"]["smart"] == "ok" and c["overall"] == "warn"
    assert machine.card({"swap_mb": 3000})["overall"] == "bad" and machine.card({})["overall"] == "unknown"


def test_m14_problem_shows_on_today_and_header(tmp_path):
    client, c = make_client(tmp_path, fake())
    assert "alert-dot" not in client.get("/").text
    machine.sample(c, tmp_path, fake(), datetime.now(timezone.utc))
    c.execute("UPDATE machine_sample SET swap_mb = 4096")
    page = client.get("/").text
    assert "The M14 needs attention" in page and "Swap heavy" in page and "alert-dot" in page


def test_chart_gridlines_are_round_numbers_and_clock_times():
    assert machine.nice_ticks(0, 100) == [0, 25, 50, 75, 100]
    assert machine.nice_ticks(30, 100) == [40, 60, 80, 100]
    assert machine.nice_ticks(-90, -10) == [-80, -60, -40, -20]
    start = datetime(2026, 10, 19, 12, 20).astimezone()
    day = machine.time_grid(start, 24)
    assert [t.hour for t, _ in day] == [15, 18, 21, 0, 3, 6, 9, 12]                  # every 3 h on the clock
    assert [label for _, label in day if label] == ["18:00", "00:00", "06:00", "12:00"]
    week = machine.time_grid(start, 24 * 7)
    assert len(week) == 7 and all(t.hour == 0 and label for t, label in week)        # each midnight, Mon/Tue/...
    assert all(t.weekday() == 0 for t, _ in machine.time_grid(start, 24 * 30))       # Mondays over a month


def test_page_styles_live_in_app_css():
    """htmx page swaps skip a page's own <style>: the Status charts came out black (2026-10-20)."""
    from scribe_bot.web.app import WEB
    for t in (WEB / "templates").glob("*.html"):
        assert "<style" not in t.read_text(), f"{t.name}: put page styles in static/app.css"


def test_cpu_is_the_average_since_the_last_sample(conn, tmp_path, monkeypatch):
    """Not top's 1-second snapshot: the kernel's tick counters between two samples (2026-10-20, Brian: numbers that
    don't match the M14 mislead)."""
    assert machine.average_cpu((100, 1000), (400, 2000)) == 30.0
    assert machine.average_cpu((2**32 - 50, 2**32 - 100), (50, 900)) == 10.0      # counters wrapped
    assert machine.average_cpu(None, (1, 2)) is None
    ticks = iter([(1000, 10000), (4000, 20000)])
    first = machine.sample(conn, tmp_path, fake(), T0, ticks=lambda: next(ticks))
    assert first["cpu_pct"] == machine.cpu(fake())["cpu_pct"]                        # no previous sample: the snapshot
    second = machine.sample(conn, tmp_path, fake(), T0 + timedelta(minutes=5), ticks=lambda: next(ticks))
    assert second["cpu_pct"] == 30.0                                                # 3000 busy of 10000 ticks
    if REAL_CPU_TICKS():                                                            # macOS: the real counters work
        busy, total = REAL_CPU_TICKS()
        assert 0 < busy < total


def test_smart_not_supported_is_no_reading():
    # Brian 2026-11-19: diskutil flips between "Verified" and "Not Supported" on the M14 (about 1 reading in 5),
    # which sent alert / all-clear pairs; "Not Supported" is no reading, only "Failing" etc. count as bad
    ns = machine.fast(fake({("diskutil",): "   SMART Status:              Not Supported\n"}))
    assert ns["smart"] == "Not Supported" and ns["smart_ok"] is None
    bad = machine.fast(fake({("diskutil",): "   SMART Status:              Failing\n"}))
    assert bad["smart_ok"] is False
