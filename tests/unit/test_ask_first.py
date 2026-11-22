import json

import pytest
from fastapi.testclient import TestClient

from pipeline_fakes import drop_file
from scribe_bot.core import db, queue, settings
from scribe_bot.pipeline import ingest, paths
from scribe_bot.web.app import create_app


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIBE_BOT_DATA", str(tmp_path / "data"))
    ingest._seen.clear()
    return settings.load()


@pytest.fixture
def conn(cfg):
    """The same database the web app opens (cfg.db_path), in ask-first mode unless a test changes it."""
    c = db.connect(cfg.db_path)
    db.migrate(c)
    yield c
    c.close()


def ask_mode(conn):
    conn.execute("DELETE FROM setting WHERE key = 'processing_mode'")  # default = ask


def new_recording(conn, cfg, name="Weekly sync.m4a"):
    ingest.scan_inbound(conn, cfg)  # baseline
    drop_file(paths.inbound(cfg), name, b"synthetic")
    [rec_id] = ingest.scan_inbound(conn, cfg)
    return rec_id


def test_default_mode_is_ask_first(conn):
    conn.execute("DELETE FROM setting WHERE key = 'processing_mode'")
    assert ingest.ask_first(conn)


def test_new_recording_waits_for_approval(conn, cfg):
    ask_mode(conn)
    rec = new_recording(conn, cfg)
    assert conn.execute("SELECT count(*) FROM job").fetchone()[0] == 0  # nothing runs
    assert conn.execute("SELECT status FROM recording WHERE id = ?", (rec,)).fetchone()[0] == "awaiting_approval"
    item = conn.execute("SELECT * FROM review_item WHERE kind = 'approval'").fetchone()
    payload = json.loads(item["payload"])
    assert payload["recording_id"] == rec and "ai_cost" in payload and "m14_minutes" in payload
    assert conn.execute("SELECT count(*) FROM notification").fetchone()[0] == 0   # Brian 2026-11-21: no "waiting for your OK" note


def test_private_in_the_name_is_an_ordinary_recording(conn, cfg):
    ask_mode(conn)   # round 7 (Brian, 2026-11-21): no more Private
    new_recording(conn, cfg, "Private chat.m4a")
    payload = json.loads(conn.execute("SELECT payload FROM review_item").fetchone()[0])
    assert not payload["private"]


def web(cfg):
    return TestClient(create_app(cfg, run=lambda *a, **k: None))


def test_approve_transcript_only_queues_job_without_ai(conn, cfg):
    ask_mode(conn)
    rec = new_recording(conn, cfg)
    rid = conn.execute("SELECT id FROM review_item").fetchone()[0]
    assert web(cfg).post(f"/review/{rid}/approve", data={"ai": "0"}).status_code in (200, 303)
    job = conn.execute("SELECT * FROM job").fetchone()
    assert job["recording_id"] == rec and json.loads(job["payload"])["ai"] is False
    assert conn.execute("SELECT status FROM review_item").fetchone()[0] == "done"


def test_skip_leaves_recording_unprocessed(conn, cfg):
    ask_mode(conn)
    rec = new_recording(conn, cfg)
    rid = conn.execute("SELECT id FROM review_item").fetchone()[0]
    web(cfg).post(f"/review/{rid}/dismiss")
    assert conn.execute("SELECT count(*) FROM job").fetchone()[0] == 0
    assert conn.execute("SELECT status FROM recording WHERE id = ?", (rec,)).fetchone()[0] == "declined"


def test_review_page_lists_waiting_recordings(conn, cfg):
    ask_mode(conn)
    new_recording(conn, cfg)
    page = web(cfg).get("/inbox").text
    assert "1 waiting" in page and ">Transcript only<" in page and ">AI notes<" in page   # redesign 2026-11-21: in the row's sheet


def test_transcript_only_skips_ai_stage(conn, cfg, monkeypatch):
    from scribe_bot.pipeline import recording
    called = []
    monkeypatch.setattr("scribe_bot.ai.extract.extract_conversation", lambda c, i: called.append(i) or {"status": "done"})
    conn.execute("INSERT INTO conversation (title, created_at, updated_at) VALUES ('x', 'now', 'now')")
    ctx = queue.StageContext(1, None, {"ai": False}, cfg.data_dir, {"file": "1"}, conn)
    monkeypatch.setattr(recording, "_conversation_id", lambda ctx: 1)
    assert recording.ai_extract(ctx) == "skipped_transcript_only" and called == []
