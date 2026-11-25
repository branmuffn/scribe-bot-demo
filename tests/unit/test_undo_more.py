"""Undo for the last four actions without it (Brian, 2026-11-25: "you can show the undo for these items briefly bc what
if I do need to undo it"): merging people, merging Inbox recordings, "This isn't X", and Trash restore / Delete forever.
Synthetic data only."""
import json

from scribe_bot.backup import trash
from scribe_bot.core import db
from scribe_bot.pipeline import identify
from test_merge import setup as inbox_setup
from test_web_feedback import HX, vec
from test_web_pages import env, one  # noqa: F401


def last_undo(conn):
    return one(conn, "SELECT max(id) FROM undo_action")


def bar(page, label):
    return 'id="undo-bar"' in page and label in page and "/undo/" in page


def add_alex_twin(conn):
    """Person 9 "Alex" (alias Al) with a speaker, a task, a note, a decision, a voiceprint and an open review item."""
    t = db.now()
    conn.execute("INSERT INTO person (id, name, aliases, notes, area, created_at) VALUES (9, 'Alex', '[\"Al\"]', 'Plays chess', "
                 "'personal', ?)", (t,))
    conn.execute("UPDATE speaker SET person_id = 9 WHERE id = 3")
    conn.execute("INSERT INTO task (id, conversation_id, title, owner_person_id, created_at, updated_at) VALUES (40, 1, 'Call back', 9, ?, ?)", (t, t))
    conn.execute("INSERT INTO person_fact (id, person_id, person_name, conversation_id, value, created_at) VALUES (40, 9, 'Alex', 1, 'Likes tea', ?)", (t,))
    conn.execute("INSERT INTO decision (id, conversation_id, text, people, created_at) VALUES (40, 1, 'Ship it', '[9, 2]', ?)", (t,))
    conn.execute("INSERT INTO voiceprint (id, person_id, embedding, sample_count, updated_at) VALUES (90, 9, ?, 1, ?)", (vec(1, 0), t))
    conn.execute("INSERT INTO voiceprint (id, person_id, embedding, sample_count, updated_at) VALUES (20, 2, ?, 3, ?)", (vec(0, 1), t))
    conn.execute("INSERT INTO review_item (id, kind, ref_id, payload, created_at) VALUES (40, 'speaker', 3, '{\"person_id\": 9}', ?)", (t,))
    conn.execute("INSERT INTO search_index (kind, ref_id, conversation_id, text) VALUES ('person', 9, NULL, 'Alex Al Plays chess')")


def as_before_merge(conn):
    assert one(conn, "SELECT name FROM person WHERE id = 9") == "Alex"
    assert one(conn, "SELECT person_id FROM speaker WHERE id = 3") == 9
    assert one(conn, "SELECT owner_person_id FROM task WHERE id = 40") == 9
    assert one(conn, "SELECT person_id || ' ' || person_name FROM person_fact WHERE id = 40") == "9 Alex"
    assert json.loads(one(conn, "SELECT people FROM decision WHERE id = 40")) == [9, 2]
    assert json.loads(one(conn, "SELECT payload FROM review_item WHERE id = 40")) == {"person_id": 9}
    p2 = conn.execute("SELECT aliases, notes, area FROM person WHERE id = 2").fetchone()
    assert (json.loads(p2["aliases"] or "[]"), p2["notes"], p2["area"]) == ([], None, "work")
    assert [tuple(r) for r in conn.execute("SELECT person_id, sample_count FROM voiceprint ORDER BY person_id")] == [(2, 3), (9, 1)]
    assert one(conn, "SELECT embedding FROM voiceprint WHERE person_id = 9") == vec(1, 0)
    assert one(conn, "SELECT count(*) FROM search_index WHERE kind = 'person' AND ref_id = 9") == 1


def test_merge_two_people_then_undo_brings_everything_back(env):
    client, conn, _ = env
    add_alex_twin(conn)
    page = client.post("/people/9/merge", data={"into": 2}).text
    assert bar(page, "People merged")
    assert one(conn, "SELECT count(*) FROM person WHERE id = 9") == 0
    assert one(conn, "SELECT count(*) FROM voiceprint") == 1                       # combined into one
    assert client.post(f"/undo/{last_undo(conn)}").status_code == 200
    as_before_merge(conn)


def test_merge_undo_keeps_a_later_edit_to_the_kept_person(env):
    """Two devices (R5-21): Brian edits Alex Example's notes after the merge; Undo brings Alex back and keeps the edit."""
    client, conn, _ = env
    add_alex_twin(conn)
    client.post("/people/9/merge", data={"into": 2})
    conn.execute("UPDATE person SET notes = 'Edited on the phone' WHERE id = 2")
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT notes FROM person WHERE id = 2") == "Edited on the phone"
    assert one(conn, "SELECT count(*) FROM person WHERE id = 9") == 1
    assert json.loads(one(conn, "SELECT aliases FROM person WHERE id = 2") or "[]") == []   # the merge's aliases go


