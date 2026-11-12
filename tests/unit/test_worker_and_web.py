import logging
import subprocess

from fastapi.testclient import TestClient

from scribe_bot import worker
from scribe_bot.core import db, queue
from scribe_bot.core.settings import Settings
from scribe_bot.web.app import create_app

PMSET_BATTERY = "Now drawing from 'Battery Power'\n -InternalBattery-0 (id=1)\t64%; discharging; 2:10 remaining present: true\n"


def fake_pmset(args, **kw):
    return subprocess.CompletedProcess(args, 0, stdout=PMSET_BATTERY, stderr="")


def test_tick_runs_jobs_and_beats(tmp_path, monkeypatch):
    cfg = Settings(tmp_path)
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    ran = []
    monkeypatch.setattr(worker, "PIPELINES", {"demo": [("only", lambda ctx: ran.append(ctx.job_id))]})
    job = queue.enqueue(conn, "demo")
    worker.tick(conn, cfg, logging.getLogger("test"))
    assert ran == [job]
    assert conn.execute("SELECT value FROM setting WHERE key = 'worker_heartbeat'").fetchone()


def test_background_thread_beats_samples_and_sends_during_long_work(tmp_path, monkeypatch):
    import threading
    import time as t
    cfg = Settings(tmp_path)
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    sends = []
    monkeypatch.setattr(worker.sender, "send_pending", lambda c: sends.append(1) or {"sent": 1})
    conn.execute("INSERT INTO notification (channel, title, created_at) VALUES ('bot', 'Test', ?)", (db.now(),))
    stop = threading.Event()
    worker.background_thread(cfg, stop, logging.getLogger("test"), every=0.01, slow_every=0.1)
    t.sleep(0.05)  # the main loop is "busy": the queued message goes out on the fast cadence, before the first beat
    assert sends and not conn.execute("SELECT 1 FROM setting WHERE key = 'worker_heartbeat'").fetchone()
    t.sleep(0.3)   # then the slow cadence: heartbeat and M14 sample
    stop.set()
    assert conn.execute("SELECT value FROM setting WHERE key = 'worker_heartbeat'").fetchone()
    assert conn.execute("SELECT COUNT(*) FROM machine_sample").fetchone()[0] >= 1


def test_paused_worker_skips_jobs(tmp_path, monkeypatch):
    cfg = Settings(tmp_path)
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    ran = []
    monkeypatch.setattr(worker, "PIPELINES", {"demo": [("only", lambda ctx: ran.append(ctx.job_id))]})
    queue.enqueue(conn, "demo")
    conn.execute("INSERT INTO setting VALUES ('pipeline_paused', '1')")
    worker.tick(conn, cfg, logging.getLogger("test"))
    assert ran == []
    conn.execute("UPDATE setting SET value = '0' WHERE key = 'pipeline_paused'")
    worker.tick(conn, cfg, logging.getLogger("test"))
    assert len(ran) == 1


def test_status_page_and_health(tmp_path):
    cfg = Settings(tmp_path)
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    client = TestClient(create_app(cfg, run=fake_pmset))

    page = client.get("/status")
    assert page.status_code == 200
    assert "ON BATTERY" in page.text and "64%" in page.text
    assert "never started" in page.text  # no worker heartbeat yet

    worker.beat(conn)
    h = client.get("/health").json()
    assert h["ok"] and h["worker"]["alive"]
    assert "running" in client.get("/status").text


def test_health_503_when_db_unmigrated(tmp_path):
    cfg = Settings(tmp_path)
    db.connect(cfg.db_path).close()  # exists but no migrations
    assert TestClient(create_app(cfg, run=fake_pmset)).get("/health").status_code == 503
