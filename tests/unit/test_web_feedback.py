"""Brian's 2026-10-19 feedback: in-place actions, speaker rename, merge, reassign, fewer confirmations (synthetic data)."""
import json
from array import array

import pytest

from scribe_bot.core import db, people
from test_web_pages import env, one  # noqa: F401  (fixture + helper)

HX = {"HX-Request": "true", "Referer": "http://testserver/conversations/1"}


def vec(*xs):
    n = sum(x * x for x in xs) ** 0.5
    return array("f", [x / n for x in xs]).tobytes()


def test_small_actions_swap_only_their_section(env):
    client, conn, _ = env
    page = client.get("/conversations/1").text
    assert 'hx-post="/tasks/1/done" hx-target="#sec-owe" hx-select="#sec-owe"' in page
    assert 'hx-post="/facts/1/confirm" hx-target="#sec-facts"' in page and 'hx-select-oob="#undo-bar,#check-strip"' in page
    r = client.post("/tasks/1/done", headers=HX)                   # htmx follows the redirect back to the page
    assert r.status_code == 200 and 'id="sec-owe"' in r.text and 'id="check-strip"' in r.text
    assert one(conn, "SELECT status FROM task WHERE id = 1") == "done"


def test_facts_are_kept_by_default_and_only_removed(env):
    client, conn, _ = env
    page = client.get("/conversations/1").text
    assert "Has a dog named Biscuit" in page and "Wrong, remove" in page
    assert 'name="ok" value="1"' not in page                         # no ✓ to tap per note
    client.post("/facts/1/confirm", data={"ok": 0}, headers=HX)
    assert one(conn, "SELECT confirmed FROM person_fact WHERE id = 1") == 0
    assert "Has a dog named Biscuit" not in client.get("/conversations/1").text


def test_check_strip_counts(env):
    page = env[0].get("/conversations/1").text
    strip = page[page.index('id="check-strip"'):page.index("</nav>", page.index('id="check-strip"'))]
    assert "2 you owe" in strip and "1 they owe you" in strip and "1 note" in strip
    assert "1 unsure" in strip and "1 speaker to name" in strip


def test_rename_speaker_relabels_lines_and_closes_review(env, monkeypatch):
    client, conn, _ = env
    enrolled = []
    monkeypatch.setattr("scribe_bot.pipeline.identify.enroll", lambda c, sid, pid, cfg=None: enrolled.append((sid, pid)))
    client.post("/speakers/3/name", data={"new_name": "Casey Sample"}, headers=HX)
    pid = one(conn, "SELECT person_id FROM speaker WHERE id = 3")
    assert one(conn, "SELECT name FROM person WHERE id = ?", pid) == "Casey Sample"
    assert enrolled == [(3, pid)]
    assert one(conn, "SELECT status FROM review_item WHERE kind = 'speaker' AND ref_id = 3") == "done"
    page = client.get("/conversations/1").text
    assert '>Casey Sample</span><span data-edit-url="/segments/4/text"' in page
    client.post("/speakers/3/name", data={"new_name": "casey sample"}, headers=HX)  # typed again: matched, not duplicated
    assert one(conn, "SELECT count(*) FROM person WHERE lower(name) = 'casey sample'") == 1


def test_this_isnt_x_unassigns_and_reopens_review(env):
    client, conn, _ = env
    client.post("/speakers/1/unassign", headers=HX)
    assert one(conn, "SELECT person_id FROM speaker WHERE id = 1") is None
    assert one(conn, "SELECT count(*) FROM review_item WHERE kind = 'speaker' AND ref_id = 1 AND status = 'open'") == 1


def test_change_task_person_moves_between_lists_and_counts_as_wrong(env):
    client, conn, _ = env
    client.post("/tasks/2/owner", data={"person_id": 1}, headers=HX)   # Alex's task was really Brian's
    assert one(conn, "SELECT owner_person_id FROM task WHERE id = 2") == 1
    assert one(conn, "SELECT rated FROM task WHERE id = 2") == "wrong"
    page = client.get("/conversations/1").text
    mine = page[page.index("<h2>You owe</h2>"):page.index("<h2>They owe you</h2>")]
    assert "Send the budget" in mine
    client.post("/tasks/1/owner", data={"new_name": "Dana Demo"}, headers=HX)
    assert one(conn, "SELECT p.name FROM task t JOIN person p ON p.id = t.owner_person_id WHERE t.id = 1") == "Dana Demo"


