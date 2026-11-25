"""M7 (Brian, 2026-11-25): three labelled test recordings for the Inbox, removable without touching real ones."""
from test_web_pages import env, one  # noqa: F401  (env is a fixture)

from scribe_bot.web import test_inbox


def test_add_three_test_recordings_then_remove_only_them(env, monkeypatch):
    client, conn, cfg = env
    monkeypatch.setattr(test_inbox.shutil, "which", lambda name: None)   # no macOS `say` here: tones
    real = one(conn, "SELECT count(*) FROM recording")
    assert "Add 3 test recordings" in client.get("/help").text
    r = client.post("/test-inbox/add", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/inbox"
    states = sorted(x[0] for x in conn.execute("SELECT status FROM recording WHERE title LIKE 'TEST %'"))
    assert states == ["awaiting_approval", "awaiting_approval", "backlog"]
    page = client.get("/inbox").text
    assert "TEST 1" in page and "TEST 2" in page and "TEST 3" in page
    assert "TEST 1" in client.get("/inbox?q=pineapple").text and "TEST 2" not in client.get("/inbox?q=pineapple").text
    assert client.post("/test-inbox/add").status_code == 200 and one(conn, "SELECT count(*) FROM recording WHERE title LIKE 'TEST %'") == 3
    # a real recording that happens to start with TEST, outside the test folder, is never removed
    conn.execute("INSERT INTO recording (source, original_path, sha256, received_at, status, title) "
                 "VALUES ('recorder', '/elsewhere/TEST real.m4a', 'x1', '2026-11-25', 'awaiting_approval', 'TEST real')")
    assert "Remove test recordings (3)" in client.get("/help").text
    client.post("/test-inbox/remove")
    assert one(conn, "SELECT count(*) FROM recording WHERE title LIKE 'TEST %'") == 1          # only the real one left
    assert one(conn, "SELECT count(*) FROM recording") == real + 1
    assert one(conn, "SELECT count(*) FROM review_item WHERE kind = 'approval' AND ref_id NOT IN (SELECT id FROM recording)") == 0
    assert not list(test_inbox.folder(cfg).glob("TEST *"))


def test_thirty_more_for_scrolling_then_remove_takes_all(env, monkeypatch):
    """R4-24: Help › Add 30 more gives the Inbox enough rows to scroll; Remove takes all 33; adding twice adds none."""
    client, conn, cfg = env
    monkeypatch.setattr(test_inbox.shutil, "which", lambda name: None)
    client.post("/test-inbox/add")
    assert "Add 30 more for scrolling" in client.get("/help").text
    client.post("/test-inbox/add", data={"more": "1"})
    client.post("/test-inbox/add", data={"more": "1"})
    assert one(conn, "SELECT count(*) FROM recording WHERE title LIKE 'TEST %'") == 33
    assert "Add 30 more for scrolling" not in client.get("/help").text and "Remove test recordings (33)" in client.get("/help").text
    assert one(conn, "SELECT count(DISTINCT sha256) FROM recording WHERE title LIKE 'TEST %'") == 33
    client.post("/test-inbox/remove")
    assert one(conn, "SELECT count(*) FROM recording WHERE title LIKE 'TEST %'") == 0
    assert not list(test_inbox.folder(cfg).glob("TEST *"))
