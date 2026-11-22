"""Brian's round 4 (2026-11-21): Today crash on worded due dates, Pause / Resume / Cancel processing, Processing history,
quick titles for talks without AI notes, People select (merge / remove), undoable Skip, stable speaker colors.
Synthetic data only."""
import json
from datetime import datetime

from scribe_bot.core import db, queue
from scribe_bot.pipeline import control, recording
from scribe_bot.web.routes import next_up
from scribe_bot.worker import resume_if_due
from test_web_pages import env, one  # noqa: F401


def test_today_survives_a_due_date_written_in_words(env):
    client, conn, _ = env
    tid = one(conn, "SELECT min(id) FROM task")
    conn.execute("UPDATE task SET due = 'before Nov 15th', remind_at = '2020-01-01T00:00:00+00:00', status = 'open', "
                 "owner_person_id = 1 WHERE id = ?", (tid,))
    assert client.get("/").status_code == 200
    card = next_up([], {"reminders": [{"id": 1, "title": "T", "due": "before Nov 15th", "conv_title": None,
                                       "conversation_id": None}], "speakers": []}, 0, "2026-11-21")
    assert card["sub"] == "due before Nov 15th"


def test_a_paused_stage_goes_back_to_waiting_without_using_an_attempt(env, tmp_path):
    _, conn, _ = env
    calls = []

    def stage(ctx):
        calls.append(1)
        if len(calls) == 1:
            raise queue.Paused()
        return "ok"
    job = queue.enqueue(conn, "t")
    assert queue.run_next(conn, {"t": [("one", stage)]}, tmp_path) == job
    assert one(conn, "SELECT status FROM job WHERE id = ?", job) == "pending"
    assert one(conn, "SELECT count(*) FROM stage_run WHERE job_id = ?", job) == 0
    queue.run_next(conn, {"t": [("one", stage)]}, tmp_path)
    assert one(conn, "SELECT status FROM job WHERE id = ?", job) == "done"


def test_transcribing_resumes_from_the_saved_pieces(tmp_path, monkeypatch):
    class Eng:
        pausable = True

        def load(self, wav):
            return "samples"

        def transcribe(self, samples, done=None, stop=None, save=None):
            out = list(done or [])
            for start in (0.0, 28.0, 56.0):
                if any(x["start"] == start for x in out):
                    continue
                if stop():
                    from scribe_bot.pipeline.speech import Stopped
                    raise Stopped()
                out.append({"start": start, "text": f"piece {start}"})
                save(out)
                if start == 28.0:
                    flag["hold"] = True
            return out
    flag = {"hold": False}
    monkeypatch.setattr(recording.speech, "engine", lambda: Eng())
    monkeypatch.setattr(recording, "_wav", lambda ctx: tmp_path / "a.wav")
    monkeypatch.setattr(recording.queue, "hold_requested", lambda conn: flag["hold"])
    ctx = queue.StageContext(1, 1, {}, tmp_path, {}, None)
    try:
        recording.transcribe(ctx)
        raise AssertionError("should pause")
    except queue.Paused:
        pass
    assert len(json.loads((tmp_path / "transcript.partial.json").read_text())) == 2
    flag["hold"] = False
    out = json.loads(open(recording.transcribe(ctx)).read())
    assert [x["start"] for x in out] == [0.0, 28.0, 56.0] and not (tmp_path / "transcript.partial.json").exists()


