"""Processing progress, start/finish times, and estimates (DECISION S, 2026-10-19).

Estimates come from how long each stage really took on the M14: the median seconds per audio minute over the last
10 completed runs of that stage. Until there is history, defaults measured on 2026-10-19 (a 17.9-min recording) are used.
Times are stored as UTC ISO strings; local_time() formats them for display ("4:09 pm").
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from statistics import median

# Must match scribe_bot.pipeline.recording.PIPELINE (a test checks this).
STAGES = ["audio", "transcribe", "diarize", "identify", "file", "ai_extract", "notify"]
LABELS = {"audio": "Preparing audio", "transcribe": "Transcribing", "diarize": "Separating speakers",
          "identify": "Recognizing voices", "file": "Saving", "ai_extract": "AI summary", "notify": "Notifying"}
# Minutes of M14 time per audio minute (identify includes the possible 1:1 re-separation and voice embeddings).
DEFAULT_RATE = {"audio": 0.002, "transcribe": 0.2, "diarize": 0.44, "identify": 0.35, "file": 0.02}
DEFAULT_FLAT_S = {"ai_extract": 20.0, "notify": 1.0}  # these don't grow with length
HISTORY = 10
RUNNING_FLOOR = 0.1  # a running stage always has at least 10% of its estimate left


def _parse(ts: str | None) -> datetime | None:
    return datetime.fromisoformat(ts) if ts else None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def local_time(ts: str | datetime | None) -> str:
    """'4:09 pm' in the M14's local time zone."""
    dt = _parse(ts) if isinstance(ts, str) else ts
    if dt is None:
        return ""
    return dt.astimezone().strftime("%-I:%M %p").lower()


