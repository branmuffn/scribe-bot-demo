import shutil
import subprocess

from scribe_bot.core import db, health, queue
from scribe_bot.core.settings import Settings

# Synthetic pmset outputs (shapes as printed by macOS Sequoia).
ON_AC = ("Now drawing from 'AC Power'\n"
         " -InternalBattery-0 (id=1234567)\t100%; charged; 0:00 remaining present: true\n")
ON_BATTERY = ("Now drawing from 'Battery Power'\n"
              " -InternalBattery-0 (id=1234567)\t87%; discharging; 3:12 remaining present: true\n")
CHARGING = ("Now drawing from 'AC Power'\n"
            " -InternalBattery-0 (id=1234567)\t42%; charging; (no estimate) present: true\n")


def runner(stdout):
    return lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout=stdout, stderr="")


def test_parse_pmset():
    assert health.parse_pmset(ON_AC) == {"source": "ac", "battery_pct": 100, "state": "charged"}
    assert health.parse_pmset(ON_BATTERY) == {"source": "battery", "battery_pct": 87, "state": "discharging"}
    assert health.parse_pmset(CHARGING) == {"source": "ac", "battery_pct": 42, "state": "charging"}
    assert health.parse_pmset("") == {"source": "unknown", "battery_pct": None, "state": None}


def test_power_survives_missing_pmset():
    def boom(args, **kw):
        raise FileNotFoundError("pmset")
    assert health.power(boom)["source"] == "unknown"


def test_healthy_after_migrate(tmp_path):
    s = Settings(data_dir=tmp_path)
    conn = db.connect(s.db_path)
    db.migrate(conn)
    queue.enqueue(conn, "demo")
    conn.close()
    r = health.check(s, runner(ON_BATTERY))
    assert r["db"] == {"ok": True, "missing_migrations": [], "queue": {"pending": 1}}
    assert r["power"]["source"] == "battery"
    assert r["ok"] is (r["disk"]["used_pct"] <= health.DISK_ALERT_PCT)


def test_unhealthy_without_migrations(tmp_path):
    r = health.check(Settings(data_dir=tmp_path), runner(ON_AC))
    assert r["db"]["ok"] is False and r["ok"] is False


def test_disk_over_threshold_is_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setattr(health.shutil, "disk_usage", lambda p: shutil._ntuple_diskusage(100, 90, 10))
    assert health.disk(tmp_path) == {"used_pct": 90.0, "free_gb": 0.0, "ok": False}
