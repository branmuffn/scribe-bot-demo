"""Processing progress and estimates (DECISION S). Synthetic data only."""
import subprocess
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from scribe_bot.core import db, progress, queue
from scribe_bot.core.settings import Settings
from scribe_bot.pipeline import recording
from scribe_bot.web.app import create_app

NOW = datetime(2026, 10, 19, 20, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.isoformat(timespec="seconds")


def add_recording(conn, duration_s=600, status="processing", title="Synthetic sync", n=[0]):
    n[0] += 1
    cur = conn.execute(
        "INSERT INTO recording (source, original_path, sha256, duration_s, received_at, status, title) "
        "VALUES ('recorder', '/x.m4a', ?, ?, ?, ?, ?)", (f"sha{n[0]}", duration_s, iso(NOW), status, title))
    return cur.lastrowid


def add_job(conn, rec_id, status="running"):
    job = queue.enqueue(conn, "recording", rec_id)
    conn.execute("UPDATE job SET status = ? WHERE id = ?", (status, job))
    return job


def add_run(conn, job, stage, start, end=None, status=None):
    conn.execute("INSERT INTO stage_run (job_id, stage, status, attempts, started_at, finished_at) VALUES (?, ?, ?, 1, ?, ?)",
                 (job, stage, status or ("done" if end else "running"), iso(start), iso(end) if end else None))


def test_stages_match_the_pipeline():
    assert progress.STAGES == [name for name, _ in recording.PIPELINE]


def test_estimate_uses_defaults_without_history(conn):
    est = progress.estimate(conn, 17.9 * 60)
    assert round(est["stages"]["transcribe"]) == round(0.2 * 60 * 17.9)
    assert round(est["stages"]["diarize"]) == round(0.44 * 60 * 17.9)
    assert est["stages"]["ai_extract"] == 20
    # ~1.0 min of M14 time per audio minute in total -> ~18 min for this recording
    assert 15 * 60 < est["total_s"] < 22 * 60


def test_estimate_learns_from_history(conn):
    for _ in range(3):  # transcribe really took 30 s per audio minute (10 min audio -> 300 s)
        job = add_job(conn, add_recording(conn, 600), "done")
        add_run(conn, job, "transcribe", NOW, NOW + timedelta(seconds=300))
    est = progress.estimate(conn, 1200)
    assert est["stages"]["transcribe"] == 600  # 20 min audio x 30 s
    assert est["stages"]["diarize"] == 0.44 * 60 * 20  # no history -> default


def test_job_progress_mid_run(conn):
    job = add_job(conn, add_recording(conn, 600))
    add_run(conn, job, "audio", NOW - timedelta(seconds=90), NOW - timedelta(seconds=88))
    add_run(conn, job, "transcribe", NOW - timedelta(seconds=60))
    p = progress.job_progress(conn, job, now=NOW)
    assert p["step"] == 2 and p["steps"] == 7 and p["current"]["label"] == "Transcribing"
    est = progress.estimate(conn, 600)["stages"]
    rest = sum(est[s] for s in progress.STAGES[2:])
    assert abs(p["remaining_s"] - ((est["transcribe"] - 60) + rest)) < 1
    assert 0 < p["percent"] < 100 and not p["done"]
    assert p["stages"][0]["took"] == "2 s" and p["stages"][1]["status"] == "running"
    assert p["stages"][1]["ends"].startswith("~") and p["stages"][-1]["ends"] == "~" + p["eta"]   # each step: when it should end
    assert p["elapsed_s"] == 90


def test_overdue_stage_keeps_a_floor(conn):
    job = add_job(conn, add_recording(conn, 600))
    add_run(conn, job, "audio", NOW - timedelta(hours=1), NOW - timedelta(hours=1))
    add_run(conn, job, "transcribe", NOW - timedelta(minutes=59))  # far beyond its ~2 min estimate
    p = progress.job_progress(conn, job, now=NOW)
    est = progress.estimate(conn, 600)["stages"]
    assert p["remaining_s"] >= 0.1 * est["transcribe"] and p["percent"] <= 99


def test_finished_job(conn):
    job = add_job(conn, add_recording(conn, 600, status="done"), "done")
    t = NOW - timedelta(minutes=12)
    for i, s in enumerate(progress.STAGES):
        add_run(conn, job, s, t + timedelta(minutes=i), t + timedelta(minutes=i + 1))
    p = progress.job_progress(conn, job, now=NOW)
    assert p["done"] and p["percent"] == 100 and p["elapsed"] == "7 min"


def test_pending_job_waits_its_turn(conn):
    job = add_job(conn, add_recording(conn, 600), "pending")
    p = progress.job_progress(conn, job, now=NOW)
    assert p["step"] == 1 and p["started"] == "" and p["remaining_s"] > 0
    assert [a["job_id"] for a in progress.active_jobs(conn, now=NOW)] == [job]


def test_local_time_format():
    assert progress.local_time(datetime(2026, 10, 19, 16, 9).astimezone()) == "4:09 PM"
    assert progress.duration_label(14 * 60) == "14 min" and progress.duration_label(3.6 * 60) == "3.6 min"


# ---------- Web ----------

def web(tmp_path):
    cfg = Settings(tmp_path)
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    return conn, TestClient(create_app(cfg, run=lambda args, **k: subprocess.CompletedProcess(args, 0, stdout="Now drawing from 'AC Power'\n", stderr="")))


def test_today_shows_live_processing_card(tmp_path):
    conn, client = web(tmp_path)
    job = add_job(conn, add_recording(conn, 600))
    add_run(conn, job, "audio", datetime.now(timezone.utc) - timedelta(seconds=30), datetime.now(timezone.utc))
    page = client.get("/").text
    assert "Processing" in page and "Step 2 of 7" in page and "Transcribing" in page and "ready ~" in page
    assert 'hx-trigger="every 15s"' in page
    # the full bar: percent, time left and each step (same card on Status)
    assert 'aria-valuenow="' in page and "%</b>" in page and " left</span>" in page
    assert "Each step" in page and "✓ Preparing audio" in page and "○ Separating speakers" in page
    status = client.get("/status").text
    assert "Processing now" in status and "Each step" in status and "Step 2 of 7" in status


def test_progress_fragment_stops_polling_when_idle(tmp_path):
    conn, client = web(tmp_path)
    frag = client.get("/progress/active").text
    assert 'id="progress-live"' in frag and "hx-trigger" not in frag


def test_waiting_for_ok_count_on_today(tmp_path):
    """A waiting recording shows once on Today: as its Needs you row (no separate notice repeating it)."""
    conn, client = web(tmp_path)
    rec = add_recording(conn, 600, status="awaiting_approval")
    conn.execute("INSERT INTO review_item (kind, ref_id, payload, created_at) VALUES ('approval', ?, ?, ?)",
                 (rec, f'{{"recording_id": {rec}, "title": "Synthetic", "minutes": 10, "ai_cost": 0.01}}', iso(NOW)))
    page = client.get("/").text
    assert "Process 1 recording" in page and 'aria-label="1 waiting"' in page and "1 recording waiting for your OK" not in page


def test_review_estimate_from_model(tmp_path):
    conn, client = web(tmp_path)
    rec = add_recording(conn, 17.9 * 60, status="awaiting_approval")
    conn.execute("INSERT INTO review_item (kind, ref_id, payload, created_at) VALUES ('approval', ?, ?, ?)",
                 (rec, f'{{"recording_id": {rec}, "title": "Synthetic", "minutes": 17.9, "m14_minutes": 5, "ai_cost": 0.02}}', iso(NOW)))
    e = progress.choices(conn, [17.9 * 60])[0]
    page = client.get("/inbox").text   # both choices side by side: time on the M14 (+ AI cost), and transcript only
    assert f"{progress.duration_label(e['ai_s'])} · $0.02" in page          # the sheet: ✦ with AI, and transcript only
    assert f"{progress.duration_label(e['local_s'])} · free" in page and e["local_s"] < e["ai_s"]
    rec2 = add_recording(conn, 30 * 60, status="awaiting_approval")
    conn.execute("INSERT INTO review_item (kind, ref_id, payload, created_at) VALUES ('approval', ?, ?, ?)",
                 (rec2, f'{{"recording_id": {rec2}, "title": "Synthetic 2", "minutes": 30, "ai_cost": 0.03}}', iso(NOW)))
    assert 'To do all 2 new ones: ~' in client.get("/inbox").text   # and the total when several wait (F32 wording)
    assert progress.duration_label(250 * 60) == "4 h 10 min" and progress.duration_label(120 * 60) == "2 h"


def test_conversation_page_shows_how_it_was_processed(tmp_path):
    conn, client = web(tmp_path)
    rec = add_recording(conn, 600, status="done")
    conn.execute("INSERT INTO conversation (recording_id, title, status, created_at, updated_at) VALUES (?, 'Synthetic', 'extracted', ?, ?)",
                 (rec, iso(NOW), iso(NOW)))
    cid = conn.execute("SELECT id FROM conversation").fetchone()[0]
    job = add_job(conn, rec, "done")
    for i, s in enumerate(progress.STAGES):
        add_run(conn, job, s, NOW + timedelta(minutes=i), NOW + timedelta(minutes=i + 1))
    conn.execute("INSERT INTO ledger_entry (time, provider, model, task, conversation_id, sent_summary, cost_usd) "
                 "VALUES (?, 'anthropic', 'claude-sonnet-5-5', 'extract', ?, 'transcript', 0.021)", (iso(NOW), cid))
    page = client.get(f"/conversations/{cid}").text
    assert "How it was processed" in page and "took 7 min" in page and "Separating speakers" in page
    assert "claude-sonnet-5-5" in page and "$0.021" in page


def test_status_page_lists_active_jobs(tmp_path):
    conn, client = web(tmp_path)
    add_job(conn, add_recording(conn, 600), "pending")
    page = client.get("/status").text
    assert "Processing now" in page and "Step 1 of 7" in page


def test_waiting_jobs_get_their_turn_times_one_after_another(conn):
    """B39 (Brian, 2026-11-25): three waiting recordings all said "ready ~11:52". The M14 does one at a time, so each
    waiting one starts when the one ahead finishes."""
    running = add_job(conn, add_recording(conn, 600), "running")
    add_run(conn, running, "prepare", NOW - timedelta(minutes=1), NOW - timedelta(seconds=50))
    add_run(conn, running, "transcribe", NOW - timedelta(seconds=50))
    w1 = add_job(conn, add_recording(conn, 600), "pending")
    w2 = add_job(conn, add_recording(conn, 600), "pending")
    jobs = progress.active_jobs(conn, NOW)
    assert [j["job_id"] for j in jobs] == [running, w1, w2]
    first, second, third = (datetime.fromisoformat(j["eta_at"]) for j in jobs)
    assert first < second < third
    assert jobs[1]["starts"] and jobs[2]["starts"] and "starts" not in jobs[0]
    single = progress.estimate(conn, 600)["total_s"]
    assert abs((third - second).total_seconds() - single) < 5


def test_status_folds_the_rest_of_the_queue(tmp_path):
    conn, client = web(tmp_path)
    for _ in range(4):
        add_job(conn, add_recording(conn, 600), "pending")
    page = client.get("/status").text
    assert "2 more waiting" in page and "starts ~" in page
