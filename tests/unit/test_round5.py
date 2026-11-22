"""Brian's round 5 (2026-11-21): short names and short due dates for lists, Process at a set time, voices not named
yet kept out of the People list, the Q1 task pop-up. Synthetic data only."""
import json
from datetime import datetime, timedelta, timezone

from scribe_bot.ai import shortnames
from scribe_bot.core import db
from scribe_bot.pipeline import control
from scribe_bot.web.app import short_due
from test_web_pages import env, one  # noqa: F401


def test_short_due_dates():
    assert short_due("before November 15th") == "by Nov 15"
    assert short_due("for the next four to six weeks") == "4–6 wks"
    assert short_due("mid-July at the earliest") == "mid-Jul+"
    assert short_due("2026-11-28").startswith("due ")
    assert short_due(None) == ""


def _waiting(conn, rec_id=81):
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, status, title, duration_s) "
                 "VALUES (?, 'recorder', '/x/a.m4a', ?, ?, 'awaiting_approval', 'Nov 4 at 10_00', 600)", (rec_id, f"s{rec_id}", db.now()))
    return conn.execute("INSERT INTO review_item (kind, ref_id, payload, created_at) VALUES ('approval', ?, '{}', ?)",
                        (rec_id, db.now())).lastrowid


def test_process_at_waits_then_sends_it_by_itself(env, monkeypatch):
    client, conn, _ = env
    rid = _waiting(conn)
    sent = []
    monkeypatch.setattr("scribe_bot.pipeline.ingest.approve", lambda c, rec, ai=True, meeting_type=None: sent.append((rec, ai)))
    at = (datetime.now() + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")
    assert client.post(f"/review/{rid}/process-at", data={"when": "pick", "at": at, "ai": "0"}, follow_redirects=False).status_code in (200, 303)
    p = json.loads(one(conn, "SELECT payload FROM review_item WHERE id = ?", rid))
    assert p["process_ai"] == 0 and p["process_at"]
    assert control.process_due(conn) == [] and not sent                       # not yet
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    assert control.process_due(conn, now=later) == [81] and sent == [(81, False)]
    assert one(conn, "SELECT status FROM review_item WHERE id = ?", rid) == "done"
    assert one(conn, "SELECT private FROM recording WHERE id = 81") == 0     # round 7: no more Private


def test_process_at_can_be_cancelled(env):
    client, conn, _ = env
    rid = _waiting(conn, 82)
    client.post(f"/review/{rid}/process-at", data={"when": "tonight", "ai": "1"})
    client.post(f"/review/{rid}/process-at/cancel")
    assert "process_at" not in json.loads(one(conn, "SELECT payload FROM review_item WHERE id = ?", rid))
    assert one(conn, "SELECT snoozed_until FROM review_item WHERE id = ?", rid) is None


def test_unnamed_voices_wait_in_one_row(env):
    client, conn, _ = env
    sid = one(conn, "SELECT min(id) FROM speaker")
    conn.execute("UPDATE speaker SET person_id = NULL WHERE id = ?", (sid,))
    conn.execute("INSERT INTO person (name, stands_for_speaker, created_at) VALUES ('Speaker 9 from Budget sync on Oct 20', ?, ?)", (sid, db.now()))
    page = client.get("/people").text
    assert "not named yet" in page and f'/speakers/{sid}/name' in page
    assert page.count("Speaker 9 from Budget sync") == 0                       # not mixed into the list


def test_lists_show_the_short_name_and_the_pop_up_the_full_one(env):
    client, conn, _ = env
    tid = one(conn, "SELECT min(id) FROM task")
    conn.execute("UPDATE task SET title = 'Put together the very long certification form with Sam', short_title = 'Certification form', "
                 "status = 'open', owner_person_id = 1 WHERE id = ?", (tid,))
    page = client.get("/tasks").text
    row = page.split(f'id="task-{tid}"')[1].split('class="fx-sheet task-sheet"')[0]
    assert '<div class="item-title">Certification form</div>' in row         # the full one stays in aria-label and the pop-up
    assert "very long certification" in page.split(f'id="ts-{tid}"')[1]


def test_short_names_backlog_and_rename(env):
    client, conn, _ = env
    cid = one(conn, "SELECT min(id) FROM conversation WHERE summary IS NOT NULL")
    conn.execute("UPDATE conversation SET short_title = NULL, private = 0 WHERE id = ?", (cid,))
    assert cid in shortnames.backlog(conn)
    client.post(f"/conversations/{cid}/summary", data={"title": "Budget"})
    assert one(conn, "SELECT short_title FROM conversation WHERE id = ?", cid) == "Budget"
    assert cid not in shortnames.backlog(conn)


def test_short_names_come_from_one_small_call(env, monkeypatch):
    _, conn, _ = env
    from scribe_bot.ai import router
    tid = one(conn, "SELECT min(id) FROM task WHERE conversation_id = 1")
    sent = {}

    class Call:
        text = '{"talk": "Budget sync.", "tasks": {"%d": "Send the deck"}}' % tid
        cost_usd = 0.0002

    def fake(conn_, task, system, user, **k):
        sent["user"] = user
        return Call()
    monkeypatch.setattr(router, "run", fake)
    conn.execute("UPDATE conversation SET private = 0, short_title = NULL WHERE id = 1")
    assert shortnames.shorten(conn, 1)["status"] == "done"
    assert one(conn, "SELECT short_title FROM conversation WHERE id = 1") == "Budget sync"
    assert one(conn, "SELECT short_title FROM task WHERE id = ?", tid) == "Send the deck"
    assert "transcript" not in sent["user"].lower() and json.loads(sent["user"])["talk"]   # titles only
    conn.execute("UPDATE conversation SET private = 1, short_title = NULL WHERE id = 1")
    assert shortnames.shorten(conn, 1)["status"] == "skipped"                            # private talks never sent


def test_a_cut_off_reply_still_keeps_the_talk_name(env, monkeypatch):
    _, conn, _ = env
    from scribe_bot.ai import router
    tid = one(conn, "SELECT min(id) FROM task WHERE conversation_id = 1")

    class Call:
        text = '{"talk": "Venue follow-up", "tasks": {"%d": "Book venue", "99999": "Half a sent' % tid
        cost_usd = 0.0001
    monkeypatch.setattr(router, "run", lambda *a, **k: Call())
    conn.execute("UPDATE conversation SET private = 0, short_title = NULL WHERE id = 1")
    shortnames.shorten(conn, 1)
    assert one(conn, "SELECT short_title FROM conversation WHERE id = 1") == "Venue follow-up"
    assert one(conn, "SELECT short_title FROM task WHERE id = ?", tid) == "Book venue"
