"""`python -m scribe_bot.worker`: the background worker (launchd service com.scribebot.worker).

Every 30 s: report any downtime, pick up new recordings, run due schedules (with catch-up), drain the job queue one
job at a time. A background thread meanwhile sends queued Discord notifications (within ~5 s), samples M14 health
(every 5 min) and writes a heartbeat to the setting table, so the Status page can show whether the worker is alive
(and the downtime report spot gaps), even in the middle of a long job.
"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime

from scribe_bot.ai import jobs as ai_jobs
from scribe_bot.ai import shortnames
from scribe_bot.backup import jobs as backup_jobs
from scribe_bot.core import cleanup, db, logs, machine, queue, scheduler, settings
from scribe_bot.discord_bot import briefing, notify, sender
from scribe_bot.pipeline import control, ingest, pace, peek, recording

POLL_SECONDS = 30
PACE_SECONDS = 55   # heat guard + Auto speed check (each check takes ~0.2 s of commands)
SEND_SECONDS = 5   # a queued Discord notification goes out within about this long
PIPELINES: dict[str, queue.Pipeline] = {"recording": recording.PIPELINE, ai_jobs.KIND: ai_jobs.PIPELINE,
                                         ai_jobs.COMPARE_KIND: ai_jobs.COMPARE_PIPELINE, ai_jobs.DETAIL_KIND: ai_jobs.DETAIL_PIPELINE,
                                         shortnames.KIND: shortnames.PIPELINE}
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
        last_slow, retry_slow = time.monotonic(), False
        last_pace = 0.0   # the first beat waits: the first tick's downtime report sees the gap
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
                try:   # heat guard + Auto speed (Brian, 2026-11-21); about once a minute
                    if time.monotonic() - last_pace >= PACE_SECONDS:
                        last_pace = time.monotonic()
                        r = pace.update(c, MACHINE_RUN, notify=lambda t: notify.enqueue(c, "bot", t, "/processing#now"))
                        if r["heat"]:
                            log.info("heat guard: %s", r["heat"])
                except Exception:
                    log.exception("pace check failed")
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
    """❚❚ Pause ('pipeline_paused') or the heat guard ('heat_paused') stops new recordings being picked up and processed
    (schedules still run)."""
    return queue.hold_requested(conn)


def resume_if_due(conn, at: str | None = None) -> bool:
    """"Resume tonight" (Brian, 2026-11-21): setting 'pipeline_resume_at' (UTC ISO) lifts the pause once it's reached."""
    row = conn.execute("SELECT value FROM setting WHERE key = 'pipeline_resume_at'").fetchone()
    if not row or not row[0] or row[0] > (at or db.now()):
        return False
    conn.execute("UPDATE setting SET value = '0' WHERE key = 'pipeline_paused'")
    conn.execute("DELETE FROM setting WHERE key = 'pipeline_resume_at'")
    return True


def tick(conn, cfg: settings.Settings, log) -> None:
    briefing.downtime_report(conn)  # before beat(), so a heartbeat gap is still visible
    if resume_if_due(conn):
        log.info("pause ended (resume time reached)")
    hold = paused(conn)
    if not hold:
        for rec_id in ingest.scan_inbound(conn, cfg):
            log.info("recording %d queued", rec_id)
    for name, missed in scheduler.run_due(conn, SCHEDULES, datetime.now().astimezone()):
        log.info("schedule %s ran (missed %d)", name, missed.count)
    night = lambda: ("recording",) if pace.holds_new_work(conn) else ()   # noqa: E731  Night only: new recordings wait for 1 AM
    for job_id in ([] if hold else queue.run_all(conn, PIPELINES, cfg.jobs_dir, night)):
        status = conn.execute("SELECT status FROM job WHERE id = ?", (job_id,)).fetchone()[0]
        log.info("job %d %s", job_id, status)
    if not hold:
        for rec_id in control.process_due(conn):   # "Process at…" times that have come
            log.info("recording %d sent (Process at)", rec_id)
        rid = control.overnight_next(conn)   # 1-7 AM: work through the Inbox as Transcript only (Brian, 2026-11-22)
        if rid:
            log.info("recording %d sent (overnight backlog, Transcript only)", rid)
        peek.run_one(conn, log, data_dir=cfg.data_dir)   # T1: quick title for one waiting recording, only when no job is running
    beat(conn)


NICE = 10   # processing yields the CPU to the web app and anything Brian is doing (child processes inherit it)


def main() -> int:
    try:
        os.nice(NICE)   # Brian, 2026-11-21: the app was laggy while a 2-hour talk processed; it now gets the CPU first
    except OSError:
        pass
    cfg = settings.load()
    log = logs.setup(cfg.data_dir / "logs")
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    try:
        recording.retitle_plain(conn)   # once per start: old no-AI talks called just "Conversation" get a quick title
    except Exception:
        log.exception("retitle failed")
    try:   # once per start: talks get the date they were recorded, from the file name (2026-11-23)
        from scribe_bot.pipeline import ingest as _ingest
        with db.transaction(conn):
            _ingest.fix_recorded_dates(conn)
    except Exception:
        log.exception("recorded-date fix failed")
    try:   # once per start, no AI: named speakers' "Speaker N" in short titles and detailed notes becomes their name
        from scribe_bot.ai import extract   # (2026-11-22 short titles; 2026-11-23 detailed notes)
        for (cid,) in conn.execute("SELECT DISTINCT conversation_id FROM task WHERE short_title LIKE '%Speaker %' UNION "
                                   "SELECT id FROM conversation WHERE short_title LIKE '%Speaker %' OR detailed LIKE '%Speaker %'").fetchall():
            extract.relabel_speakers(conn, cid)
    except Exception:
        log.exception("short-title relabel failed")
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
            if not paused(conn):
                shortnames.maybe_queue(conn)   # Q5: short list names for new and older talks (cheap model, titles only)
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