def test_pause_resume_tonight_and_cancel(env):
    client, conn, cfg = env
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, status, title) "
                 "VALUES (70, 'recorder', '/x/70.m4a', 's70', ?, 'processing', 'Nov 3 at 10_00')", (db.now(),))
    conn.execute("INSERT INTO review_item (kind, ref_id, payload, status, created_at) VALUES ('approval', 70, '{}', 'done', ?)", (db.now(),))
    job = queue.enqueue(conn, "recording", 70)
    conn.execute("INSERT INTO conversation (recording_id, title, status, created_at, updated_at) VALUES (70, 'X', 'transcribing', ?, ?)",
                 (db.now(), db.now()))
    (cfg.jobs_dir / str(job)).mkdir(parents=True, exist_ok=True)
    client.post("/processing/pause")
    assert control.state(conn)["paused"]
    page = client.get("/processing").text
    assert "Processing history" in page and "paused" in page
    client.post("/processing/resume", data={"when": "tonight"})
    st = control.state(conn)
    assert st["paused"] and st["resume_at"]
    assert not resume_if_due(conn, "2000-01-01T00:00:00+00:00") and resume_if_due(conn, "2999-01-01T00:00:00+00:00")
    assert not control.state(conn)["paused"]
    assert control.tonight(datetime(2026, 11, 21, 23, 30).astimezone()).day == 22
    client.post("/processing/pause")
    r = client.post(f"/processing/{job}/cancel", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/inbox#rec-70"
    assert one(conn, "SELECT status FROM recording WHERE id = 70") == "awaiting_approval"
    assert one(conn, "SELECT count(*) FROM job WHERE id = ?", job) == 0
    assert one(conn, "SELECT count(*) FROM conversation WHERE recording_id = 70") == 0
    assert one(conn, "SELECT status FROM review_item WHERE ref_id = 70") == "open"
    assert not (cfg.jobs_dir / str(job)).exists()


def test_talks_without_ai_notes_get_a_quick_title_and_one_short_card(env):
    client, conn, _ = env
    conn.execute("UPDATE conversation SET title = 'Conversation', summary = NULL, private = 1, status = 'transcribed' WHERE id = 1")
    conn.execute("DELETE FROM task WHERE conversation_id = 1")
    conn.execute("DELETE FROM decision WHERE conversation_id = 1")
    conn.execute("DELETE FROM person_fact WHERE conversation_id = 1")
    page = client.get("/conversations/1").text
    assert 'id="sec-plain"' in page and 'id="sec-summary"' not in page and 'id="sec-decisions"' not in page
    conn.execute("UPDATE segment SET text = 'The venue budget and the venue contract for the kickoff' WHERE conversation_id = 1")
    assert recording.retitle_plain(conn) >= 1 and one(conn, "SELECT title FROM conversation WHERE id = 1") != "Conversation"


def test_people_select_merge_and_remove(env):
    client, conn, _ = env
    t = db.now()
    a = conn.execute("INSERT INTO person (name, area, created_at) VALUES ('Zed One', 'both', ?)", (t,)).lastrowid
    b = conn.execute("INSERT INTO person (name, area, created_at) VALUES ('Zed Two', 'both', ?)", (t,)).lastrowid
    c = conn.execute("INSERT INTO person (name, area, created_at) VALUES ('Zed Three', 'both', ?)", (t,)).lastrowid
    people = client.get("/people").text
    assert "data-picker" in people and f'data-pick-id="{a}"' in people and 'data-pk-go="/people/merge-many"' in people
    page = client.get(f"/people/merge-many?ids={a},{b}").text
    assert "Merge 2 people" in page and "Zed Two" in page
    client.post("/people/merge-many", data={"ids": f"{a},{b}", "into": a})
    assert one(conn, "SELECT count(*) FROM person WHERE id = ?", b) == 0
    client.post("/people/delete-many", data={"ids": f"{a},{c},1"})
    assert one(conn, "SELECT count(*) FROM person WHERE id IN (?, ?)", a, c) == 0 and one(conn, "SELECT count(*) FROM person WHERE id = 1") == 1
    uid = one(conn, "SELECT max(id) FROM undo_action")
    client.post(f"/undo/{uid}")
    assert one(conn, "SELECT count(*) FROM person WHERE id IN (?, ?)", a, c) == 2


def test_inbox_skip_can_be_undone(env):
    client, conn, _ = env
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, status, title) "
                 "VALUES (71, 'recorder', '/x/71.m4a', 's71', ?, 'awaiting_approval', 'Nov 4 at 10_00')", (db.now(),))
    rid = conn.execute("INSERT INTO review_item (kind, ref_id, payload, created_at) VALUES ('approval', 71, '{}', ?)", (db.now(),)).lastrowid
    client.post(f"/review/{rid}/dismiss")
    assert one(conn, "SELECT status FROM recording WHERE id = 71") == "declined"
    client.post(f"/undo/{one(conn, 'SELECT max(id) FROM undo_action')}")
    assert one(conn, "SELECT status FROM recording WHERE id = 71") == "awaiting_approval"
    assert one(conn, "SELECT status FROM review_item WHERE id = ?", rid) == "open"


