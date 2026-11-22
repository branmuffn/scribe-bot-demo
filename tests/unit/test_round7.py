"""Brian's round 7 (2026-11-21): no more Private (a talk without AI notes is just "Transcript only", and Make AI notes
works on it), no long-recording Discord message. Synthetic data only."""
import json

from scribe_bot.core import db
from scribe_bot.pipeline import control
from test_web_pages import env, one  # noqa: F401


def _waiting(conn, rec_id=91, title="call_private Nov 4"):
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, status, title, duration_s) "
                 "VALUES (?, 'recorder', '/x/a.m4a', ?, ?, 'awaiting_approval', ?, 600)", (rec_id, f"s{rec_id}", db.now(), title))
    return conn.execute("INSERT INTO review_item (kind, ref_id, payload, created_at) VALUES ('approval', ?, '{}', ?)",
                        (rec_id, db.now())).lastrowid


def test_transcript_only_no_longer_locks_anything(env, monkeypatch):
    client, conn, _ = env
    rid = _waiting(conn)
    monkeypatch.setattr("scribe_bot.pipeline.ingest.approve", lambda *a, **k: None)
    item = conn.execute("SELECT * FROM review_item WHERE id = ?", (rid,)).fetchone()
    control.approve_recording(conn, item, ai=False)
    assert one(conn, "SELECT private FROM recording WHERE id = 91") == 0


def test_no_private_wording_or_mark_private(env):
    client, conn, _ = env
    cid = one(conn, "SELECT id FROM conversation ORDER BY id LIMIT 1")
    page = client.get(f"/conversations/{cid}").text
    assert "Mark private" not in page and "🔒" not in page
    assert client.post(f"/conversations/{cid}/private").status_code in (404, 405)
    assert "private" not in client.get("/settings").text.lower().replace("cache-control: private", "")


def test_migration_clears_old_private_marks(env):
    client, conn, _ = env
    conn.execute("UPDATE conversation SET private = 1")
    sql = open("scribe_bot/core/migrations/0030_no_private.sql").read()
    conn.executescript(sql)
    assert one(conn, "SELECT count(*) FROM conversation WHERE private = 1") == 0


def test_make_ai_notes_on_a_transcript_only_talk(env):
    client, conn, _ = env
    cid = one(conn, "SELECT id FROM conversation ORDER BY id LIMIT 1")
    conn.execute("UPDATE conversation SET summary = NULL WHERE id = ?", (cid,))
    r = client.get(f"/conversations/{cid}/rerun/confirm")
    assert r.status_code == 200 and "Make AI notes?" in r.text and "Private" not in r.text
    r = client.post(f"/conversations/{cid}/rerun", data={"confirm": "1"}, follow_redirects=False)
    assert r.status_code == 303
    assert any(json.loads(p[0]).get("conversation_id") == cid for p in conn.execute("SELECT payload FROM job"))
