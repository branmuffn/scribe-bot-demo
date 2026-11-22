"""N1 (Brian, 2026-11-20): "No longer needed" closes a task without counting it as done; Closed view filters; Undo/Reopen."""
from test_web_pages import env, one  # noqa: F401


def open_task(conn) -> int:
    return one(conn, "SELECT id FROM task WHERE status = 'open' AND owner_person_id = 1 ORDER BY id LIMIT 1")


def test_not_needed_is_closed_but_not_done(env):
    client, conn, _ = env
    tid = open_task(conn)
    title = one(conn, "SELECT title FROM task WHERE id = ?", tid)
    page = client.get("/tasks").text
    # Brian, 2026-11-21: just "Not needed", no reason box (the route still accepts a note from older pages)
    assert "Not needed because" not in page and "aria-label=\"Not needed: " in page
    client.post(f"/tasks/{tid}/not-needed", data={"note": "event passed"})
    assert one(conn, "SELECT status || '/' || closed_as || '/' || closed_note FROM task WHERE id = ?", tid) == "done/not_needed/event passed"
    assert f'id="task-{tid}"' not in client.get("/tasks").text                         # gone from Open (the Undo bar names it)
    nn = client.get("/tasks?show=not_needed").text
    assert title in nn and "Not needed: event passed" in nn
    assert f'id="task-{tid}"' not in client.get("/tasks?show=done").text                         # Done means really done
    assert title in client.get("/tasks?show=closed").text
    assert one(conn, "SELECT rated FROM task WHERE id = ?", tid) is None          # not a judgment on the AI
    client.post(f"/tasks/{tid}/reopen")
    assert one(conn, "SELECT status || coalesce(closed_as, '-') FROM task WHERE id = ?", tid) == "open-"


def test_not_needed_never_counts_as_finished(env):
    client, conn, _ = env
    before = one(conn, "SELECT count(*) FROM task WHERE status = 'done' AND closed_as IS NULL")
    client.post(f"/tasks/{open_task(conn)}/not-needed", data={})
    assert one(conn, "SELECT count(*) FROM task WHERE status = 'done' AND closed_as IS NULL") == before
