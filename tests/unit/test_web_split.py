"""Fixing who said what inside a line (speaker fix A, 2026-11-20): split, "Who said this?", join, undo (synthetic only)."""
from scribe_bot.web.routes import _word_bounds
from test_web_pages import env, one  # noqa: F401


def lines(conn, cid=1):
    return [tuple(r) for r in conn.execute("SELECT idx, speaker_id, text FROM segment WHERE conversation_id = ? ORDER BY idx", (cid,))]


def state(conn):
    return (lines(conn), [tuple(r) for r in conn.execute("SELECT id, talk_seconds FROM speaker ORDER BY id")],
            one(conn, "SELECT count(*) FROM search_index WHERE kind = 'segment'"))


def last_undo(conn):
    return one(conn, "SELECT max(id) FROM undo_action")


def test_word_bounds():
    t = "Hi. Hi. How are you?"
    assert _word_bounds(t, 4, 6) == (4, 7)        # half a word counts as the word
    assert _word_bounds(t, 3, 8) == (4, 7)        # spaces around the selection are dropped
    assert _word_bounds(t, 9, 9) == (8, 8)        # a cursor snaps to the start of its word
    assert _word_bounds(t, 7, 4) == (4, 7)


def setup_line(conn, text="Hi. Hi. How are you?"):
    sid = one(conn, "SELECT id FROM segment WHERE conversation_id = 1 AND idx = 1")
    conn.execute("UPDATE segment SET text = ?, start_s = 10, end_s = 30 WHERE id = ?", (text, sid))
    return sid


