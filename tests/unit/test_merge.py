"""Merge before processing (Brian, 2026-11-21): waiting recordings join into one, labeled by part; Unmerge undoes it
while nothing has run (synthetic audio only)."""
import json
import wave

from scribe_bot.core import db
from scribe_bot.pipeline import merge, speech
from test_web_pages import env, one  # noqa: F401



def tone(path, secs):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b"\x00\x00" * int(16000 * secs))


def setup(conn, tmp_path, monkeypatch):
    lens = {}
    for rid, name, secs, when in ((71, "Oct 14 at 11_30", 3, "2026-10-14"), (72, "Oct 14 at 11_45", 2, "2026-10-14")):
        f = tmp_path / f"{name}.m4a"; f.write_bytes(f"audio {rid}".encode()); lens[str(f)] = secs
        conn.execute("INSERT INTO recording (id, source, original_path, sha256, duration_s, received_at, status, title) "
                     "VALUES (?, 'recorder', ?, ?, ?, ?, 'awaiting_approval', ?)", (rid, str(f), f"m{rid}", secs, db.now(), name))
        conn.execute("INSERT INTO review_item (kind, ref_id, payload, created_at) VALUES ('approval', ?, ?, ?)",
                     (rid, json.dumps({"recording_id": rid, "title": name, "minutes": secs / 60, "ai_cost": 0.01}), db.now()))
    monkeypatch.setattr(merge, "_to_wav", lambda src, dst: tone(dst, lens[str(src)]))
    monkeypatch.setattr(merge, "_encode", lambda wav, dst: dst.write_bytes(wav.read_bytes()))
    monkeypatch.setattr(speech, "_engine", type("E", (), {"duration": lambda self, p: 5.0})())   # undone after the test


def test_merge_then_unmerge(env, tmp_path, monkeypatch):
    client, conn, cfg = env
    setup(conn, tmp_path, monkeypatch)
    r = client.post("/inbox/merge", data={"ids": "72,71"}, follow_redirects=False)
    assert r.status_code == 303
    new = one(conn, "SELECT max(id) FROM recording")
    parts = conn.execute("SELECT source_id, offset_s, duration_s FROM recording_part WHERE recording_id = ? ORDER BY idx", (new,)).fetchall()
    assert [tuple(p) for p in parts] == [(71, 0.0, 3.0), (72, 3.0, 2.0)]                 # in recorded order, with offsets
    assert one(conn, "SELECT title FROM recording WHERE id = ?", new) == "Oct 14 at 11_30 + Oct 14 at 11_45"
    assert {r[0] for r in conn.execute("SELECT status FROM recording WHERE id IN (71, 72)")} == {"merged"}
    page = client.get("/inbox").text
    assert f'id="rec-{new}"' in page and 'id="rec-71"' not in page and "2 parts" in page and "Unmerge" in page
    assert client.post("/inbox/merge", data={"ids": "71"}).status_code == 400              # one isn't a merge
    client.post(f"/inbox/{new}/unmerge")
    assert one(conn, "SELECT count(*) FROM recording WHERE id = ?", new) == 0
    assert {r[0] for r in conn.execute("SELECT status FROM recording WHERE id IN (71, 72)")} == {"awaiting_approval"}
    assert one(conn, "SELECT count(*) FROM review_item WHERE kind = 'approval' AND status = 'open' AND ref_id IN (71, 72)") == 2
    assert 'id="rec-71"' in client.get("/inbox").text


def test_merged_talk_shows_its_parts(env, tmp_path, monkeypatch):
    client, conn, cfg = env
    rec = one(conn, "SELECT recording_id FROM conversation WHERE id = 1")
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, status, title) VALUES (81, 'recorder', '/x', 'p81', ?, 'merged', 'First part')", (db.now(),))
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, status, title) VALUES (82, 'recorder', '/y', 'p82', ?, 'merged', 'Second part')", (db.now(),))
    for i, (src, off) in enumerate(((81, 0), (82, 2))):
        conn.execute("INSERT INTO recording_part (recording_id, source_id, idx, offset_s, duration_s, title, prev_status) VALUES (?, ?, ?, ?, 2, ?, 'awaiting_approval')",
                     (rec, src, i, off, "First part" if i == 0 else "Second part"))
    page = client.get("/conversations/1?tx=1").text
    assert "Part 1 of 2" in page and "Part 2 of 2" in page and "2 parts" in page