def test_change_fact_person(env):
    client, conn, _ = env
    client.post("/facts/1/person", data={"person_id": 1}, headers=HX)
    assert one(conn, "SELECT person_id FROM person_fact WHERE id = 1") == 1
    assert one(conn, "SELECT person_name FROM person_fact WHERE id = 1") == "Brian"


def test_merge_moves_every_reference_and_combines_voices(env):
    client, conn, _ = env
    t = db.now()
    conn.execute("INSERT INTO person (id, name, aliases, created_at) VALUES (9, 'Alex', '[\"Al\"]', ?)", (t,))
    conn.execute("UPDATE speaker SET person_id = 9 WHERE id = 3")
    conn.execute("INSERT INTO task (conversation_id, title, owner_person_id, created_at, updated_at) VALUES (1, 'Call back', 9, ?, ?)", (t, t))
    conn.execute("INSERT INTO person_fact (person_id, conversation_id, value, created_at) VALUES (9, 1, 'Likes tea', ?)", (t,))
    conn.execute("INSERT INTO decision (conversation_id, text, people, created_at) VALUES (1, 'Ship it', '[9, 2]', ?)", (t,))
    conn.execute("INSERT INTO voiceprint (person_id, embedding, sample_count, updated_at) VALUES (9, ?, 1, ?)", (vec(1, 0), t))
    conn.execute("INSERT INTO voiceprint (person_id, embedding, sample_count, updated_at) VALUES (2, ?, 3, ?)", (vec(0, 1), t))

    pv = people.preview(conn, 9, 2)
    assert pv["tasks"] == 1 and pv["notes"] == 1 and pv["conversations"] == 1 and pv["has_voice"]
    page = client.get("/people/9/merge?into=2").text
    assert "Merge <b>Alex / Al</b> into <b>Alex Example</b>" in page

    client.post("/people/9/merge", data={"into": 2})
    assert one(conn, "SELECT count(*) FROM person WHERE id = 9") == 0
    assert one(conn, "SELECT person_id FROM speaker WHERE id = 3") == 2
    assert one(conn, "SELECT owner_person_id FROM task WHERE title = 'Call back'") == 2
    assert one(conn, "SELECT person_id FROM person_fact WHERE value = 'Likes tea'") == 2
    assert json.loads(one(conn, "SELECT people FROM decision WHERE text = 'Ship it'")) == [2]
    assert set(json.loads(one(conn, "SELECT aliases FROM person WHERE id = 2")) ) >= {"Alex", "Al"}
    vp = conn.execute("SELECT embedding, sample_count FROM voiceprint WHERE person_id = 2").fetchall()
    assert len(vp) == 1 and vp[0]["sample_count"] == 4
    a = array("f"); a.frombytes(vp[0]["embedding"])
    assert a[1] > a[0] > 0                                           # weighted toward the 3-sample print
    assert one(conn, "SELECT count(*) FROM search_index WHERE kind = 'person' AND ref_id = 9") == 0


def test_brian_is_never_merged_away(env):
    client, conn, _ = env
    with pytest.raises(ValueError):
        people.merge(conn, 1, 2)
    assert client.post("/people/1/merge", data={"into": 2}).status_code == 400
    people.merge(conn, 2, 1)                                         # others can merge into Brian
    assert one(conn, "SELECT owner_person_id FROM task WHERE id = 2") == 1


def test_duplicate_suggestions(env):
    client, conn, _ = env
    conn.execute("INSERT INTO person (name, created_at) VALUES ('Alex', ?)", (db.now(),))
    assert any(d["into_name"] == "Alex Example" for d in people.duplicate_suggestions(conn))
    assert "Possibly the same person" in client.get("/people").text


def test_transcript_only_label_and_private(env):
    client, conn, cfg = env
    t = db.now()
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, title, status) "
                 "VALUES (7, 'recorder', '/y', 'def', ?, 'Oct 16 at 09_29', 'awaiting_approval')", (t,))
    conn.execute("INSERT INTO review_item (kind, ref_id, payload, created_at) VALUES ('approval', 7, ?, ?)",
                 (json.dumps({"recording_id": 7, "title": "Oct 16 at 09_29", "minutes": 5, "ai_cost": 0.01}), t))
    assert "Transcript Only (No AI)" in client.get("/review").text
    rid = one(conn, "SELECT id FROM review_item WHERE ref_id = 7 AND kind = 'approval'")
    client.post(f"/review/{rid}/approve", data={"ai": "0"})
    assert one(conn, "SELECT private FROM recording WHERE id = 7") == 1


