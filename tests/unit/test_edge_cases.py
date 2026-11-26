"""Robustness (Worker 2, 2026-11-26): edge inputs, stale pages, second taps and Undo, on made-up demo data and on an
empty database. Every case answers with a page or a clear message: never a crash, never changed data it shouldn't."""
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient

from scribe_bot import demo_data
from scribe_bot.core import db
from scribe_bot.core.settings import Settings
from scribe_bot.web.app import create_app
from test_web_pages import FakeKeys, fake_pmset

HTML = {"accept": "text/html"}


@pytest.fixture(scope="module")
def demo_dir(tmp_path_factory):
    d = tmp_path_factory.mktemp("demo")
    assert demo_data.main(["x", str(d)]) == 0
    return d


@pytest.fixture
def app(demo_dir, tmp_path, monkeypatch):
    """A fresh copy of the demo data for each test (the actions below change it)."""
    d = tmp_path / "data"
    shutil.copytree(demo_dir, d)
    monkeypatch.setenv("SCRIBE_BOT_DATA", str(d))
    cfg = Settings(d)
    client = TestClient(create_app(cfg, run=fake_pmset, secrets_store=FakeKeys()), headers={"origin": "http://testserver"})
    client.db = lambda: db.connect(cfg.db_path)
    return client


@pytest.fixture
def empty(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIBE_BOT_DATA", str(tmp_path))
    cfg = Settings(tmp_path)
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    conn.close()
    return TestClient(create_app(cfg, run=fake_pmset, secrets_store=FakeKeys()), headers={"origin": "http://testserver"})


def one(client, sql, *args):
    with client.db() as conn:
        return conn.execute(sql, args).fetchone()[0]


# ---------- F70: a date that can't be, in the overnight link ----------

@pytest.mark.parametrize("night", ["2026-02-30", "2026-13-01", "0000-00-00", "9999-99-99", "1", "", "abc"])
def test_overnight_link_with_an_impossible_date_still_opens_the_list(app, night):
    r = app.get(f"/conversations?night={night}", headers=HTML)
    assert r.status_code == 200
    assert "Traceback" not in r.text


# ---------- F71: a filter value the page doesn't take ----------

@pytest.mark.parametrize("url, kept", [("/tasks?n=abc", "/tasks"), ("/tasks?n=abc&tab=waiting", "/tasks?tab=waiting"),
                                       ("/conversations?person=x", "/conversations")])
def test_a_mangled_filter_opens_the_page_without_it(app, url, kept):
    r = app.get(url, headers=HTML, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == kept
    assert app.get(url, headers=HTML).status_code == 200


def test_a_missing_required_value_does_not_send_the_page_round_in_circles(app):
    """/people/2/merge needs ?into=: dropping a value that isn't there would redirect to the same address forever."""
    for url in ("/people/2/merge", "/people/2/merge?into=abc"):
        r = app.get(url, headers=HTML)   # follows redirects: one hop at most, then a page, never raw code text
        assert r.status_code == 404 and "<html" in r.text.lower() and '"detail"' not in r.text, url
        assert len(r.history) <= 1, url


def test_a_mangled_filter_still_says_why_to_code(app):
    """htmx and scripts still get the plain 422 (they never show raw text to Brian)."""
    assert app.get("/tasks?n=abc", headers={"accept": "application/json"}).status_code == 422
    assert app.get("/tasks?n=abc", headers={**HTML, "hx-request": "true"}).status_code == 422


# ---------- F72: Approve twice ----------

def recording_jobs(client, rec):
    return one(client, "SELECT count(*) FROM job WHERE kind = 'recording' AND recording_id = ?", rec)


def test_approving_a_recording_twice_queues_it_once(app):
    rid, rec = 4, one(app, "SELECT json_extract(payload, '$.recording_id') FROM review_item WHERE id = 4")
    before = recording_jobs(app, rec)
    for _ in range(2):
        assert app.post(f"/review/{rid}/approve", data={"ai": "1", "confirm": "1"}).status_code == 200
    assert recording_jobs(app, rec) == before + 1


def test_approving_a_dismissed_recording_says_so(app):
    app.post("/review/4/dismiss")
    r = app.post("/review/4/approve", data={"ai": "1", "confirm": "1"}, headers=HTML)
    assert r.status_code == 409 and "Trash" in r.text


def test_the_pipeline_never_queues_one_recording_twice(app):
    from scribe_bot.pipeline import ingest
    with app.db() as conn:
        rec = conn.execute("SELECT json_extract(payload, '$.recording_id') FROM review_item WHERE id = 5").fetchone()[0]
        first = ingest.approve(conn, rec)
        assert ingest.approve(conn, rec) == first


# ---------- Stale pages: the thing is gone ----------

@pytest.mark.parametrize("url, data", [
    ("/decisions/99999/wrong", {}), ("/decisions/99999/right", {}), ("/decisions/99999/edit", {"text": "x"}),
    ("/facts/99999/confirm", {"ok": "0"}), ("/facts/99999/confirm", {"ok": "1"}), ("/facts/99999/edit", {"value": "x"}),
    ("/tasks/99999/done", {}), ("/tasks/99999/sure", {}), ("/speakers/99999/name", {"new_name": "Ana"}),
    ("/segments/99999/join", {}), ("/people/99999/not-same", {"into": "2"}),
])
def test_acting_on_something_gone_says_it_is_gone(app, url, data):
    r = app.post(url, data=data, headers=HTML)
    assert r.status_code == 404 and "isn't here any more" in r.text


def test_people_not_same_with_itself_is_refused(app):
    r = app.post("/people/2/not-same", data={"into": "2"}, headers=HTML)
    assert r.status_code == 400
    assert one(app, "SELECT count(*) FROM person_not_same") == 0


# ---------- Second taps and Undo (F73–F78) ----------

SKIP_TABLES = {"undo_action", "removal", "setting", "search_history", "schema_migrations", "machine_sample",
               "sqlite_sequence", "job", "stage_run", "schedule_run", "ledger_entry", "backup_run"}
SKIP_COLS = {"updated_at", "opened_at", "created_at", "last_at"}


def dump(client) -> dict:
    """Everything Brian could see, minus clocks and logs."""
    out = {}
    with client.db() as conn:
        conn.row_factory = sqlite3.Row
        for (t,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'search_index_%'"):
            if t in SKIP_TABLES:
                continue
            if t == "search_index":
                out[t] = sorted(tuple(r) for r in conn.execute("SELECT kind, ref_id, conversation_id, text FROM search_index"))
                continue
            out[t] = sorted((tuple((k, r[k]) for k in r.keys() if k not in SKIP_COLS) for r in conn.execute(f"SELECT * FROM {t}")),
                            key=repr)
    return out


def changed(a: dict, b: dict) -> list[str]:
    return [t for t in sorted(set(a) | set(b)) if set(a.get(t, [])) != set(b.get(t, []))]


ACTIONS = [
    ("/tasks/1/done", {}), ("/tasks/1/not-needed", {}), ("/tasks/14/delete", {}), ("/tasks/14/sure", {}),
    ("/tasks/1/edit", {"title": "A new title", "owner": "2", "due": "2026-12-01"}), ("/tasks-bulk/done", {"ids": "1,2,3"}),
    ("/tasks-bulk/not-needed", {"ids": "1,2"}), ("/decisions/1/wrong", {}), ("/decisions/1/edit", {"text": "Other words"}),
    ("/facts/1/confirm", {"ok": "0"}), ("/facts/1/edit", {"value": "Other note"}), ("/facts/1/kind", {"kind": "event"}),
    ("/facts/1/person", {"person_id": "3"}), ("/conversations/2/delete", {}), ("/talks-bulk", {"ids": "1,2", "what": "trash"}),
    ("/conversations/8/tags/9/remove", {}), ("/review/4/dismiss", {}), ("/speakers/12/unknown", {}),
    ("/speakers/12/name", {"special": "idk"}), ("/speakers/2/name", {"special": "not"}), ("/speakers/2/unassign", {}),
    ("/segments/3/speaker", {"speaker_id": "1"}), ("/segments/3/text", {"text": "Changed words here"}), ("/segments/3/join", {}),
    ("/conversations/1/summary", {"title": "New title"}), ("/conversations/1/summary", {"summary": "New summary"}),
    ("/people/3/not-same", {"into": "2"}), ("/people/3/not-same", {"into": "2", "later": "1"}),
    ("/notifications/1/dismiss", {}), ("/conversations/41/restore", {}),
]


@pytest.mark.parametrize("presses", [1, 2])
@pytest.mark.parametrize("url, data", ACTIONS, ids=[f"{u} {d}" for u, d in ACTIONS])
def test_one_undo_puts_back_what_the_taps_changed(app, url, data, presses):
    """F73–F78: tap once (or twice, as a double tap or the other device does), then Undo once on the bar: everything is
    as it was. A second tap that changed nothing used to save an Undo of nothing, which the bar offered instead."""
    if url == "/facts/1/kind":
        assert one(app, "SELECT type FROM person_fact WHERE id = 1") != "event"
    before = dump(app)
    top = one(app, "SELECT coalesce(max(id), 0) FROM undo_action")
    for _ in range(presses):
        r = app.post(url, data=data, headers={**HTML, "referer": "http://testserver/"}, follow_redirects=False)
        assert r.status_code < 500
    assert changed(before, dump(app)), "the first tap should change something"
    with app.db() as conn:
        offered = conn.execute("SELECT id FROM undo_action WHERE id > ? AND undone_at IS NULL ORDER BY id DESC LIMIT 1",
                               (top,)).fetchone()
    assert offered, "the bar offers Undo"
    assert app.post(f"/undo/{offered[0]}", headers={"referer": "http://testserver/"}, follow_redirects=False).status_code == 303
    assert changed(before, dump(app)) == []


def test_a_second_tap_offers_no_second_undo(app):
    app.post("/tasks/1/done")
    app.post("/tasks/1/done")
    assert one(app, "SELECT count(*) FROM undo_action WHERE label = 'Marked done'") == 1


def test_an_edit_saved_unchanged_offers_no_undo(app):
    with app.db() as conn:
        t = conn.execute("SELECT title, owner_person_id, due FROM task WHERE id = 1").fetchone()
    app.post("/tasks/1/edit", data={"title": t[0], "owner": str(t[1]), "due": t[2] or ""})
    assert one(app, "SELECT count(*) FROM undo_action") == 0


def test_hiding_a_suggestion_twice_keeps_one_row(app):
    for _ in range(2):
        app.post("/people/3/not-same", data={"into": "2"})
    assert one(app, "SELECT count(*) FROM person_not_same WHERE a = 2 AND b = 3") == 1


def test_join_undo_points_tasks_back_at_their_line(app):
    """F78: a task said by the joined line moves to the line above; Undo puts it back on its own line."""
    seg = one(app, "SELECT source_segment_id FROM task WHERE id = 2")
    app.post(f"/segments/{seg}/join")
    assert one(app, "SELECT source_segment_id FROM task WHERE id = 2") != seg
    uid = one(app, "SELECT max(id) FROM undo_action")
    app.post(f"/undo/{uid}")
    assert one(app, "SELECT source_segment_id FROM task WHERE id = 2") == seg


def test_fact_kind_undo_puts_the_kind_back(app):
    kind = one(app, "SELECT type FROM person_fact WHERE id = 1")
    app.post("/facts/1/kind", data={"kind": "event" if kind != "event" else "work"})
    app.post(f"/undo/{one(app, 'SELECT max(id) FROM undo_action')}")
    assert one(app, "SELECT type FROM person_fact WHERE id = 1") == kind


def test_looks_right_undo_puts_the_task_back_in_the_inbox(app):
    """F74: the Inbox item came back closed, so the task sat in neither list."""
    app.post("/tasks/14/sure")
    app.post(f"/undo/{one(app, 'SELECT max(id) FROM undo_action')}")
    assert one(app, "SELECT status FROM review_item WHERE kind = 'inbox' AND ref_id = 14") == "open"


# ---------- Edge text ----------

WEIRD = ["   ", "x" * 10_000, "émoji 🎉 עברית مرحبا", "<script>alert(1)</script>", "\"'; DROP TABLE task; --"]


@pytest.mark.parametrize("text", WEIRD)
def test_task_titles_take_any_text_and_show_it_escaped(app, text):
    r = app.post("/tasks/1/edit", data={"title": text, "owner": "1"}, headers=HTML)
    if not text.strip():
        assert r.status_code < 500
        return
    assert r.status_code == 200
    page = app.get("/tasks").text
    assert "<script>alert(1)</script>" not in page
    assert one(app, "SELECT count(*) FROM task") > 0


@pytest.mark.parametrize("q", ['"', "'", "*", "AND", "OR NOT", "(", "a:b", "NEAR(", "^", "-", "🎉", "%_", "\\", "x" * 5000])
def test_search_takes_any_words(app, q):
    assert app.get("/search", params={"q": q}).status_code == 200


@pytest.mark.parametrize("url", ["/conversations/0", "/conversations/-1", "/conversations/99999999999999999999",
                                 "/people/0", "/people/abc", "/tasks/99999999999999999999/done"])
def test_odd_ids_answer_not_found(app, url):
    r = app.post(url) if url.endswith("/done") else app.get(url, headers=HTML)
    assert r.status_code in (404, 422)


# ---------- Empty database ----------

PAGES = ["/", "/inbox", "/review", "/tasks", "/tasks?tab=waiting", "/tasks?tab=inbox", "/tasks?tab=decisions", "/conversations",
         "/conversations?trash=1", "/people", "/search?q=anything", "/notifications", "/settings", "/status", "/capture"]


@pytest.mark.parametrize("path", PAGES)
def test_every_page_opens_on_an_empty_database_without_placeholder_words(empty, path):
    r = empty.get(path)
    assert r.status_code == 200
    import re
    text = re.sub(r"<script.*?</script>|<[^>]+>", " ", r.text, flags=re.S)
    for word in ("None", "nan", "undefined", "NaN"):
        assert not re.search(rf"\b{word}\b", text), (path, word)


def test_two_devices_approving_at_once_queue_it_once(app):
    """F72, at the same moment: each request checks inside the write lock, so the second finds the first's job."""
    import threading
    rec = one(app, "SELECT json_extract(payload, '$.recording_id') FROM review_item WHERE id = 6")
    before = recording_jobs(app, rec)
    codes = []
    go = threading.Barrier(2)

    def tap():
        go.wait()
        codes.append(app.post("/review/6/approve", data={"ai": "1", "confirm": "1"}).status_code)

    threads = [threading.Thread(target=tap) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes == [200, 200]
    assert recording_jobs(app, rec) == before + 1


def test_a_line_listed_twice_moves_its_talk_time_once(app):
    """F83: "Who said these lines" with a line listed twice took its seconds off the old speaker twice."""
    with app.db() as conn:
        to = conn.execute("SELECT speaker_id FROM segment WHERE id = 4").fetchone()[0]
        moving = conn.execute("SELECT coalesce(sum(end_s - start_s), 0) FROM segment WHERE id IN (3, 5) AND speaker_id != ?",
                              (to,)).fetchone()[0]
        had = conn.execute("SELECT talk_seconds FROM speaker WHERE id = ?", (to,)).fetchone()[0]
    assert moving > 0
    app.post("/segments/3/speaker", data={"speaker_id": str(to), "ids": "5,5,5"})
    assert one(app, "SELECT talk_seconds FROM speaker WHERE id = ?", to) == pytest.approx(had + moving)


def test_people_search_takes_percent_and_underscore_literally(app):
    """F81: "_" matched every person."""
    page = app.get("/people", params={"q": "_"}).text
    assert page.count('href="/people/') < app.get("/people").text.count('href="/people/')


def test_the_confirm_step_for_one_already_approved_says_so(app):
    """F82: Back after approving showed "Transcribe and summarise with AI?" again for one already on its way."""
    assert app.post("/review/7/approve", data={"ai": "1", "confirm": "1"}).status_code == 200
    r = app.get("/review/7/confirm", headers=HTML)
    assert r.status_code == 409 and "Already approved" in r.text


@pytest.mark.parametrize("url, data", ACTIONS, ids=[f"{u} {d}" for u, d in ACTIONS])
def test_a_double_tap_across_a_clock_tick_still_undoes_in_one(app, url, data, monkeypatch):
    """F118 (CI flake, 2026-11-26): a double tap that lands either side of a second re-stamped the time (Trash moved
    "deleted at" on), so the second tap counted as a change and its Undo put back the first tap, not the start. The
    clock now ticks a second on every read, so every action is checked at that worst moment."""
    from datetime import datetime, timedelta, timezone
    t = [datetime.now(timezone.utc).replace(microsecond=0)]

    def tick() -> str:
        t[0] += timedelta(seconds=1)
        return t[0].isoformat(timespec="seconds")
    import sys
    real = db.now
    for m in [m for k, m in list(sys.modules.items()) if k.startswith("scribe_bot")]:   # also "from db import now"
        for name in ("now", "utc_now"):
            if getattr(m, name, None) is real:
                monkeypatch.setattr(m, name, tick)
    test_one_undo_puts_back_what_the_taps_changed(app, url, data, 2)
