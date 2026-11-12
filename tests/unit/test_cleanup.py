"""Weekly task cleanup: archive finished tasks, ask about old open ones; nothing deleted (synthetic only)."""
from datetime import datetime, timedelta, timezone

from scribe_bot.core import cleanup
from test_web_pages import env, one  # noqa: F401

NOW = datetime.now(timezone.utc) - timedelta(minutes=1)   # the routes stamp the real clock
OLD = (NOW - timedelta(days=45)).isoformat(timespec="seconds")
RECENT = (NOW - timedelta(days=3)).isoformat(timespec="seconds")


def add(conn, tid, status, updated, due=None, inbox=0):
    conn.execute("INSERT INTO task (id, title, owner_person_id, status, due, inbox, created_at, updated_at) "
                 "VALUES (?, ?, 1, ?, ?, ?, ?, ?)", (tid, f"Synthetic task {tid}", status, due, inbox, updated, updated))


def test_archives_old_finished_and_flags_old_open(env):
    _, conn, _ = env
    for tid, status, updated, due, inbox in [(10, "done", OLD, None, 0), (11, "done", RECENT, None, 0), (12, "dropped", OLD, None, 0),
                                             (13, "open", OLD, None, 0), (14, "open", RECENT, None, 0),
                                             (15, "open", OLD, "2026-12-01", 0), (16, "open", OLD, None, 1)]:
        add(conn, tid, status, updated, due, inbox)
    total = one(conn, "SELECT count(*) FROM task")
    assert cleanup.run(conn, NOW) == {"archived": 2, "stale": 1}
    assert [r[0] for r in conn.execute("SELECT id FROM task WHERE archived_at IS NOT NULL ORDER BY id")] == [10, 12]
    assert [r[0] for r in conn.execute(f"SELECT id FROM task t WHERE {cleanup.STALE}")] == [13]   # not dated, not inbox
    assert one(conn, "SELECT count(*) FROM task") == total                                    # nothing deleted
    assert cleanup.run(conn, NOW) == {"archived": 0, "stale": 0}                               # idempotent


def test_still_need_it_silences_until_another_month(env):
    client, conn, _ = env
    add(conn, 13, "open", OLD)
    cleanup.run(conn, NOW)
    page = client.get("/").text
    assert "1 old task to check" in page and 'href="/tasks?show=old"' in page
    old = client.get("/tasks?show=old").text
    assert "Synthetic task 13" in old and 'action="/tasks/13/keep"' in old and "Review the launch deck" not in old
    client.post("/tasks/13/keep")
    assert "old task" not in client.get("/").text
    assert cleanup.run(conn, NOW)["stale"] == 0                                                # just answered
    cleanup.run(conn, NOW + timedelta(days=31))                                                # another quiet month: ask again
    assert 13 in [r[0] for r in conn.execute(f"SELECT id FROM task t WHERE {cleanup.STALE}")]


def test_done_list_hides_archived_with_a_way_back(env):
    client, conn, _ = env
    add(conn, 10, "done", OLD)
    cleanup.run(conn, NOW)
    done = client.get("/tasks?show=done").text
    assert "Synthetic task 10" not in done and "show 1 archived" in done
    assert "Synthetic task 10" in client.get("/tasks?show=done&archived=1").text
