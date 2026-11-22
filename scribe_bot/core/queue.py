"""SQLite job queue with saved stages (CLAUDE.md rule 5: every step saves its output; jobs resume after a crash).

A pipeline is an ordered list of (stage name, handler). Each handler gets a StageContext and returns what it saved
(usually a file path). A stage is marked done only after its handler returns, so after a crash the job resumes at the
first stage that isn't done. Handlers must therefore be safe to re-run (write to a temp file, then rename).
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from scribe_bot.core.db import now, transaction

MAX_ATTEMPTS = 3


class Paused(Exception):
    """A stage stopped early because Brian pressed Pause (setting 'pipeline_paused' = '1', Brian 2026-11-21). The job goes
    back to 'pending' without using up an attempt; stages save what they finished so Resume carries on from there."""


def hold_requested(conn: sqlite3.Connection | None) -> bool:
    if conn is None:
        return False
    # ❚❚ Pause, or the heat guard (pipeline/pace.py, Brian "heat OK" 2026-11-21): both stop at the same safe points
    return conn.execute("SELECT 1 FROM setting WHERE key IN ('pipeline_paused', 'heat_paused') AND value = '1'").fetchone() is not None


@dataclass
class StageContext:
    job_id: int
    recording_id: int | None
    payload: dict
    work_dir: Path                 # this job's folder for stage outputs
    outputs: dict[str, str]        # earlier stages' saved outputs, by stage name
    conn: sqlite3.Connection | None = None   # the worker's database connection


Handler = Callable[[StageContext], str | None]
Pipeline = list[tuple[str, Handler]]


def enqueue(conn: sqlite3.Connection, kind: str, recording_id: int | None = None, payload: dict | None = None) -> int:
    t = now()
    cur = conn.execute(
        "INSERT INTO job (kind, recording_id, payload, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (kind, recording_id, json.dumps(payload or {}), t, t),
    )
    return cur.lastrowid


def _claim(conn: sqlite3.Connection, skip_kinds: tuple[str, ...] = ()) -> sqlite3.Row | None:
    """Oldest unfinished job. A job left 'running' means the worker died mid-job, so it is picked up again first.
    skip_kinds: pending jobs of these kinds wait (Night only holds new recordings until 1 AM)."""
    marks = ",".join("?" * len(skip_kinds))
    held = f" AND NOT (status = 'pending' AND kind IN ({marks}))" if skip_kinds else ""
    with transaction(conn):
        row = conn.execute(
            f"SELECT * FROM job WHERE status IN ('running', 'pending'){held} ORDER BY status = 'pending', id LIMIT 1",
            tuple(skip_kinds)).fetchone()
        if row:
            conn.execute("UPDATE job SET status = 'running', updated_at = ? WHERE id = ?", (now(), row["id"]))
    return row


def _finish(conn: sqlite3.Connection, job_id: int, status: str, error: str | None = None) -> None:
    conn.execute("UPDATE job SET status = ?, error = ?, updated_at = ? WHERE id = ?", (status, error, now(), job_id))


def run_next(conn: sqlite3.Connection, pipelines: dict[str, Pipeline], jobs_dir: Path,
             skip_kinds: tuple[str, ...] = ()) -> int | None:
    """Run one job to completion (or until a stage fails). Returns the job id, or None if the queue is empty."""
    job = _claim(conn, skip_kinds)
    if job is None:
        return None
    pipeline = pipelines.get(job["kind"])
    if pipeline is None:
        _finish(conn, job["id"], "failed", f"no pipeline for kind {job['kind']!r}")
        return job["id"]

    done = {r["stage"]: r["output"] for r in conn.execute(
        "SELECT stage, output FROM stage_run WHERE job_id = ? AND status = 'done'", (job["id"],))}
    work_dir = jobs_dir / str(job["id"])
    work_dir.mkdir(parents=True, exist_ok=True)

    for stage, handler in pipeline:
        if stage in done:
            continue
        with transaction(conn):
            conn.execute(
                "INSERT INTO stage_run (job_id, stage, status, attempts, started_at) VALUES (?, ?, 'running', 1, ?) "
                "ON CONFLICT (job_id, stage) DO UPDATE SET status = 'running', attempts = attempts + 1, "
                "started_at = excluded.started_at, error = NULL",
                (job["id"], stage, now()),
            )
        ctx = StageContext(job["id"], job["recording_id"], json.loads(job["payload"]), work_dir, dict(done), conn)
        try:
            output = handler(ctx)
        except Paused:
            with transaction(conn):
                conn.execute("DELETE FROM stage_run WHERE job_id = ? AND stage = ?", (job["id"], stage))
                _finish(conn, job["id"], "pending", None)
            return job["id"]
        except Exception as e:  # a crash (kill, power loss) never reaches here; the 'running' row is resumed instead
            attempts = conn.execute(
                "SELECT attempts FROM stage_run WHERE job_id = ? AND stage = ?", (job["id"], stage)).fetchone()[0]
            with transaction(conn):
                conn.execute(
                    "UPDATE stage_run SET status = 'failed', error = ?, finished_at = ? WHERE job_id = ? AND stage = ?",
                    (f"{type(e).__name__}: {e}", now(), job["id"], stage),
                )
                # Under the attempt limit the job goes back in the queue and retries this stage next time.
                _finish(conn, job["id"], "failed" if attempts >= MAX_ATTEMPTS else "pending", f"{stage}: {e}")
            return job["id"]
        with transaction(conn):
            conn.execute(
                "UPDATE stage_run SET status = 'done', output = ?, finished_at = ? WHERE job_id = ? AND stage = ?",
                (output, now(), job["id"], stage),
            )
        done[stage] = output

    _finish(conn, job["id"], "done")
    return job["id"]


def run_all(conn: sqlite3.Connection, pipelines: dict[str, Pipeline], jobs_dir: Path,
            skip_kinds=()) -> list[int]:
    """Drain the queue one job at a time (the M14 runs one job at a time to fit in 16 GB of RAM). skip_kinds may be a
    function returning the kinds to hold, asked before each job (so Night only stops new work at 7 AM between jobs)."""
    ran = []
    while (job_id := run_next(conn, pipelines, jobs_dir, tuple(skip_kinds() if callable(skip_kinds) else skip_kinds))) is not None:
        ran.append(job_id)
        status = conn.execute("SELECT status FROM job WHERE id = ?", (job_id,)).fetchone()[0]
        if status == "pending":  # a stage failed and will retry; don't spin on it within this drain
            break
    return ran
