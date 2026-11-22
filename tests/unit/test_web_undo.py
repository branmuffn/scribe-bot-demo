"""Undo bar: every undoable action can be reversed, search included (synthetic only)."""
import pytest

from scribe_bot.web import undo
from test_web_pages import env, one  # noqa: F401


def last_undo(conn):
    return one(conn, "SELECT max(id) FROM undo_action")


def test_task_done_and_remove_undo(env):
    client, conn, _ = env
    page = client.post("/tasks/1/done").text
    assert 'id="undo-bar"' in page and "Marked “Review the launch deck” done" in page and "/undo/" in page
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT status FROM task WHERE id = 1") == "open"
    client.post("/tasks/1/delete")
    assert one(conn, "SELECT count(*) FROM search_index WHERE kind = 'task' AND ref_id = 1") == 0
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT status FROM task WHERE id = 1") == "open"
    assert one(conn, "SELECT text FROM search_index WHERE kind = 'task' AND ref_id = 1") == "Review the launch deck"


def test_undo_once_only_and_bar_goes_away(env):
    client, conn, _ = env
    client.post("/tasks/1/done")
    uid = last_undo(conn)
    assert client.post(f"/undo/{uid}").status_code == 200
    assert client.post(f"/undo/{uid}").status_code == 410
    assert f"/undo/{uid}" not in client.get("/tasks").text


def test_undo_too_late(env):
    client, conn, _ = env
    client.post("/tasks/1/done")
    conn.execute("UPDATE undo_action SET created_at = '2020-01-01T00:00:00+00:00'")
    assert 'id="undo-bar" class="undo-bar" role="status" aria-live="polite" hidden' in client.get("/tasks").text
    assert client.post(f"/undo/{last_undo(conn)}").status_code == 410
    assert one(conn, "SELECT status FROM task WHERE id = 1") == "done"


def test_owner_decision_and_note_undo(env):
    client, conn, _ = env
    client.post("/tasks/1/owner", data={"person_id": 2})
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT owner_person_id FROM task WHERE id = 1") == 1
    client.post("/decisions/1/wrong")
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT rated FROM decision WHERE id = 1") is None
    assert one(conn, "SELECT count(*) FROM search_index WHERE kind = 'decision' AND ref_id = 1") == 1
    fid = one(conn, "SELECT min(id) FROM person_fact")
    client.post(f"/facts/{fid}/confirm", data={"ok": 0})
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT coalesce(confirmed, 1) FROM person_fact WHERE id = ?", fid) == 1


def test_line_fixes_undo_including_a_new_speaker(env):
    client, conn, _ = env
    sid = one(conn, "SELECT id FROM segment WHERE idx = 3")
    speakers = one(conn, "SELECT count(*) FROM speaker")
    client.post(f"/segments/{sid}/speaker", data={"new_name": "Pat Example"})
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT speaker_id FROM segment WHERE id = ?", sid) == 3
    assert one(conn, "SELECT talk_seconds FROM speaker WHERE id = 3") == 100
    assert one(conn, "SELECT count(*) FROM speaker") == speakers                      # the one-line speaker is gone again
    client.post(f"/segments/{sid}/text", data={"text": "Changed words."})
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT text FROM segment WHERE id = ?", sid) == "Quick interruption from someone else."
    assert one(conn, "SELECT text FROM search_index WHERE kind = 'segment' AND ref_id = ?", sid) == "Quick interruption from someone else."


def test_in_place_actions_refresh_the_bar(env):
    page = env[0].get("/conversations/1").text
    assert 'hx-select-oob="#undo-bar,#check-strip"' in page                         # task rows update the bar and the strip


@pytest.mark.parametrize("table", list(undo.COLUMNS))
def test_undo_columns_exist(env, table):
    cols = {r[1] for r in env[1].execute(f"PRAGMA table_info({table})")}
    assert set(undo.COLUMNS[table]) <= cols