def test_transcribe_plus_ai_needs_a_confirm_step(env, monkeypatch):
    client, conn, _ = env
    approved = []
    monkeypatch.setattr("scribe_bot.pipeline.ingest.approve", lambda c, rid, ai=True, meeting_type=None: approved.append((rid, ai, meeting_type)))
    t = db.now()
    conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, title, duration_s, status) "
                 "VALUES (8, 'recorder', '/z', 'ghi', ?, 'Oct 17 at 10_00', 1080, 'awaiting_approval')", (t,))
    conn.execute("INSERT INTO review_item (kind, ref_id, payload, created_at) VALUES ('approval', 8, ?, ?)",
                 (json.dumps({"recording_id": 8, "title": "Oct 17 at 10_00", "minutes": 18, "ai_cost": 0.02}), t))
    rid = one(conn, "SELECT id FROM review_item WHERE ref_id = 8 AND kind = 'approval'")
    assert f'hx-get="/review/{rid}/confirm"' in client.get("/review").text
    page = client.post(f"/review/{rid}/approve", data={"ai": "1"}).text           # a stray post never spends
    assert approved == [] and "This will cost about <b>$0.02</b>" in page and "Claude Sonnet" in page
    assert 'id="mt-' in client.get("/review").text                                 # type picker on the card itself
    page = client.get(f"/review/{rid}/confirm?meeting_type=learning").text
    assert '<option value="learning" selected>' in page                            # carried into the confirm step
    client.post(f"/review/{rid}/approve", data={"ai": "1", "confirm": "1", "meeting_type": "learning"})
    assert approved == [(8, True, "learning")]


def test_header_shows_local_date_and_recording_name(env):
    page = env[0].get("/conversations/1").text
    assert "Recording: Weekly sync" in page and " min</span>" in page


def test_help_card_dismiss(env):
    client, conn, _ = env
    assert "How to use Scribe Bot" in client.get("/").text
    client.post("/help/dismiss")
    assert "How to use Scribe Bot" not in client.get("/").text


def test_change_one_lines_speaker(env):
    client, conn, _ = env
    sid = one(conn, "SELECT id FROM segment WHERE idx = 3")                          # "Quick interruption", unnamed Speaker 3
    assert f'hx-get="/segments/{sid}/edit"' in client.get("/conversations/1").text     # the fix panel loads on first open
    assert f'action="/segments/{sid}/speaker"' in client.get(f"/segments/{sid}/edit").text
    client.post(f"/segments/{sid}/speaker", data={"speaker_id": 1})                  # it was Alex
    assert one(conn, "SELECT speaker_id FROM segment WHERE id = ?", sid) == 1
    assert one(conn, "SELECT talk_seconds FROM speaker WHERE id = 1") == 1204         # the line's 4 s moved with it
    assert one(conn, "SELECT talk_seconds FROM speaker WHERE id = 3") == 96
    assert one(conn, "SELECT count(*) FROM segment WHERE speaker_id = 2") == 1        # other lines untouched
    client.post(f"/segments/{sid}/speaker", data={"new_name": "Pat Example"})        # someone not in the conversation yet
    new = one(conn, "SELECT speaker_id FROM segment WHERE id = ?", sid)
    assert one(conn, "SELECT p.name FROM speaker s JOIN person p ON p.id = s.person_id WHERE s.id = ?", new) == "Pat Example"
    assert "Pat Example" in client.get("/conversations/1").text
    assert client.post(f"/segments/{sid}/speaker", data={"speaker_id": 999}).status_code == 400
    assert client.post("/segments/9999/speaker", data={"speaker_id": 1}).status_code == 404