def test_speaker_colors_stay_put_when_who_talks_first_changes(env):
    client, conn, _ = env
    from scribe_bot.web.routes import speakers_of
    before = {s["id"]: s["color"] for s in speakers_of(conn, 1)}
    first = one(conn, "SELECT speaker_id FROM segment WHERE conversation_id = 1 ORDER BY idx LIMIT 1")
    other = one(conn, "SELECT speaker_id FROM segment WHERE conversation_id = 1 AND speaker_id != ? LIMIT 1", first)
    conn.execute("UPDATE segment SET speaker_id = ? WHERE conversation_id = 1 AND idx = (SELECT min(idx) FROM segment WHERE conversation_id = 1)", (other,))
    after = {s["id"]: s["color"] for s in speakers_of(conn, 1)}
    assert all(after[k] == before[k] for k in after if k in before)


def test_rail_page_has_no_bare_processing_link_and_history_is_in_the_menu(env):
    client, conn, _ = env
    assert 'href="/processing"' in client.get("/more").text
    assert "Processing history" in client.get("/help").text and "What each button does" in client.get("/help").text


def test_inbox_length_and_date_ranges(env):
    client, conn, _ = env
    t = "2026-11-10T15:00:00+00:00"
    for rid, secs in ((80, 120), (81, 1800), (82, 5400)):
        conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, status, title, duration_s) "
                     "VALUES (?, 'recorder', ?, ?, ?, 'awaiting_approval', ?, ?)", (rid, f"/x/{rid}.m4a", f"s{rid}", t, f"Rec {rid}", secs))
        conn.execute("INSERT INTO review_item (kind, ref_id, payload, created_at) VALUES ('approval', ?, ?, ?)",
                     (rid, json.dumps({"recording_id": rid, "title": f"Rec {rid}", "minutes": secs / 60}), t))
    page = client.get("/inbox?min_m=10&max_m=60").text
    assert 'id="rec-81"' in page and 'id="rec-80"' not in page and 'id="rec-82"' not in page and "10–60 min" in page
    assert 'id="rec-81"' not in client.get("/inbox?start=2030-01-01").text
    assert 'name="min_m"' in page and 'type="range"' in page


def test_tags_add_filter_group_and_ai_picks_only_existing(env):
    client, conn, _ = env
    from scribe_bot.ai import prompts
    from scribe_bot.core import tags
    client.post("/conversations/1/tags", data={"name": "Budget, Venue"})
    mine = {t["name"]: t for t in tags.of(conn, 1)}
    assert set(mine) == {"Budget", "Venue"}
    assert "#Budget" in client.get("/conversations/1").text
    page = client.get(f"/conversations?tag={mine['Budget']['id']}").text
    assert 'href="/conversations/1"' in page and "# Budget ✕" in page
    assert "# Budget" in client.get("/conversations?order=tag").text
    assert tags.add(conn, 1, "Brand new", by_ai=True) is None            # the AI never makes a new tag
    sys = prompts.extract_system("Brian", tags=tags.names(conn))
    assert '"Budget"' in sys and "never make up a new one" in sys
    assert prompts.parse_extract('{"summary": "x", "decisions": [], "tasks": [], "waiting_on": [], "people_facts": [], "tags": ["Budget", 5, ""]}')["tags"] == ["Budget"]
    client.post(f"/conversations/1/tags/{mine['Venue']['id']}/remove")
    assert "Venue" not in tags.names(conn)


def test_detailed_notes_on_request(env, monkeypatch):
    client, conn, _ = env
    from scribe_bot.ai import extract, jobs, router

    class Call:
        text = "Sure, here are the notes:\n## Budget\n- Venue costs $5k\n- <b>raw</b> stays text\n\nThanks!"
        cost_usd = 0.01
    monkeypatch.setattr(router, "run", lambda *a, **k: Call())
    conn.execute("UPDATE conversation SET private = 0 WHERE id = 1")
    assert "Make detailed notes?" in client.get("/conversations/1/detail/confirm").text
    r = client.post("/conversations/1/detail", data={"confirm": "1"}, follow_redirects=False)
    assert r.headers["location"] == "/conversations/1?notes=detailed" and jobs.detail_pending(conn, 1)
    assert extract.detail_conversation(conn, 1)["status"] == "done"
    saved = one(conn, "SELECT detailed FROM conversation WHERE id = 1")
    assert saved.startswith("## Budget") and "Thanks" not in saved and "Sure" not in saved
    page = client.get("/conversations/1?notes=detailed").text
    assert "<h3>Budget</h3>" in page and "&lt;b&gt;raw&lt;/b&gt;" in page and 'data-notes="detailed"' in page