def test_merge_many_is_one_undo(env):
    client, conn, _ = env
    add_alex_twin(conn)
    t = db.now()
    conn.execute("INSERT INTO person (id, name, area, created_at) VALUES (11, 'Alexander', 'work', ?)", (t,))
    conn.execute("UPDATE decision SET people = '[9, 11]' WHERE id = 1")
    page = client.post("/people/merge-many", data={"ids": "9,11,2", "into": 2}).text
    assert bar(page, "3 people merged")
    assert one(conn, "SELECT count(*) FROM person WHERE id IN (9, 11)") == 0
    assert json.loads(one(conn, "SELECT people FROM decision WHERE id = 1")) == [2]
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT count(*) FROM person WHERE id IN (9, 11)") == 2
    assert json.loads(one(conn, "SELECT people FROM decision WHERE id = 1")) == [9, 11]   # its first "before" wins
    as_before_merge(conn)


def test_failed_merge_leaves_no_undo_note(env):
    client, conn, _ = env
    before = one(conn, "SELECT count(*) FROM undo_action")
    assert client.post("/people/1/merge", data={"into": 2}).status_code == 400      # Brian can't be merged away
    assert client.post("/people/77/merge", data={"into": 2}).status_code == 404
    assert one(conn, "SELECT count(*) FROM undo_action") == before


def test_this_isnt_x_then_undo_puts_the_name_back_everywhere(env, monkeypatch):
    client, conn, _ = env
    t = db.now()
    conn.execute("UPDATE speaker SET confirmed = 1 WHERE id = 1")
    conn.execute("INSERT INTO voiceprint (id, person_id, embedding, sample_count, updated_at) VALUES (20, 2, ?, 3, ?)", (vec(0, 1), t))
    monkeypatch.setattr(identify, "speaker_embedding", lambda *a, **k: list(json.loads("[0.6, 0.8]")))
    conn.execute("UPDATE conversation SET summary = 'Alex Example asked for the deck.' WHERE id = 1")
    people_before = one(conn, "SELECT count(*) FROM person")
    page = client.post("/speakers/1/unassign").text
    assert bar(page, "Name taken off")
    assert one(conn, "SELECT person_id FROM speaker WHERE id = 1") is None
    assert one(conn, "SELECT summary FROM conversation WHERE id = 1") == "Speaker 1 asked for the deck."
    assert one(conn, "SELECT sample_count FROM voiceprint WHERE person_id = 2") == 2   # this voice left the voiceprint
    assert one(conn, "SELECT count(*) FROM person") == people_before + 1                # a "Speaker 1" stand-in for the task
    client.post(f"/undo/{last_undo(conn)}")
    sp = conn.execute("SELECT person_id, confirmed FROM speaker WHERE id = 1").fetchone()
    assert tuple(sp) == (2, 1)
    assert one(conn, "SELECT summary FROM conversation WHERE id = 1") == "Alex Example asked for the deck."
    assert one(conn, "SELECT owner_person_id FROM task WHERE id = 2") == 2
    assert one(conn, "SELECT person_id FROM person_fact WHERE id = 1") == 2
    assert [tuple(r) for r in conn.execute("SELECT person_id, sample_count, embedding FROM voiceprint")] == [(2, 3, vec(0, 1))]
    assert one(conn, "SELECT count(*) FROM review_item WHERE kind = 'speaker' AND ref_id = 1") == 0
    assert one(conn, "SELECT count(*) FROM person") == people_before                    # the stand-in is gone
    assert one(conn, "SELECT text FROM search_index WHERE kind = 'summary' AND ref_id = 1") == "Alex Example asked for the deck."


def test_inbox_merge_then_undo_splits_it_back(env, tmp_path, monkeypatch):
    client, conn, _ = env
    inbox_setup(conn, tmp_path, monkeypatch)
    page = client.post("/inbox/merge", data={"ids": "72,71"}).text
    assert bar(page, "2 recordings merged")
    new = one(conn, "SELECT max(id) FROM recording")
    assert client.post(f"/undo/{last_undo(conn)}").status_code == 200
    assert one(conn, "SELECT count(*) FROM recording WHERE id = ?", new) == 0
    assert {r[0] for r in conn.execute("SELECT status FROM recording WHERE id IN (71, 72)")} == {"awaiting_approval"}
    assert one(conn, "SELECT count(*) FROM review_item WHERE kind = 'approval' AND status = 'open' AND ref_id IN (71, 72)") == 2


def test_inbox_merge_undo_after_it_was_processed_says_why(env, tmp_path, monkeypatch):
    client, conn, _ = env
    inbox_setup(conn, tmp_path, monkeypatch)
    client.post("/inbox/merge", data={"ids": "72,71"})
    new = one(conn, "SELECT max(id) FROM recording")
    conn.execute("UPDATE recording SET status = 'queued' WHERE id = ?", (new,))      # it started processing on the M14
    r = client.post(f"/undo/{last_undo(conn)}")
    assert r.status_code == 409 and "already been processed" in r.text
    assert one(conn, "SELECT count(*) FROM recording WHERE id = ?", new) == 1