def duration_label(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 60:
        return f"{max(int(round(seconds)), 1)} s"
    minutes = seconds / 60
    if minutes >= 90:   # "4 h 10 min"
        h, m = divmod(round(minutes), 60)
        return f"{h} h {m} min" if m else f"{h} h"
    return f"{minutes:.0f} min" if minutes >= 10 else f"{minutes:.1f} min".replace(".0 min", " min")


def rates(conn: sqlite3.Connection) -> dict[str, tuple[str, float]]:
    """stage -> ('per_min', seconds per audio minute) or ('flat', seconds)."""
    out: dict[str, tuple[str, float]] = {}
    for stage in STAGES:
        rows = conn.execute(
            "SELECT s.started_at, s.finished_at, r.duration_s FROM stage_run s JOIN job j ON j.id = s.job_id "
            "JOIN recording r ON r.id = j.recording_id WHERE j.kind = 'recording' AND s.stage = ? AND s.status = 'done' "
            "AND s.started_at IS NOT NULL AND s.finished_at IS NOT NULL AND r.duration_s > 0 "
            "ORDER BY s.finished_at DESC LIMIT ?", (stage, HISTORY)).fetchall()
        took = [((_parse(r["finished_at"]) - _parse(r["started_at"])).total_seconds(), r["duration_s"] / 60) for r in rows]
        if stage in DEFAULT_FLAT_S:
            out[stage] = ("flat", median(t for t, _ in took) if took else DEFAULT_FLAT_S[stage])
        else:
            out[stage] = ("per_min", median(t / m for t, m in took) if took else DEFAULT_RATE[stage] * 60)
    return out


def estimate(conn: sqlite3.Connection, duration_s: float | None) -> dict:
    """{'stages': {stage: seconds}, 'total_s': seconds} for a recording of this length."""
    minutes = (duration_s or 0) / 60
    stages = {s: (v if kind == "flat" else v * minutes) for s, (kind, v) in rates(conn).items()}
    return {"stages": stages, "total_s": sum(stages.values())}


def choices(conn: sqlite3.Connection, durations_s: list[float | None]) -> list[dict]:
    """Before you approve a recording: M14 time for "Transcript + AI" and for "Transcript only" (no AI stage), each."""
    r = rates(conn)
    out = []
    for d in durations_s:
        stages = {s: (v if kind == "flat" else v * (d or 0) / 60) for s, (kind, v) in r.items()}
        total = sum(stages.values())
        out.append({"ai_s": total, "local_s": total - stages["ai_extract"]})
    return out


def queue_wait_s(conn: sqlite3.Connection, now: datetime | None = None) -> float:
    """How long until the M14 is free: what's left of every recording job running or waiting its turn."""
    return sum(p["remaining_s"] for p in active_jobs(conn, now))


def job_progress(conn: sqlite3.Connection, job_id: int, now: datetime | None = None) -> dict | None:
    now = now or datetime.now(timezone.utc)
    job = conn.execute(
        "SELECT j.*, r.duration_s, r.title AS rec_title, c.id AS conversation_id, c.title AS conv_title "
        "FROM job j LEFT JOIN recording r ON r.id = j.recording_id LEFT JOIN conversation c ON c.recording_id = j.recording_id "
        "WHERE j.id = ?", (job_id,)).fetchone()
    if job is None:
        return None
    est = estimate(conn, job["duration_s"])["stages"]
    runs = {r["stage"]: r for r in conn.execute("SELECT * FROM stage_run WHERE job_id = ?", (job_id,))}
    stages, current, spent_current = [], None, 0.0
    for i, name in enumerate(STAGES):
        r = runs.get(name)
        started, finished = _parse(r["started_at"]) if r else None, _parse(r["finished_at"]) if r else None
        status = r["status"] if r else "pending"
        took = (finished - started).total_seconds() if started and finished and status == "done" else None
        stages.append({"stage": name, "label": LABELS[name], "status": status, "started_at": r["started_at"] if r else None,
                       "finished_at": r["finished_at"] if r and status == "done" else None, "duration_s": took,
                       "estimate_s": est[name], "started": local_time(started), "finished": local_time(finished) if took is not None else "",
                       "took": duration_label(took)})
        if current is None and status != "done":
            current = i
            if status == "running" and started:
                spent_current = max((now - started).total_seconds(), 0)

    first = next((s["started_at"] for s in stages if s["started_at"]), None)
    done = current is None
    if done:
        last = max((s["finished_at"] for s in stages if s["finished_at"]), default=None)
        eta = _parse(last) if last else now
        remaining = 0.0
    else:
        cur = est[STAGES[current]]
        remaining = max(cur - spent_current, RUNNING_FLOOR * cur) + sum(est[s] for s in STAGES[current + 1:])
        eta = now + timedelta(seconds=remaining)
    t = now   # when each unfinished step should end: the running one, then the rest in turn
    for i, st in enumerate(stages):
        if st["status"] == "done" or done:
            st["ends"] = st["finished"]
            continue
        left = max(st["estimate_s"] - spent_current, RUNNING_FLOOR * st["estimate_s"]) if i == current else st["estimate_s"]
        t = t + timedelta(seconds=left)
        st["ends"] = "~" + local_time(t)
    total_est = sum(est.values()) or 1
    finished_est = sum(est[s["stage"]] for s in stages if s["status"] == "done")
    if not done:
        finished_est += min(spent_current, est[STAGES[current]] * (1 - RUNNING_FLOOR))
    elapsed = ((eta if done else now) - _parse(first)).total_seconds() if first else 0.0
    return {
        "job_id": job_id, "status": job["status"], "recording_id": job["recording_id"],
        "conversation_id": job["conversation_id"], "title": job["conv_title"] or job["rec_title"] or "Recording",
        "minutes": round((job["duration_s"] or 0) / 60, 1), "stages": stages,
        "step": len(STAGES) if done else current + 1, "steps": len(STAGES),
        "current": None if done else stages[current], "percent": 100 if done else min(int(100 * finished_est / total_est), 99),
        "started_at": first, "started": local_time(first), "elapsed_s": elapsed, "elapsed": duration_label(elapsed),
        "remaining_s": remaining, "eta_at": _iso(eta), "eta": local_time(eta), "done": done,
    }


def active_jobs(conn: sqlite3.Connection, now: datetime | None = None) -> list[dict]:
    """Recording jobs that are running or waiting their turn, oldest first."""
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM job WHERE kind = 'recording' AND status IN ('running', 'pending') ORDER BY id")]
    return [p for p in (job_progress(conn, i, now) for i in ids) if p]


def latest_for_recording(conn: sqlite3.Connection, recording_id: int | None) -> dict | None:
    if recording_id is None:
        return None
    row = conn.execute("SELECT max(id) FROM job WHERE kind = 'recording' AND recording_id = ?", (recording_id,)).fetchone()
    return job_progress(conn, row[0]) if row and row[0] else None
