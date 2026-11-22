"""Voice memos from a computer (2026-11-20): several files per upload, a same-name .txt is the Inbox transcript,
and "Add transcript" on a waiting recording (paste or .txt) wins over a synced one (synthetic only)."""
import json

from scribe_bot.core import db
from test_web_pages import env, one  # noqa: F401


def waiting(conn, folder, rid=20, name="Venue chat _ Oct 14 at 11_30"):
    t = db.now()
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, duration_s, received_at, status, title) "
                 "VALUES (?, 'recorder', ?, ?, 600, ?, 'awaiting_approval', ?)", (rid, str(folder / f"{name}.m4a"), f"s{rid}", t, name))
    conn.execute("INSERT INTO review_item (kind, ref_id, payload, created_at) VALUES ('approval', ?, ?, ?)",
                 (rid, json.dumps({"recording_id": rid, "title": name, "minutes": 10, "ai_cost": 0.01}), t))


def test_add_and_replace_transcript_on_a_waiting_recording(env, tmp_path):
    client, conn, cfg = env
    folder = tmp_path / "Recordings"; folder.mkdir()
    (folder / "Venue chat - Oct 14 at 11-30.txt").write_text("The venue budget venue budget.\n", encoding="utf-8")
    waiting(conn, folder)
    page = client.get("/inbox").text
    assert 'action="/inbox/20/transcript"' in page and "＋ Transcript" in page and "“Venue, budget”" in page
    r = client.post("/inbox/20/transcript", data={"text": "Garden plans garden plans and the garden shed."}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/inbox#rec-20"
    page = client.get("/inbox").text
    assert "“Garden" in page and "Venue, budget" not in page                 # the added one wins
    client.post("/inbox/20/transcript", files={"file": ("memo.txt", b"Piano piano lesson notes piano.", "text/plain")})
    assert "“Piano" in client.get("/inbox").text
    assert client.post("/inbox/20/transcript", data={"text": " "}).status_code == 400
    assert client.post("/inbox/20/transcript", files={"file": ("x.exe", b"MZ", "application/octet-stream")}).status_code == 400
    assert client.post("/inbox/999/transcript", data={"text": "x"}).status_code == 404


def test_upload_several_files_and_a_matching_txt(env, tmp_path):
    client, conn, cfg = env
    r = client.post("/capture/upload", follow_redirects=False, files=[
        ("file", ("Lunch talk.m4a", b"audio-1", "audio/mp4")), ("file", ("Walk notes.m4a", b"audio-2", "audio/mp4")),
        ("file", ("Lunch talk.txt", b"Soup soup and the soup menu.", "text/plain"))])
    assert r.status_code == 303 and "saved=audio" in r.headers["location"] and "n=2" in r.headers["location"]
    up = cfg.data_dir / "uploads"
    assert sorted(p.name for p in up.iterdir()) == ["Lunch talk.m4a", "Lunch talk.txt", "Walk notes.m4a"]
    waiting(conn, up, rid=30, name="Lunch talk")
    assert "“Soup" in client.get("/inbox").text
    r = client.post("/capture/upload", follow_redirects=False, files=[("file", ("Only.txt", b"x", "text/plain"))])
    assert "saved=transcript" in r.headers["location"]


def memo_m4a(path, words):
    """A tiny synthetic Apple Voice Memos file: ftyp, mdat, then moov/trak/udta/tsrp with the transcript JSON."""
    import struct
    def box(kind, body):
        return struct.pack(">I4s", 8 + len(body), kind) + body
    runs, table = [], []
    for i, (w, t) in enumerate(words):
        runs += [w, i]
        table.append({"timeRange": [t, t + 0.4]})
    tsrp = json.dumps({"locale": {"identifier": "en_US"}, "attributedString": {"runs": runs, "attributeTable": table}}).encode()
    path.write_bytes(box(b"ftyp", b"M4A \x00\x00\x00\x00") + box(b"mdat", b"\x00" * 64) +
                     box(b"moov", box(b"mvhd", b"\x00" * 20) + box(b"trak", box(b"udta", box(b"tsrp", tsrp)))))


def test_voice_memo_transcript_is_read_from_inside_the_file(env, tmp_path):
    from pathlib import Path
    from scribe_bot.pipeline import voicememo
    from scribe_bot.web.routes import recorded_at, recorder_title
    client, conn, cfg = env
    up = cfg.data_dir / "uploads"; up.mkdir(parents=True, exist_ok=True)
    words = [("Garden ", 0.0), ("plans ", 0.5), ("and ", 1.0), ("the ", 1.2), ("garden ", 1.5), ("shed. ", 50.0), ("Garden ", 60.0), ("tools.", 61.0)]
    memo_m4a(up / "20261113 201200.m4a", words)
    assert voicememo.transcript(up / "20261113 201200.m4a") == "Garden plans and the garden shed.\n\nGarden tools."
    assert voicememo.words(up / "nothing.m4a") is None
    waiting(conn, up, rid=40, name="20261113 201200")
    page = client.get("/inbox").text
    assert "“Garden" in page and "Voice Memos" in page and "Nov 13 at 20:12" in page and "/inbox/40/transcript\"" in page
    assert "Garden plans and the garden shed." in client.get("/inbox/40/transcript").text
    assert client.get("/inbox/40/transcript.txt").text.startswith("Garden plans")
    assert recorder_title("20261113 201200") == "Nov 13 at 20:12"
    assert recorded_at("20261113 201200", None).strftime("%Y-%m-%d %H:%M") == "2026-11-13 20:12"
    from scribe_bot.pipeline import peek
    peek._last = 0
    assert peek.run_one(conn, child=False) == 40                                     # quick title with no speech work
    assert "Garden" in one(conn, "SELECT text FROM recording_peek WHERE recording_id = 40")


def test_short_memo_flat_layout_and_odd_data_never_break_the_inbox(env, tmp_path):
    """A 7-second memo (2026-11-20) stored attributedString as a flat list [text, {timeRange}, ...]: it crashed /inbox."""
    import struct
    from scribe_bot.pipeline import voicememo
    client, conn, cfg = env
    up = cfg.data_dir / "uploads"; up.mkdir(parents=True, exist_ok=True)
    def box(kind, body):
        return struct.pack(">I4s", 8 + len(body), kind) + body
    def write(name, doc):
        tsrp = json.dumps(doc).encode()
        (up / name).write_bytes(box(b"ftyp", b"M4A \x00\x00\x00\x00") + box(b"moov", box(b"trak", box(b"udta", box(b"tsrp", tsrp)))))
    write("20261110 134647.m4a", {"locale": {"identifier": "en_US"},
                                  "attributedString": ["Quick ", {"timeRange": [0, 0.66]}, "note.", {"timeRange": [4.4, 6.9]}]})
    assert voicememo.words(up / "20261110 134647.m4a") == [("Quick ", 0.0), ("note.", 4.4)]
    write("20261110 134648.m4a", {"attributedString": "odd"})
    assert voicememo.words(up / "20261110 134648.m4a") is None
    waiting(conn, up, rid=41, name="20261110 134647")
    page = client.get("/inbox")
    assert page.status_code == 200 and "Quick note." in client.get("/inbox/41/transcript").text


def test_filter_panel_several_filters_and_chips(env, tmp_path):
    """FX-A (2026-11-20): Inbox filters combine; each one on shows as a chip that removes it; old ?show= links work."""
    client, conn, _ = env
    folder = tmp_path / "Recordings"; folder.mkdir()
    (folder / "Long one.txt").write_text("Budget budget plan plan.", encoding="utf-8")
    t = db.now()
    for rid, name, secs in ((50, "Long one", 1800), (51, "Long two", 1500), (52, "Short one", 60)):
        conn.execute("INSERT INTO recording (id, source, original_path, sha256, duration_s, received_at, status, title) "
                     "VALUES (?, 'recorder', ?, ?, ?, ?, 'awaiting_approval', ?)", (rid, str(folder / f"{name}.m4a"), f"f{rid}", secs, t, name))
        conn.execute("INSERT INTO review_item (kind, ref_id, payload, created_at) VALUES ('approval', ?, ?, ?)",
                     (rid, json.dumps({"recording_id": rid, "title": name, "minutes": secs // 60, "ai_cost": 0.01}), t))
    page = client.get("/inbox?length=long&has=transcript").text
    assert 'id="rec-50"' in page and 'id="rec-51"' not in page and 'id="rec-52"' not in page
    assert "⚙ Filter · 2" in page and 'href="/inbox?has=transcript"' in page and "Long (20+ min) ✕" in page
    assert 'id="rec-51"' in client.get("/inbox?show=long").text and 'id="rec-52"' not in client.get("/inbox?show=long").text
    talks = client.get("/conversations?type=1:1").text
    assert "⚙ Filter · 1" in talks and "1:1 ✕" in talks and "Export the talks shown" in talks and "Notes + transcripts" in talks and "transcript=1" in talks


def test_four_talk_types_only():
    """Brian, 2026-11-20: talk types are 1:1, Group, Learning and Personal; a one-voice recording is Personal."""
    from scribe_bot.ai import prompts
    assert list(prompts.MEETING_TYPES) == ["1:1", "multi", "learning", "personal"]
    assert set(prompts.TEMPLATES) == set(prompts.MEETING_TYPES)