def test_who_said_this_splits_and_moves_the_words(env):
    client, conn, _ = env
    sid = setup_line(conn)
    before = state(conn)
    n = len(lines(conn))
    owner = one(conn, "SELECT speaker_id FROM segment WHERE id = ?", sid)
    other = one(conn, "SELECT id FROM speaker WHERE conversation_id = 1 AND id != ? LIMIT 1", owner)
    r = client.post(f"/segments/{sid}/split", data={"start": 4, "end": 7, "speaker_id": other}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#L2")
    ls = lines(conn)
    assert len(ls) == n + 2 and [x[0] for x in ls] == list(range(len(ls)))
    assert ls[1] == (1, owner, "Hi.") and ls[2] == (2, other, "Hi.") and ls[3] == (3, owner, "How are you?")
    times = conn.execute("SELECT start_s, end_s FROM segment WHERE conversation_id = 1 AND idx BETWEEN 1 AND 3 ORDER BY idx").fetchall()
    assert times[0][0] == 10 and times[-1][1] == 30 and all(a[1] == b[0] for a, b in zip(times, times[1:]))
    moved = times[1][1] - times[1][0]
    assert one(conn, "SELECT talk_seconds FROM speaker WHERE id = ?", other) == dict(before[1])[other] + moved
    assert one(conn, "SELECT count(*) FROM search_index WHERE kind = 'segment' AND text = 'How are you?'") == 1
    client.post(f"/undo/{last_undo(conn)}")
    assert state(conn) == before


def test_split_here_keeps_the_speaker_and_new_name_makes_a_speaker(env):
    client, conn, _ = env
    sid = setup_line(conn)
    before = state(conn)
    client.post(f"/segments/{sid}/split", data={"start": 8, "end": 8})
    ls = lines(conn)
    assert ls[1][2] == "Hi. Hi." and ls[2][2] == "How are you?" and ls[1][1] == ls[2][1]
    client.post(f"/undo/{last_undo(conn)}")
    assert state(conn) == before
    speakers = one(conn, "SELECT count(*) FROM speaker")
    client.post(f"/segments/{sid}/split", data={"start": 0, "end": 20, "new_name": "Pat Example"})
    assert len(lines(conn)) == len(before[0])                       # whole line selected: no split, just a new speaker
    assert one(conn, "SELECT count(*) FROM speaker") == speakers + 1
    client.post(f"/undo/{last_undo(conn)}")
    assert state(conn) == before and one(conn, "SELECT count(*) FROM speaker") == speakers


def test_join_with_line_above_and_undo(env):
    client, conn, _ = env
    before = state(conn)
    sid = one(conn, "SELECT id FROM segment WHERE conversation_id = 1 AND idx = 2")
    above = lines(conn)[1][2]
    r = client.post(f"/segments/{sid}/join", follow_redirects=False)
    assert r.headers["location"].endswith("#L1")
    ls = lines(conn)
    assert len(ls) == len(before[0]) - 1 and [x[0] for x in ls] == list(range(len(ls)))
    assert ls[1][2] == f"{above} {before[0][2][2]}"
    assert one(conn, "SELECT count(*) FROM segment WHERE id = ?", sid) == 0
    client.post(f"/undo/{last_undo(conn)}")
    assert state(conn) == before
    first = one(conn, "SELECT id FROM segment WHERE conversation_id = 1 AND idx = 0")
    assert client.post(f"/segments/{first}/join").status_code == 400


def test_line_panel_offers_join_and_page_has_select_hint(env):
    client, conn, _ = env
    sid = one(conn, "SELECT id FROM segment WHERE conversation_id = 1 AND idx = 2")
    assert f'action="/segments/{sid}/join"' in client.get(f"/segments/{sid}/edit").text
    page = client.get("/conversations/1").text
    assert "data-split-speakers" in page and "tap a <b>coloured name</b>" in page and "<b>✂ split</b>" in page


def two_person(conn):
    """Conversation 1 as a 1:1: lines alternate between speakers 1 and 2."""
    conn.execute("UPDATE segment SET speaker_id = CASE WHEN idx % 2 = 0 THEN 1 ELSE 2 END WHERE conversation_id = 1")
    conn.execute("DELETE FROM speaker WHERE conversation_id = 1 AND id NOT IN (1, 2) AND id NOT IN (SELECT speaker_id FROM segment WHERE speaker_id IS NOT NULL)")


def test_tap_a_word_in_a_one_on_one_goes_to_the_other_person_and_joins_up(env):
    client, conn, _ = env
    two_person(conn)
    sid = setup_line(conn)                     # line 1 (speaker 2): "Hi. Hi. How are you?"; line 2 is speaker 1
    before = state(conn)
    nxt = lines(conn)[2][2]
    r = client.post(f"/segments/{sid}/flip", data={"at": 2}, follow_redirects=False)   # "How are you?" was speaker 1
    assert r.status_code == 303
    ls = lines(conn)
    assert ls[1] == (1, 2, "Hi. Hi.") and ls[2] == (2, 1, "How are you?") and ls[3][2] == nxt   # separate lines stay
    assert len(ls) == len(before[0]) + 1 and [x[0] for x in ls] == list(range(len(ls)))
    client.post(f"/segments/{one(conn, 'SELECT id FROM segment WHERE conversation_id = 1 AND idx = 2')}/flip", data={"at": 0})
    assert lines(conn)[1] == (1, 2, "Hi. Hi. How are you?")                             # pieces of one line join back
    client.post(f"/undo/{last_undo(conn)}")
    assert lines(conn)[2] == (2, 1, "How are you?")
    client.post(f"/undo/{last_undo(conn) - 1}")
    assert state(conn) == before
    client.post(f"/undo/{last_undo(conn)}")
    assert state(conn) == before


def test_tap_then_tap_back_gives_hi_hi_how_are_you(env):
    client, conn, _ = env
    two_person(conn)
    sid = setup_line(conn)
    client.post(f"/segments/{sid}/flip", data={"at": 1})                                # second "Hi." onward: speaker 1
    tail = one(conn, "SELECT id FROM segment WHERE conversation_id = 1 AND idx = 2")
    assert one(conn, "SELECT text FROM segment WHERE id = ?", tail) == "Hi. How are you?"
    client.post(f"/segments/{tail}/flip", data={"at": 1})                               # "How are you?" back to speaker 2
    ls = lines(conn)
    assert ls[1][1:] == (2, "Hi.") and ls[2][1:] == (1, "Hi.") and ls[3][1:] == (2, "How are you?")


def test_flip_needs_a_pick_with_three_people_and_keeps_task_links(env):
    client, conn, _ = env
    sid = setup_line(conn)
    if one(conn, "SELECT count(DISTINCT speaker_id) FROM segment WHERE conversation_id = 1") > 2:
        assert client.post(f"/segments/{sid}/flip", data={"at": 1}).status_code == 400
    two_person(conn)
    client.post(f"/segments/{sid}/flip", data={"at": 2})
    piece = one(conn, "SELECT id FROM segment WHERE conversation_id = 1 AND idx = 2")
    conn.execute("UPDATE task SET source_segment_id = ? WHERE id = 1", (piece,))
    client.post(f"/segments/{piece}/flip", data={"at": 0})                               # joins back into line 1
    assert one(conn, "SELECT source_segment_id FROM task WHERE id = 1") == sid           # the task follows it


def test_fast_fixes_send_back_only_the_changed_lines(env):
    """Brian, 2026-11-20: fixes were slow because each one reloaded the whole page. With X-Fast the reply is a few lines."""
    client, conn, _ = env
    sid = setup_line(conn)
    before = state(conn)
    owner = one(conn, "SELECT speaker_id FROM segment WHERE id = ?", sid)
    other = one(conn, "SELECT id FROM speaker WHERE conversation_id = 1 AND id != ? LIMIT 1", owner)
    r = client.post(f"/segments/{sid}/flip", data={"at": 2, "speaker_id": other}, headers={"X-Fast": "1"}, follow_redirects=False)
    j = r.json()
    assert r.status_code == 200 and j["undo"]["label"].startswith("Fixed who said")
    got = {x["id"]: x for x in j["lines"]}
    db_rows = {r[0]: r for r in conn.execute("SELECT id, idx, speaker_id, text FROM segment WHERE conversation_id = 1")}
    assert got[sid]["text"] == "Hi. Hi." and any(x["text"] == "How are you?" and x["speaker_id"] == other for x in j["lines"])
    assert all(db_rows[i][1] == x["idx"] for i, x in got.items()) and len(j["lines"]) < len(db_rows) + 1
    assert {s[0] for s in j["speakers"]} >= {owner, other}
    client.post(f"/undo/{j['undo']['id']}")
    assert state(conn) == before


def test_tap_a_name_moves_a_block_of_lines(env):
    client, conn, _ = env
    ids = [r[0] for r in conn.execute("SELECT id FROM segment WHERE conversation_id = 1 ORDER BY idx LIMIT 2")]
    before = state(conn)
    new = client.post(f"/segments/{ids[0]}/speaker", data={"new_name": "Pat Example", "ids": ",".join(map(str, ids))},
                      headers={"X-Fast": "1"}).json()
    sp = {x["id"]: x["speaker_id"] for x in new["lines"]}
    assert sp[ids[0]] == sp[ids[1]] and new["undo"]["label"] == "Changed who said 2 lines"
    assert "Pat Example" in [s[1] for s in new["speakers"]]
    client.post(f"/undo/{new['undo']['id']}")
    assert state(conn)[0] == before[0]
