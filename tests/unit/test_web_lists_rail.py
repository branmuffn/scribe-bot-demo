"""Evening of 2026-11-20 (Brian): word fixes save in the background, list pages get date headings (one rail tick each),
Talks › Select › Export just the picked talks, the red floating +, and "Show everything" at the top (synthetic only)."""
from datetime import datetime, timedelta, timezone

from scribe_bot.web.app import date_group, due_group, group_phrase
from test_web_pages import env, one  # noqa: F401


def test_date_groups():
    now = datetime(2026, 11, 20, 12, tzinfo=timezone.utc).astimezone()   # a Friday
    at = lambda d: (now - timedelta(days=d)).isoformat()  # noqa: E731
    assert date_group(at(0), now) == "This week" and date_group(at(4), now) == "This week"
    assert date_group(at(6), now) == "Last week" and date_group(at(11), now) == "Last week"
    assert date_group(at(15), now) == "Earlier in November"
    assert date_group(at(40), now) == "October" and date_group("2025-10-02T10:00:00+00:00", now) == "October 2025"
    assert date_group(None) == "No date" and date_group("junk") == "No date"
    assert group_phrase("This week", "Recorded") == "Recorded this week" and group_phrase("October", "Added") == "Added in October"
    assert due_group(None) == "No due date" and due_group("Friday") == "Rough due date" and due_group("2020-01-01") == "Overdue"


def test_word_fix_saves_in_the_background(env):
    """The fix used to reload the whole talk page and timed out on the phone; with X-Fast the reply is a few lines."""
    client, conn, _ = env
    sid = one(conn, "SELECT id FROM segment WHERE conversation_id = 1 AND idx = 1")
    r = client.post(f"/segments/{sid}/text", data={"text": "Fixed words here."}, headers={"X-Fast": "1"}, follow_redirects=False)
    j = r.json()
    assert r.status_code == 200 and j["undo"]["label"] == "Edited line 2"
    assert {x["id"]: x["text"] for x in j["lines"]}[sid] == "Fixed words here."
    assert one(conn, "SELECT text FROM segment WHERE id = ?", sid) == "Fixed words here."
    client.post(f"/undo/{j['undo']['id']}")
    assert one(conn, "SELECT text FROM segment WHERE id = ?", sid) != "Fixed words here."
    assert client.post(f"/segments/{sid}/text", data={"text": "Plain post."}, follow_redirects=False).status_code == 303


def test_export_only_the_picked_talks(env):
    client, conn, _ = env
    t = "2026-11-01T10:00:00+00:00"
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, status) VALUES (90, 'recorder', '/x/90.m4a', 's90', ?, 'done')", (t,))
    conn.execute("INSERT INTO conversation (id, recording_id, title, started_at, status, created_at, updated_at) "
                 "VALUES (90, 90, 'Second talk', ?, 'done', ?, ?)", (t, t, t))
    ids = [r[0] for r in conn.execute("SELECT id FROM conversation WHERE deleted_at IS NULL ORDER BY id")]
    assert len(ids) >= 2
    md = client.get(f"/conversations/export.md?ids={ids[0]}").text
    assert md.startswith("# Scribe Bot export · 1 conversations")
    md2 = client.get(f"/conversations/export.md?ids={ids[0]},{ids[1]},x").text
    assert md2.startswith("# Scribe Bot export · 2 conversations")
    page = client.get("/conversations").text
    assert "data-pick-mode" in page and 'id="pick-bar"' in page and 'class="fab" href="/capture"' in page


def test_lists_have_date_headings_and_talks_open_quickly(env):
    client, conn, _ = env
    assert 'class="list-head"' in client.get("/conversations").text
    cid = one(conn, "SELECT id FROM conversation WHERE summary IS NOT NULL AND summary != '' LIMIT 1")
    if cid:
        page = client.get(f"/conversations/{cid}").text
        if 'id="glance"' in page:
            assert 'class="glance-top"' in page and page.count("data-remember") >= 2


def test_school_and_other_areas(env):
    """Brian, 2026-11-21: areas are Work, Home, School and Other, everywhere Work/Home was."""
    client, conn, _ = env
    client.post("/conversations/1/area", data={"area": "school"})
    assert one(conn, "SELECT area FROM conversation WHERE id = 1") == "school"
    assert "School" in client.get("/conversations?area=school").text and 'class="badge badge-school"' in client.get("/conversations").text
    tid = one(conn, "SELECT min(id) FROM task")
    client.post(f"/tasks/{tid}/file", data={"area": "other"})
    assert one(conn, "SELECT area FROM task WHERE id = ?", tid) == "other"
    page = client.get("/").text
    assert 'href="?area=school"' in page and 'href="?area=other"' in page
    import sqlite3
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("UPDATE task SET area = 'bogus' WHERE id = ?", (tid,))