def test_edit_decision_and_summary_with_undo(env):
    client, conn, _ = env
    page = client.get("/conversations/1").text
    assert 'action="/decisions/1/edit"' in page and 'data-edit-url="/conversations/1/summary" data-edit-field="summary"' in page
    assert 'data-edit-url="/conversations/1/summary" data-edit-field="title"' in page and "data-enter-submits" in page
    client.post("/decisions/1/edit", data={"text": "Launch moves to June"})
    assert one(conn, "SELECT text || '|' || rated FROM decision WHERE id = 1") == "Launch moves to June|right"   # kept on re-run
    assert one(conn, "SELECT text FROM search_index WHERE kind = 'decision' AND ref_id = 1") == "Launch moves to June"
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT text FROM decision WHERE id = 1") == "Launch moves to May"
    client.post("/conversations/1/summary", data={"title": "Zephyr sync", "summary": "Fixed summary.", "takeaways": "- one\n\n• two\n"})
    assert one(conn, "SELECT title || '|' || summary || '|' || takeaways FROM conversation WHERE id = 1") == 'Zephyr sync|Fixed summary.|["one", "two"]'
    assert one(conn, "SELECT count(*) FROM search_index WHERE kind = 'summary' AND text = 'Fixed summary.'") == 1
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT title FROM conversation WHERE id = 1") == "Weekly sync with Alex"
    assert one(conn, "SELECT count(*) FROM search_index WHERE kind = 'summary' AND text = 'Fixed summary.'") == 0
    assert client.post("/decisions/1/edit", data={"text": " "}).status_code == 400


def test_edit_in_place_one_field_at_a_time(env):
    client, conn, _ = env
    before = one(conn, "SELECT summary || '|' || takeaways FROM conversation WHERE id = 1")
    client.post("/conversations/1/summary", data={"title": "Renamed only"})
    assert one(conn, "SELECT title FROM conversation WHERE id = 1") == "Renamed only"
    assert one(conn, "SELECT summary || '|' || takeaways FROM conversation WHERE id = 1") == before   # untouched
    assert "Renamed the conversation" in client.get("/conversations/1").text                         # undo bar label
    page = client.get("/conversations/1").text                    # the task's in-place edit carries its owner and due date
    # 2026-11-21: a task's edit lives in its sheet (tap the task), with its owner kept
    assert 'action="/tasks/2/edit"' in page or 'hx-post="/tasks/2/edit"' in page
    sheet = page.split('id="ts-2"')[1].split('id="task-')[0]
    assert '/tasks/2/edit' in sheet and 'name="owner" value="2"' in sheet
    client.post("/tasks/2/edit", data={"title": "Send the budget today", "owner": 2, "due": ""})
    assert one(conn, "SELECT owner_person_id FROM task WHERE id = 2") == 2
    client.post("/conversations/1/private")
    assert client.post("/conversations/1/summary", data={"title": "Private but renamed"}).status_code == 200
    assert client.post("/conversations/1/summary", data={"summary": "x"}).status_code == 200   # kept notes can still be fixed by hand (nothing goes to the AI)


def test_edit_a_people_note_in_place_and_whole_card_opens(env):
    client, conn, _ = env
    fid = one(conn, "SELECT min(id) FROM person_fact")
    assert f'data-edit-url="/facts/{fid}/edit"' in client.get("/conversations/1").text
    client.post(f"/facts/{fid}/edit", data={"value": "Has a dog named Biscuit (a beagle)"})
    assert one(conn, "SELECT value FROM person_fact WHERE id = ?", fid) == "Has a dog named Biscuit (a beagle)"
    assert one(conn, "SELECT text FROM search_index WHERE kind = 'fact' AND ref_id = ?", fid).endswith("(a beagle)")
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT value FROM person_fact WHERE id = ?", fid) == "Has a dog named Biscuit"
    assert 'class="item tap-row conv-row"' in client.get("/conversations").text