def test_inbox_merge_undo_after_unmerge_is_harmless(env, tmp_path, monkeypatch):
    client, conn, _ = env
    inbox_setup(conn, tmp_path, monkeypatch)
    client.post("/inbox/merge", data={"ids": "72,71"})
    uid, new = last_undo(conn), one(conn, "SELECT max(id) FROM recording")
    client.post(f"/inbox/{new}/unmerge")                                              # Unmerge on the other device
    assert client.post(f"/undo/{uid}").status_code == 200
    assert {r[0] for r in conn.execute("SELECT status FROM recording WHERE id IN (71, 72)")} == {"awaiting_approval"}


def skip_recording(conn, rid=50):
    t = db.now()
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, status, title) "
                 "VALUES (?, 'recorder', '/x', ?, ?, 'declined', 'Skipped one')", (rid, f"s{rid}", t))
    conn.execute("INSERT INTO review_item (id, kind, ref_id, status, resolved_at, created_at) VALUES (?, 'approval', ?, 'dismissed', ?, ?)",
                 (rid, rid, t, t))


def test_trash_restore_one_then_undo(env):
    client, conn, _ = env
    t = db.now()
    conn.execute("UPDATE conversation SET deleted_at = ? WHERE id = 1", (t,))
    page = client.post("/conversations/1/restore", headers={"Referer": "http://testserver/conversations?trash=1"}).text
    assert bar(page, "Restored")
    assert one(conn, "SELECT deleted_at FROM conversation WHERE id = 1") is None
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT deleted_at FROM conversation WHERE id = 1") == t


def test_trash_restore_several_and_a_skipped_recording_is_one_undo(env):
    client, conn, _ = env
    t = db.now()
    conn.execute("UPDATE conversation SET deleted_at = ? WHERE id = 1", (t,))
    skip_recording(conn)
    page = client.post("/trash-bulk/restore", data={"ids": "c1,r50"}).text
    assert bar(page, "2 items restored")
    assert one(conn, "SELECT status FROM recording WHERE id = 50") == "awaiting_approval"
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT deleted_at FROM conversation WHERE id = 1") == t
    assert one(conn, "SELECT status FROM recording WHERE id = 50") == "declined"
    assert one(conn, "SELECT status FROM review_item WHERE id = 50") == "dismissed"
    page = client.get("/conversations?trash=1").text
    assert 'data-pick-id="c1"' in page and 'data-pick-id="r50"' in page


def test_skipped_recording_restore_alone_can_be_undone(env):
    client, conn, _ = env
    skip_recording(conn)
    page = client.post("/recordings/50/unskip", headers={"Referer": "http://testserver/conversations?trash=1"}).text
    assert bar(page, "Restored")
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT status FROM recording WHERE id = 50") == "declined"


def test_delete_forever_waits_out_the_undo_then_deletes(env):
    client, conn, _ = env
    conn.execute("UPDATE conversation SET deleted_at = ? WHERE id = 1", (db.now(),))
    page = client.post("/trash-bulk/forever", data={"ids": "c1"}).text
    assert bar(page, "Deleted forever") and 'data-pick-id="c1"' not in page
    assert one(conn, "SELECT count(*) FROM conversation WHERE id = 1") == 1            # kept while Undo can bring it back
    assert client.get("/conversations/1").status_code == 404
    client.post(f"/undo/{last_undo(conn)}")
    assert one(conn, "SELECT purge_at FROM conversation WHERE id = 1") is None
    assert 'data-pick-id="c1"' in client.get("/conversations?trash=1").text            # back in Trash as it was
    client.post("/trash-bulk/forever", data={"ids": "c1"})
    assert trash.purge_due(conn) == 0                                                  # not yet: Undo still works
    conn.execute("UPDATE conversation SET purge_at = '2020-01-01T00:00:00+00:00' WHERE id = 1")
    assert trash.purge_due(conn) == 1
    assert one(conn, "SELECT count(*) FROM conversation WHERE id = 1") == 0
    assert one(conn, "SELECT count(*) FROM task WHERE conversation_id = 1") == 0


def test_nightly_purge_also_deletes_the_due_ones(env):
    _, conn, _ = env
    conn.execute("UPDATE conversation SET deleted_at = ?, purge_at = '2020-01-01T00:00:00+00:00' WHERE id = 1", (db.now(),))
    assert trash.purge(conn) == 1


def test_trash_confirm_no_longer_says_it_cant_be_undone(env):
    conn = env[1]
    conn.execute("UPDATE conversation SET deleted_at = ? WHERE id = 1", (db.now(),))
    assert "can't be undone" not in env[0].get("/conversations?trash=1").text.replace("&#39;", "'")


def test_hx_unassign_still_swaps_in_place(env):
    client, conn, _ = env
    r = client.post("/speakers/1/unassign", headers=HX)
    assert r.status_code == 200 and one(conn, "SELECT person_id FROM speaker WHERE id = 1") is None
