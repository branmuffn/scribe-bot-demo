"""Prompt v2.1: the AI writes a topic title; v2.2: it also files Work/Personal. Synthetic data only; providers are faked."""
from test_ai import GOOD, factory, make_conversation

from scribe_bot.ai import extract, prompts


def test_prompt_v21_keeps_v2_and_asks_for_a_title():
    system = prompts.extract_system("Brian")
    assert prompts.PROMPT_VERSION == "v2.2"
    assert system.startswith(prompts.EXTRACT_V2.format(me="Brian"))  # v2 wording unchanged
    assert "- title: 3 to 8 words" in system and '"title": str' in system


def test_clean_title():
    assert prompts.clean_title('  "Spring fair planning."  ') == "Spring fair planning"
    assert prompts.clean_title("x" * 200) == "x" * prompts.TITLE_MAX
    assert prompts.clean_title(None) is None and prompts.clean_title("   ") is None and prompts.clean_title(7) is None


def test_ai_title_replaces_the_fallback_and_is_searchable(conn):
    cid, _ = make_conversation(conn)
    rec = conn.execute("INSERT INTO recording (source, original_path, sha256, received_at, title) "
                       "VALUES ('recorder', '/x/Oct 3 at 14_05.m4a', 'abc', 'now', 'Oct 3 at 14_05')").lastrowid
    conn.execute("UPDATE conversation SET recording_id = ? WHERE id = ?", (rec, cid))
    out = extract.extract_conversation(conn, cid, factory=factory([{**GOOD, "title": "Spring fair planning"}]))
    assert out["status"] == "done"
    assert conn.execute("SELECT title FROM conversation WHERE id = ?", (cid,)).fetchone()[0] == "Spring fair planning"
    assert conn.execute("SELECT summary_type IS type FROM conversation WHERE id = ?", (cid,)).fetchone()[0] == 1   # F6
    assert conn.execute("SELECT title FROM recording WHERE id = ?", (rec,)).fetchone()[0] == "Oct 3 at 14_05"
    rows = conn.execute("SELECT kind, text FROM search_index WHERE conversation_id = ? AND kind = 'summary'", (cid,)).fetchall()
    assert ("summary", "Spring fair planning") in [tuple(r) for r in rows]


def test_missing_title_keeps_the_fallback(conn):
    cid, _ = make_conversation(conn)
    extract.extract_conversation(conn, cid, factory=factory([GOOD]))  # GOOD has no title
    assert conn.execute("SELECT title FROM conversation WHERE id = ?", (cid,)).fetchone()[0] == "Synthetic planning call"


def test_prompt_v22_asks_for_area_and_parses_it():
    system = prompts.extract_system("Brian")
    assert "- area: \"work\" if this conversation is mainly about Brian's job" in system and '"area_confidence": number' in system
    empty = '"decisions": [], "tasks": [], "waiting_on": [], "people_facts": []'
    assert prompts.parse_extract('{"summary": "s", "area": "work", "area_confidence": 0.9, %s}' % empty)["area_confidence"] == 0.9
    assert prompts.parse_extract('{"summary": "s", "area": "school", "area_confidence": 0.8, %s}' % empty)["area"] == "school"   # 2026-11-21
    bad = prompts.parse_extract('{"summary": "s", "area": "gym", "area_confidence": true, %s}' % empty)
    assert bad["area"] is None and bad["area_confidence"] is None


def test_ai_files_work_or_personal_when_sure_and_leaves_a_guess_when_not(conn):
    cid, _ = make_conversation(conn)
    conn.execute("UPDATE conversation SET area = NULL WHERE id = ?", (cid,))
    extract.extract_conversation(conn, cid, factory=factory([{**GOOD, "area": "personal", "area_confidence": 0.95}]))
    row = conn.execute("SELECT area, area_guess FROM conversation WHERE id = ?", (cid,)).fetchone()
    assert tuple(row) == ("personal", "personal")
    assert {r[0] for r in conn.execute("SELECT area FROM task WHERE conversation_id = ?", (cid,))} == {"personal"}
    cid2, _ = make_conversation(conn)
    conn.execute("UPDATE conversation SET area = NULL WHERE id = ?", (cid2,))
    extract.extract_conversation(conn, cid2, factory=factory([{**GOOD, "area": "work", "area_confidence": 0.5}]))
    assert tuple(conn.execute("SELECT area, area_guess FROM conversation WHERE id = ?", (cid2,)).fetchone()) == (None, "work")
    cid3, _ = make_conversation(conn)
    conn.execute("UPDATE conversation SET area = 'work' WHERE id = ?", (cid3,))   # Brian filed it: the AI never overrides
    extract.extract_conversation(conn, cid3, factory=factory([{**GOOD, "area": "personal", "area_confidence": 0.99}]))
    assert conn.execute("SELECT area FROM conversation WHERE id = ?", (cid3,)).fetchone()[0] == "work"
