"""`python -m scribe_bot.worker`: the background worker (launchd service com.scribebot.worker).

Every 30 s: report any downtime, pick up new recordings, run due schedules (with catch-up), drain the job queue one
job at a time. A background thread meanwhile sends queued Discord notifications (within ~5 s), samples M14 health
(every 5 min) and writes a heartbeat to the setting table, so the Status page can show whether the worker is alive
(and the downtime report spot gaps), even in the middle of a long job.
"""
from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

from scribe_bot.ai import jobs as ai_jobs
from scribe_bot.backup import jobs as backup_jobs
from scribe_bot.core import cleanup, db, logs, machine, queue, scheduler, settings
from scribe_bot.discord_bot import briefing, sender
from scribe_bot.pipeline import ingest, recording

POLL_SECONDS = 30
SEND_SECONDS = 5   # a queued Discord notification goes out within about this long
PIPELINES: dict[str, queue.Pipeline] = {"recording": recording.PIPELINE, ai_jobs.KIND: ai_jobs.PIPELINE}
MACHINE_RUN = subprocess.run   # tests swap in a fake (no real commands)
SCHEDULES: list[scheduler.Schedule] = [*briefing.SCHEDULES, *backup_jobs.SCHEDULES, *cleanup.SCHEDULES]


def beat(conn) -> None:
    conn.execute("INSERT INTO setting (key, value) VALUES ('worker_heartbeat', ?) "
                 "ON CONFLICT (key) DO UPDATE SET value = excluded.value", (db.now(),))


def background_thread(cfg: settings.Settings, stop: threading.Event, log, every: float = SEND_SECONDS,
                      slow_every: float = POLL_SECONDS) -> threading.Thread:
    """Heartbeat, Discord and the M14 sample from a separate thread (own connection), so a long stage doesn't make the
    worker look dead, hold notifications back (a test message waited 32 s; none went out mid-recording) or leave a gap
    in the Status charts. The beat means "the process is alive".

    Every `every` s: send if something is queued. Every `slow_every` s: beat, sample if due, and send anyway (held
    night-time messages; retries after Discord was unreachable, so a Discord outage isn't retried every 5 s)."""
    def run():
        c = db.connect(cfg.db_path)
        last_slow, retry_slow = time.monotonic(), False   # the first beat waits: the first tick's downtime report sees the gap
        try:
            while not stop.wait(every):
                slow = time.monotonic() - last_slow >= slow_every
                try:
                    queued = c.execute("SELECT 1 FROM notification WHERE status = 'queued' LIMIT 1").fetchone()
                    if slow or (queued and not retry_slow):
                        retry_slow = sender.send_pending(c).get("waiting", 0) > 0
                except Exception:
                    log.exception("sending notifications failed")
                if not slow:
                    continue
                last_slow = time.monotonic()
                try:
                    machine.maybe_sample(c, cfg.data_dir, MACHINE_RUN)   # M14 health, every 5 min (numbers only)
                except Exception:
                    log.exception("machine sample failed")
                try:
                    beat(c)
                except Exception:
                    pass  # a locked database just skips one beat
        finally:
            c.close()
    t = threading.Thread(target=run, name="background", daemon=True)
    t.start()
    return t


def paused(conn) -> bool:
    """Setting 'pipeline_paused' = '1' stops new recordings being picked up and processed (schedules still run)."""
    row = conn.execute("SELECT value FROM setting WHERE key = 'pipeline_paused'").fetchone()
    return bool(row) and row[0] == "1"


def tick(conn, cfg: settings.Settings, log) -> None:
    briefing.downtime_report(conn)  # before beat(), so a heartbeat gap is still visible
    hold = paused(conn)
    if not hold:
        for rec_id in ingest.scan_inbound(conn, cfg):
            log.info("recording %d queued", rec_id)
    for name, missed in scheduler.run_due(conn, SCHEDULES, datetime.now().astimezone()):
        log.info("schedule %s ran (missed %d)", name, missed.count)
    for job_id in ([] if hold else queue.run_all(conn, PIPELINES, cfg.jobs_dir)):
        status = conn.execute("SELECT status FROM job WHERE id = ?", (job_id,)).fetchone()[0]
        log.info("job %d %s", job_id, status)
    beat(conn)


def main() -> int:
    cfg = settings.load()
    log = logs.setup(cfg.data_dir / "logs")
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    SCHEDULES.extend(recording.make_schedules(conn, cfg))
    stopping = False

    def stop(*_):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    beating = threading.Event()
    background_thread(cfg, beating, log)
    log.info("worker started")
    while not stopping:
        try:
            tick(conn, cfg, log)
        except Exception:
            log.exception("worker tick failed")  # launchd restarts us only if we exit; keep going
        for _ in range(POLL_SECONDS):
            if stopping:
                break
            time.sleep(1)
    beating.set()
    log.info("worker stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