def test_edit_a_line_and_teach_the_glossary(env):
    client, conn, _ = env
    sid = one(conn, "SELECT id FROM segment WHERE idx = 1")
    # the synthetic seed already has Zephyr, heard as "zefir"
    client.post(f"/segments/{sid}/text", data={"text": "Sure. Let's talk about the Zephyr launch.", "heard": "zeffer", "correct": "Zephyr"})
    assert one(conn, "SELECT text FROM segment WHERE id = ?", sid) == "Sure. Let's talk about the Zephyr launch."
    assert one(conn, "SELECT text FROM search_index WHERE kind = 'segment' AND ref_id = ?", sid).endswith("Zephyr launch.")
    assert one(conn, "SELECT count(*) FROM search_index WHERE kind = 'segment' AND ref_id = ?", sid) == 1   # replaced, not doubled
    assert json.loads(one(conn, "SELECT variants FROM glossary_term WHERE term = 'Zephyr'")) == ["zefir", "zeffer"]  # merged
    client.post(f"/segments/{sid}/text", data={"text": "Sure."})                     # no glossary fields: glossary unchanged
    assert one(conn, "SELECT count(*) FROM glossary_term") == 1
    assert client.post(f"/segments/{sid}/text", data={"text": "  "}).status_code == 400


def test_copy_summary_recap(env):
    client, conn, _ = env
    page = client.get("/conversations/1").text
    assert 'data-copy="#recap"' in page
    recap = page.split('<textarea id="recap"')[1].split("</textarea>")[0]
    assert "Weekly sync with Alex" in recap and "zephyr launch plan" in recap and "Key points:" in recap
    assert "Decisions:" in recap and "Launch moves to May" in recap
    for (fact,) in conn.execute("SELECT value FROM person_fact"):
        assert fact not in recap                                                   # people notes stay out
    conn.execute("UPDATE conversation SET summary = NULL WHERE id = 1")
    assert 'data-copy="#recap"' not in client.get("/conversations/1").text          # nothing to copy yet


def test_open_items_per_person_can_be_acted_on(env):
    client, conn, _ = env
    page = client.get("/people/2").text
    tid = one(conn, "SELECT id FROM task WHERE title = 'Send the budget'")
    assert 'id="person-open"' in page and f'action="/tasks/{tid}/done"' in page and 'hx-target="#person-open"' in page
    assert "you owe 2" in client.get("/people").text and "owes you 1" in client.get("/people").text
    client.post(f"/tasks/{tid}/done")
    assert "Send the budget" not in client.get("/people/2").text.split('id="person-open"')[1].split('id="person-facts"')[0]


def test_secondary_actions_live_in_menus(env):
    page = env[0].get("/conversations/1").text
    row = page.split('id="task-1"')[1].split('id="task-')[0]
    visible, clock, menu = row.split('class="row-menu"')                         # Done · ⏰ Remind me · ⋯ (last)
    assert 'action="/tasks/1/remind"' in clock and "Remove<" not in clock
    assert "/tasks/1/done" in visible and "Remove<" not in visible and "Change person" not in visible
    assert "Remove<" in menu and "Change person" in menu and "/tasks/1/edit" in menu
    head = page.split('class="conv-controls"')[1].split("</header>")[0]
    assert "Move to Trash" in head.split('class="row-menu"')[1] and "Mark private" in head.split('class="row-menu"')[1]


def test_naming_a_speaker_fixes_speaker_n_in_the_ai_text(env, monkeypatch):
    client, conn, _ = env
    monkeypatch.setattr("scribe_bot.web.routes.identify.enroll", lambda *a, **k: None)
    conn.execute("UPDATE conversation SET summary = 'Brian spoke with Speaker 3 about the launch; speaker 3 will call Speaker 30.', "
                 "takeaways = '[\"Speaker 3 owns the budget\"]' WHERE id = 1")
    conn.execute("INSERT INTO person_fact (person_name, conversation_id, value, created_at) VALUES ('Speaker 3', 1, 'Likes tea', 'x')")
    assert "Speaker 3" in client.get("/conversations/1").text.split('id="speakers"')[1].split('class="conv-controls"')[0]   # chip = summary's N
    client.post("/speakers/3/name", data={"new_name": "Pat Example"})
    summary = one(conn, "SELECT summary FROM conversation WHERE id = 1")
    assert summary == "Brian spoke with Pat Example about the launch; Pat Example will call Speaker 30."   # not Speaker 30
    assert one(conn, "SELECT takeaways FROM conversation WHERE id = 1") == '["Pat Example owns the budget"]'
    pid = one(conn, "SELECT person_id FROM speaker WHERE id = 3")
    assert one(conn, "SELECT person_id FROM person_fact WHERE value = 'Likes tea'") == pid      # note moved to the person
    assert one(conn, "SELECT count(*) FROM search_index WHERE kind = 'summary' AND text LIKE '%Pat Example%'") == 1
