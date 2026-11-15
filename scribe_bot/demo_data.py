"""Demo mode data: fill a fresh, separate data folder with made-up meetings so anyone can explore Scribe Bot.

    python -m scribe_bot.demo_data ./demo-data        (or just: scripts/demo.sh)

Everything here is synthetic: invented people, invented conversations, no audio, no keys, nothing sent anywhere.
It refuses to touch a database that holds any conversation not titled "DEMO: ...", so it can never mix with real data.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

from scribe_bot.core import db
from scribe_bot.core.settings import DEMO_MARKER
from scribe_bot.web import demo

MARKER = "DEMO: 1:1 with Priya, Q3 budget review"


class NotADemoDatabase(RuntimeError):
    pass


def _seed_meetings(conn: sqlite3.Connection) -> None:
    now = db.now()
    base = datetime.fromisoformat(now)

    def ago(days, hours=0):
        return (base - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")

    def due(days):
        return (base + timedelta(days=days)).date().isoformat()

    def person(name, area, color=None):
        return conn.execute("INSERT INTO person (name, area, notes, created_at, color) VALUES (?, ?, 'Demo person (made up).', ?, ?)",
                            (name, area, now, color)).lastrowid

    acct = conn.execute("INSERT INTO account (provider, nickname, owner, type, monthly_cap_usd, training_policy, may_train, enabled, "
                        "secret_ref, created_at) VALUES ('anthropic', 'Personal API (demo)', 'Me', 'paid', 10, "
                        "'no training (API default)', 0, 1, 'demo-no-key', ?)", (now,)).lastrowid
    gem = conn.execute("INSERT INTO account (provider, nickname, owner, type, monthly_cap_usd, training_policy, may_train, enabled, "
                       "secret_ref, created_at) VALUES ('google', 'Gemini free (demo)', 'Me', 'free', 0, "
                       "'may train', 1, 0, 'demo-no-key', ?)", (now,)).lastrowid

    priya = person("Priya Shah (demo)", "work", "violet")
    marcus = person("Marcus Lee (demo)", "work", "green")
    alex = person("Alex Rivera (demo)", "personal", "amber")

    def convo(title, days, ctype, area, people, lines, summary, takeaways=None, private=0, tasks=(), decisions=(), facts=(),
              cost=None):
        """people: cluster -> person id (cluster 0 = me). lines: (cluster, text)."""
        start = ago(days)
        dur = len(lines) * 9 + 30
        cid = conn.execute(
            "INSERT INTO conversation (title, started_at, duration_s, type, area, area_confidence, summary, takeaways, private, "
            "processed_by, status, created_at, updated_at, type_chosen) VALUES (?, ?, ?, ?, ?, 0.92, ?, ?, ?, ?, 'done', ?, ?, 1)",
            (title, start, dur * 20, ctype, area, summary, json.dumps(takeaways or []), private,
             "local" if private else "demo", start, start)).lastrowid
        talk = {}
        for c, _ in lines:
            talk[c] = talk.get(c, 0) + 9 * 20
        spk = {c: conn.execute("INSERT INTO speaker (conversation_id, cluster, person_id, confirmed, talk_seconds) "
                               "VALUES (?, ?, ?, 1, ?)", (cid, c, pid, talk.get(c, 0))).lastrowid for c, pid in people.items()}
        seg, t = [], 0.0
        for i, (c, text) in enumerate(lines):
            length = max(3.0, len(text.split()) / 2.6)
            seg.append(conn.execute("INSERT INTO segment (conversation_id, idx, start_s, end_s, speaker_id, text) "
                                    "VALUES (?, ?, ?, ?, ?, ?)", (cid, i, t, t + length, spk[c], text)).lastrowid)
            conn.execute("INSERT INTO search_index (kind, ref_id, conversation_id, text) VALUES ('segment', ?, ?, ?)",
                         (seg[-1], cid, text))
            t += length + 0.8
        for title_, owner, line, d in tasks:
            conn.execute("INSERT INTO task (conversation_id, title, owner_person_id, due, area, confidence, source_segment_id, "
                         "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0.9, ?, ?, ?)",
                         (cid, title_, owner, d, area, seg[line], start, start))
        for text, line, who in decisions:
            conn.execute("INSERT INTO decision (conversation_id, text, people, source_segment_id, created_at) VALUES (?, ?, ?, ?, ?)",
                         (cid, text, json.dumps(who), seg[line], start))
        for pid, name, ftype, value, line in facts:
            conn.execute("INSERT INTO person_fact (person_id, person_name, conversation_id, type, value, source_segment_id, "
                         "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)", (pid, name, cid, ftype, value, seg[line], start))
        if cost:
            conn.execute("INSERT INTO ledger_entry (time, account_id, provider, model, task, conversation_id, sent_summary, "
                         "tokens_in, tokens_out, cost_usd, training_policy, status) VALUES (?, ?, 'anthropic', 'claude-sonnet', "
                         "'extract', ?, ?, ?, ?, ?, 'no training (API default)', 'ok')",
                         (start, acct, cid, f"Transcript text of '{title}' ({len(lines)} lines); no audio", cost[0], cost[1],
                          round(cost[0] * 3e-6 + cost[1] * 15e-6, 4)))
        return cid

    convo(MARKER, 1, "1:1", "work", {0: 1, 1: priya}, [
        (1, "Thanks for making time. Um, I wanted to go over the Q3 budget before Friday."),
        (0, "Sure. Where do you think we're over?"),
        (1, "Travel, mostly. We're about twelve percent above plan."),
        (0, "Okay. What if we cut travel by ten percent and move the summit online?"),
        (1, "That works. I'll share the vendor quotes by Wednesday so we can compare."),
        (0, "Great. I'll send you the revised budget by Thursday."),
        (1, "Perfect. Unrelated, I'm running my first half marathon next month."),
        (0, "That's amazing. Good luck! How's training going?"),
        (1, "Slowly, but I'm getting there."),
    ], "Reviewed the Q3 budget. Travel is about 12% over plan; agreed to cut travel by 10% and run the summit online. "
       "Priya sends vendor quotes; I send the revised budget.",
       ["Cut travel budget by 10%", "Summit moves online"],
       tasks=[("Send the revised Q3 budget to Priya", 1, 5, due(2)), ("Share the vendor quotes", priya, 4, due(1))],
       decisions=[("Cut travel by 10% and hold the summit online", 3, [1, priya])],
       facts=[(priya, "Priya Shah (demo)", "event", "Running her first half marathon next month", 6)], cost=(5200, 900))

    convo("DEMO: Team weekly sync", 3, "multi", "work", {0: 1, 1: priya, 2: marcus}, [
        (0, "Okay, let's get started. Marcus, how's the onboarding bug?"),
        (2, "I found it. It's in the sign-up form. I'll have a fix out by Tuesday."),
        (0, "Nice. Priya, anything blocking you?"),
        (1, "Just the offsite. We still need a room."),
        (0, "Right, I'll book the offsite room. You know, I should've done that last week."),
        (2, "Should we move standup to ten?"),
        (0, "Yes, let's do that starting Monday. Any other questions?"),
    ], "Weekly sync: Marcus found the onboarding bug and will ship a fix; I book the offsite room; standup moves to 10 AM.",
       ["Standup moves to 10 AM from Monday"],
       tasks=[("Book the offsite room", 1, 4, due(-1)), ("Ship the onboarding bug fix", marcus, 1, due(1))],
       decisions=[("Standup moves to 10 AM starting Monday", 6, [1, priya, marcus])], cost=(4100, 700))

    convo("DEMO: Dinner plans with Alex", 2, "personal", "personal", {0: 1, 1: alex}, [
        (1, "Are we still on for Saturday?"),
        (0, "Yes! I'll reserve a table. Any preference?"),
        (1, "Somewhere vegetarian-friendly, I'm off meat these days."),
        (0, "Got it. Can you send me that list of places you liked?"),
        (1, "Sure, I'll text it tonight."),
    ], "Saturday dinner is on. I reserve a table; Alex sends a list of restaurants. Alex is vegetarian now.",
       tasks=[("Reserve a table for Saturday dinner", 1, 1, due(3)), ("Send the restaurant list", alex, 4, due(0))],
       facts=[(alex, "Alex Rivera (demo)", "preference", "Vegetarian now", 2)], cost=(1800, 400))

    convo("DEMO: Doctor follow-up (private)", 5, "voice_note", "personal", {0: 1}, [
        (0, "Notes from my check-up. Everything looks fine, follow up in six months."),
    ], None, private=1)

    convo("DEMO: Talk, Leading through uncertainty", 6, "learning", "work", {0: 1, 1: None}, [
        (1, "The best leaders I know say what they don't know, out loud."),
        (1, "Uncertainty isn't a reason to stop deciding. It's a reason to decide in smaller steps."),
        (1, "Write down your assumptions, then check them every month."),
        (0, "Um, how do you keep a team calm when the plan keeps changing?"),
        (1, "Tell them what won't change. People can handle a lot if the anchors stay put."),
    ], "A talk on leading when the future is unclear: name what you don't know, decide in small steps, and keep a few "
       "anchors fixed.",
       ["Say what you don't know, out loud", "Decide in smaller steps", "Review your assumptions monthly",
        "Tell the team what won't change"], cost=(3900, 600))

    conn.execute("INSERT INTO ledger_entry (time, account_id, provider, model, task, conversation_id, sent_summary, tokens_in, "
                 "tokens_out, cost_usd, training_policy, status) VALUES (?, ?, 'google', 'gemini-flash', 'extract', NULL, "
                 "'Blocked: this account may train on data, so it is never used for real conversations', 0, 0, 0, "
                 "'may train', 'blocked')", (ago(4), gem))
    # A recording waiting for approval (ask-first mode), so the Review flow has something to show. No audio file exists.
    conn.execute("INSERT INTO recording (source, original_path, sha256, duration_s, received_at, status, title) "
                 "VALUES ('recorder', 'demo/coffee-with-sam.m4a', 'demo-coffee-with-sam', 1920, ?, 'awaiting_approval', "
                 "'DEMO: Coffee with Sam')", (ago(0, 1),))
    conn.execute("UPDATE person SET last_spoken_at = ? WHERE name LIKE '%(demo)'", (ago(1),))


def seed(conn: sqlite3.Connection) -> int:
    """Load every demo meeting into an empty or demo-only database. Safe to run twice. Returns the conversation count."""
    real = conn.execute("SELECT count(*) FROM conversation WHERE title NOT LIKE 'DEMO:%'").fetchone()[0]
    if real:
        raise NotADemoDatabase("this database holds non-demo conversations; demo data goes in its own folder")
    demo.load(conn)   # the built-in one-conversation demo (Help page: Load / Remove)
    if not conn.execute("SELECT 1 FROM conversation WHERE title = ?", (MARKER,)).fetchone():
        with db.transaction(conn):
            _seed_meetings(conn)
    return conn.execute("SELECT count(*) FROM conversation").fetchone()[0]


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: python -m scribe_bot.demo_data <demo-data-folder>", file=sys.stderr)
        return 2
    folder = Path(argv[1]).expanduser().resolve()
    if folder == (Path.home() / "scribe-bot-data").resolve():
        print("refusing: that is the real data folder; pick a separate demo folder", file=sys.stderr)
        return 1
    conn = db.connect(folder / "scribe-bot.db")
    try:
        db.migrate(conn)
        n = seed(conn)
    except NotADemoDatabase as e:
        print(f"refusing: {e}", file=sys.stderr)
        return 1
    finally:
        conn.close()
    (folder / DEMO_MARKER).write_text("Demo mode data folder: made-up meetings only. Delete this folder to start over.\n")
    print(f"demo data ready in {folder}: {n} made-up conversations")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
