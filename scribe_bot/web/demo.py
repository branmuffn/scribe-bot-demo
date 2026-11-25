"""Demo data (Settings → Help): one clearly labelled, made-up conversation to explore safely. Synthetic names and text
only; no recording, no audio, nothing sent anywhere. Everything it creates is tagged so Remove deletes exactly that."""
from __future__ import annotations

import json
import sqlite3

from scribe_bot.core.db import now, transaction

TITLE = "DEMO: Product kickoff with Jordan"
DEMO_PERSON = "Jordan Sample (demo)"
LINES = [  # (speaker: 0 = Brian, 1 = Jordan), seconds, text
    (1, 0, "Thanks for making time. I wanted to walk through the spring launch plan."),
    (0, 6, "Sure. Where are we on the landing page?"),
    (1, 11, "Design is done. I'll send you the final mock-ups by Thursday."),
    (0, 18, "Great. I'll draft the announcement email and share it Friday."),
    (1, 25, "Perfect. Let's lock the launch date as April 14th."),
    (0, 31, "Agreed, April 14th it is. How was the trip to Lisbon, by the way?"),
    (1, 36, "Lovely, thanks. My daughter Maya loved the trams."),
]


def load(conn: sqlite3.Connection) -> int:
    existing = conn.execute("SELECT id FROM conversation WHERE title = ?", (TITLE,)).fetchone()
    if existing:
        return existing[0]
    t = now()
    with transaction(conn):
        pid = conn.execute("INSERT INTO person (name, area, notes, created_at) VALUES (?, 'work', 'Demo person (made up).', ?)",
                           (DEMO_PERSON, t)).lastrowid
        cid = conn.execute(
            "INSERT INTO conversation (title, started_at, duration_s, type, area, area_confidence, summary, takeaways, private, "
            "processed_by, status, created_at, updated_at) VALUES (?, ?, 45, '1:1', 'work', 0.9, ?, ?, 0, 'demo', 'done', ?, ?)",
            (TITLE, t, "A made-up kickoff: the landing page design is finished, mock-ups and an announcement email are on "
                       "their way, and the launch date is set for April 14th.", json.dumps(["Launch on April 14th"]), t, t)).lastrowid
        spk = {}
        for cluster, person, talk in ((0, 1, 20.0), (1, pid, 25.0)):
            spk[cluster] = conn.execute("INSERT INTO speaker (conversation_id, cluster, person_id, confirmed, talk_seconds) "
                                        "VALUES (?, ?, ?, 1, ?)", (cid, cluster, person, talk)).lastrowid
        seg = []
        for i, (who, start, text) in enumerate(LINES):
            seg.append(conn.execute("INSERT INTO segment (conversation_id, idx, start_s, end_s, speaker_id, text) VALUES (?, ?, ?, ?, ?, ?)",
                                    (cid, i, start, start + 5, spk[who], text)).lastrowid)
            conn.execute("INSERT INTO search_index (kind, ref_id, conversation_id, text) VALUES ('segment', ?, ?, ?)", (seg[-1], cid, text))
        for title, owner, line in (("Draft the announcement email and share it", 1, 3), ("Send the final landing page mock-ups", pid, 2)):
            conn.execute("INSERT INTO task (conversation_id, title, owner_person_id, area, confidence, source_segment_id, created_at, "
                         "updated_at) VALUES (?, ?, ?, 'work', 0.9, ?, ?, ?)", (cid, title, owner, seg[line], t, t))
        conn.execute("INSERT INTO decision (conversation_id, text, people, source_segment_id, created_at) VALUES (?, ?, ?, ?, ?)",
                     (cid, "Launch date is April 14th", json.dumps([1, pid]), seg[4], t))
        conn.execute("INSERT INTO person_fact (person_id, person_name, conversation_id, type, value, source_segment_id, created_at) "
                     "VALUES (?, ?, ?, 'family', 'Has a daughter named Maya; recently visited Lisbon', ?, ?)", (pid, DEMO_PERSON, cid, seg[6], t))
    return cid


def remove(conn: sqlite3.Connection) -> int:
    """Delete everything load() made (and only that). Returns the number of demo conversations removed."""
    ids = [r[0] for r in conn.execute("SELECT id FROM conversation WHERE title = ?", (TITLE,))]
    with transaction(conn):
        for cid in ids:
            conn.execute("DELETE FROM search_index WHERE conversation_id = ?", (cid,))
            conn.execute("DELETE FROM task WHERE conversation_id = ?", (cid,))
            conn.execute("DELETE FROM review_item WHERE conversation_id = ?", (cid,))
            conn.execute("DELETE FROM conversation WHERE id = ?", (cid,))
        for (pid,) in conn.execute("SELECT id FROM person WHERE name = ?", (DEMO_PERSON,)).fetchall():
            # F39: if the demo person was picked in a real talk, removing them failed ("changed on another device") and
            # took real tasks with them. Real talks keep their lines (speaker goes back to unnamed) and tasks (back to me).
            conn.execute("UPDATE speaker SET person_id = NULL, confirmed = 0 WHERE person_id = ?", (pid,))
            conn.execute("UPDATE task SET owner_person_id = 1 WHERE owner_person_id = ?", (pid,))
            conn.execute("DELETE FROM search_index WHERE kind = 'person' AND ref_id = ?", (pid,))
            conn.execute("DELETE FROM person WHERE id = ?", (pid,))
    return len(ids)
