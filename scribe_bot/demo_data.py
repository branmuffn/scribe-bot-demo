"""Demo mode data: fill a fresh, separate data folder with made-up meetings so anyone can explore Scribe Bot.

    python -m scribe_bot.demo_data ./demo-data        (or just: scripts/demo.sh)

Everything here is synthetic: invented people, invented conversations, no audio, no keys, nothing sent anywhere.
It refuses to touch a database that holds any conversation not titled "DEMO: ...", so it can never mix with real data.
"""
from __future__ import annotations

import json
import math
import random
import sqlite3
import struct
import sys
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scribe_bot.core import db
from scribe_bot.core.settings import DEMO_MARKER
from scribe_bot.web import demo

MARKER = "DEMO: 1:1 with Priya, Q3 budget review"


class NotADemoDatabase(RuntimeError):
    pass


def _seed_meetings(conn: sqlite3.Connection, folder: Path | None = None) -> None:
    now = db.now()
    base = datetime.fromisoformat(now)

    def ago(days, hours=0):
        return (base - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")

    def at(hhmm, days=0, weekday=None):
        """F35: the last time the clock read hhmm here (the pages show local time), `days` further back; weekday 6 = Sunday."""
        loc = base.astimezone()
        t = loc.replace(hour=int(hhmm[:2]), minute=int(hhmm[3:]), second=0, microsecond=0)
        if t > loc:
            t -= timedelta(days=1)
        while weekday is not None and t.weekday() != weekday:
            t -= timedelta(days=1)
        return (t - timedelta(days=days)).astimezone(timezone.utc).isoformat(timespec="seconds")

    def due(days):
        return (base + timedelta(days=days)).date().isoformat()

    def person(name, area, color=None):
        return conn.execute("INSERT INTO person (name, area, notes, created_at, color) VALUES (?, ?, 'Demo person (made up).', ?, ?)",
                            (name, area, now, color)).lastrowid

    # F38 (live pass): the first install names person 1 after the owner; on the public demo they are just "Me"
    conn.execute("UPDATE person SET name = 'Me' WHERE id = 1")
    # F16 (live pass): the first install already has one Claude account; the demo renames it instead of adding a second
    first = conn.execute("SELECT id FROM account WHERE provider = 'anthropic' ORDER BY id LIMIT 1").fetchone()
    if first:
        acct = first[0]
        conn.execute("UPDATE account SET nickname = 'Personal API (demo)', owner = 'Me', type = 'paid', monthly_cap_usd = 10, "
                     "training_policy = 'no training (API default)', may_train = 0, enabled = 1, secret_ref = 'demo-no-key' WHERE id = ?", (acct,))
    else:
        acct = conn.execute("INSERT INTO account (provider, nickname, owner, type, monthly_cap_usd, training_policy, may_train, enabled, "
                            "secret_ref, created_at) VALUES ('anthropic', 'Personal API (demo)', 'Me', 'paid', 10, "
                            "'no training (API default)', 0, 1, 'demo-no-key', ?)", (now,)).lastrowid
    gem = conn.execute("INSERT INTO account (provider, nickname, owner, type, monthly_cap_usd, training_policy, may_train, enabled, "
                       "secret_ref, created_at) VALUES ('google', 'Gemini free (demo)', 'Me', 'free', 0, "
                       "'may train', 1, 0, 'demo-no-key', ?)", (now,)).lastrowid

    priya = person("Priya Shah (demo)", "work", "violet")
    marcus = person("Marcus Lee (demo)", "work", "green")
    alex = person("Alex Rivera (demo)", "personal", "amber")

    def convo(title, days, ctype, area, people, lines, summary, takeaways=None, no_ai=0, tasks=(), decisions=(), facts=(),
              cost=None, hours=0, status="done"):
        """people: cluster -> person id (cluster 0 = me). lines: (cluster, text)."""
        start = ago(days, hours)
        dur = len(lines) * 9 + 30
        x = min(20, 75 * 60 / dur)   # F11: a made-up talk runs 75 minutes at most (long ones read as "427 min")
        cid = conn.execute(
            "INSERT INTO conversation (title, started_at, duration_s, type, area, area_confidence, summary, takeaways, private, "
            "processed_by, status, created_at, updated_at, type_chosen) VALUES (?, ?, ?, ?, ?, 0.92, ?, ?, ?, ?, ?, ?, ?, 1)",
            (title, start, round(dur * x), ctype, area, summary, json.dumps(takeaways or []), 0,
             "local" if no_ai else "demo", "transcribed" if no_ai and status == "done" else status, start, start)).lastrowid   # Transcript only = no AI step
        talk = {}
        for c, _ in lines:
            talk[c] = talk.get(c, 0) + round(9 * x)
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

    convo("DEMO: Doctor follow-up (transcript only)", 5, "voice_note", "personal", {0: 1}, [
        (0, "Notes from my check-up. Everything looks fine, follow up in six months."),
    ], None, no_ai=1)

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
    _seed_more(conn, convo, person, ago, due, folder, at)


# ---------- More of everything (Brian, 2026-11-25: the demo should look like real use, every screen filled) ----------
# Dozens of made-up talks over two months, long titles, up to 7 speakers, long transcripts, many tasks and tags, a full
# Inbox, notifications, Trash and every state. Built from a fixed random seed, so every demo instance is the same.

MORE_PEOPLE = [("Dana Okoro (demo)", "work", "blue"), ("Sam Whitaker (demo)", "work", "teal"),
               ("Dr. Evangeline Montgomery-Whitfield (demo)", "work", "rose"), ("Chris Tanaka (demo)", "work", None),
               ("Lena Fischer (demo)", "school", "indigo"), ("Omar Haddad (demo)", "work", "orange"),
               ("Grace Kim (demo)", "personal", "pink"), ("Theo Brandt (demo)", "other", None),
               ("Nadia Petrova (demo)", "work", "cyan"), ("Coach Riley (demo)", "personal", None)]
TOPICS = ["the launch checklist", "hiring for the support team", "the customer survey", "next quarter's roadmap",
          "the vendor contract", "the website redesign", "onboarding new staff", "the volunteer schedule",
          "the board update", "the grant report", "the move to the new office", "the training plan"]
SAY = ["Let's start with {t}. Where are we?", "I think we're mostly on track, but the timeline is tight.",
       "Can you send me the numbers by Friday?", "Sure, I'll pull them together tomorrow morning.",
       "What's blocking us right now?", "Mostly waiting on the other team to sign off.",
       "Okay, um, let's decide that today so it doesn't slip again.", "I'd rather keep it simple for the first version.",
       "Agreed. We can add the rest after we hear back from people.", "Who owns the follow-up on {t}?",
       "I can take it. I'll check in with everyone next week.", "Let's put a date on it: the end of the month.",
       "One more thing, the budget for {t} is still not final.", "I'll ask finance and get back to you.",
       "Good point. Let's write that down as a decision.", "Does anyone have questions before we wrap up?",
       "Not from me. This was really helpful, thanks.", "Can we move this meeting to Thursdays from now on?",
       "That works for me. I'll update the invite.", "Honestly I think we've been overthinking {t}.",
       "So, like, the main risk is that we run out of time.", "Then let's cut scope instead of moving the date.",
       "I'll draft a short note for the team about {t} today.", "Remind me, did we already promise this to the client?"]
# Varied made-up tasks (F9: the same six titles repeated down Today's lists)
TASKS = ["Send the follow-up notes on {t}", "Check the numbers for {t}", "Book a room for the next session",
         "Draft the plan for {t}", "Ask finance about the budget", "Share the slides with the group",
         "Write up the decision on {t}", "Call the vendor about the quote", "Update the shared calendar",
         "Get feedback from two customers", "Review the draft before Friday", "Order the supplies",
         "Set up the sign-up form", "Collect the receipts", "Send the thank-you notes", "Fix the typo in the handout",
         "Find a date for the offsite", "Ask legal about the contract terms", "Post the summary in the team chat",
         "Print the agenda", "Check in with the new hire", "Renew the software licence", "Reply to the parents' email",
         "Pick up the signed forms", "Move the deadline in the tracker", "Prepare three questions for {t}",
         "Compare the two quotes", "Book the photographer", "Test the new checklist", "Archive last year's files",
         "Confirm the catering order"]
TAGS = ["Planning", "Hiring", "Budget", "Clients", "Follow-up", "Ideas", "Family", "Health", "Learning", "Volunteering"]
LONG = " and what we still need to settle before the end of the month"
WAITING = [("Recording Nov 24 at 4_05 PM", 1, False), ("Standup notes", 3, False),
           ("Call with the landlord about the lease renewal and the parking spot", 5, False),
           ("Voice memo", 8, False), ("Lunch and learn: writing clear emails", 26, False), ("Private: health check-in", 2, True),
           ("Recording Nov 18 at 9_30 AM", 40, None), ("Recording Nov 12 at 2_15 PM", 12, None)]   # None: older (backlog)


def _tone(path: Path, seconds: float, freq: float) -> None:
    """A soft tone stands in for audio (the demo has no real recordings), so ▶ Play has something to play."""
    rate = 8000
    frames = b"".join(struct.pack("<h", int(1500 * math.sin(2 * math.pi * freq * i / rate))) for i in range(int(seconds * rate)))
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate), w.writeframes(frames)


def _seed_more(conn, convo, person, ago, due, folder: Path | None, at) -> None:
    from scribe_bot.pipeline import ingest
    rnd = random.Random(2026)
    now = db.now()
    people = [person(n, a, c) for n, a, c in MORE_PEOPLE]
    old = [r[0] for r in conn.execute(f"SELECT id FROM person WHERE name LIKE '%(demo)' AND id NOT IN ({','.join(map(str, people))})")]
    everyone = people + old
    tag_ids = {n: conn.execute("INSERT INTO tag (name, created_at) VALUES (?, ?)", (n, now)).lastrowid for n in TAGS}
    kinds = [("1:1", "1:1 with {p} about {t}"), ("multi", "Team meeting: {t}"), ("multi", "Planning session for {t}"),
             ("learning", "Workshop: {t}"), ("personal", "Catch-up with {p}"), ("1:1", "Weekly check-in with {p}")]
    made = []
    for n in range(36):
        days = 7 + n * 1.6 + rnd.random()
        ctype, pattern = kinds[n % len(kinds)]
        topic = TOPICS[n % len(TOPICS)]
        size = {"1:1": 2, "personal": 2, "learning": 2}.get(ctype) or rnd.randint(3, 7)
        crew = rnd.sample(everyone, size - 1)
        name = conn.execute("SELECT name FROM person WHERE id = ?", (crew[0],)).fetchone()[0].replace(" (demo)", "")
        title = "DEMO: " + pattern.format(p=name.split()[0] if not name.startswith("Dr.") else name, t=topic) + (LONG if n % 5 == 2 else "")
        area = "personal" if ctype == "personal" else ["work", "work", "school", "other"][n % 4]
        cl = {0: 1, **{i + 1: pid for i, pid in enumerate(crew)}}
        if n % 6 == 3:
            cl[len(cl)] = None   # a voice nobody has named yet ("1 to name")
        lines = [(rnd.choice(list(cl)), rnd.choice(SAY).format(t=topic)) for _ in range(rnd.randint(18, 90 if n % 4 else 140))]
        no_ai = n % 7 == 4
        tasks = []
        if not no_ai:
            for k in range(rnd.randint(0, 5)):
                who = rnd.choice([1, 1] + crew)
                tasks.append((TASKS[(n * 5 + k * 7) % len(TASKS)].format(t=topic) +
                              (" before the next meeting with everyone who joined" if k == 3 and n % 3 == 0 else ""),
                              who, rnd.randrange(len(lines)), due(rnd.randint(-10, 14)) if k % 3 else None))
        cid = convo(title, days, ctype, area, cl, lines,
                    None if no_ai else f"Talked through {topic}. Agreed on next steps and who owns them; a few questions "
                                        f"are still open and come back next time.",
                    None if no_ai else [f"Next steps for {topic} are agreed", "Open questions come back next week"],
                    no_ai=int(no_ai), tasks=tasks,
                    decisions=[] if no_ai else [(f"Keep {topic} simple for the first version", rnd.randrange(len(lines)), [1])] * (n % 2),
                    facts=[(crew[0], name + " (demo)", "preference", "Prefers short meetings", 1)] if n % 8 == 1 else (),
                    cost=None if no_ai else (rnd.randint(3000, 30000), rnd.randint(500, 2500)), hours=rnd.randint(0, 9))
        made.append(cid)
        for tg in rnd.sample(TAGS, n % 4):
            conn.execute("INSERT INTO conversation_tag (conversation_id, tag_id, by_ai, created_at) VALUES (?, ?, ?, ?)",
                         (cid, tag_ids[tg], n % 2, now))
        if n > 3:   # older talks were read already (no "New" badge)
            conn.execute("UPDATE conversation SET opened_at = ? WHERE id = ?", (now, cid))
    # Tasks in every tab: some done, some no longer needed, some overdue (the due dates above), some others owe you
    for i, (tid,) in enumerate(conn.execute(f"SELECT id FROM task WHERE conversation_id IN ({','.join(map(str, made))})").fetchall()):
        if i % 4 == 0:
            conn.execute("UPDATE task SET status = 'done', updated_at = ? WHERE id = ?", (now, tid))
        elif i % 9 == 1:
            conn.execute("UPDATE task SET status = 'dropped', closed_as = 'not_needed', updated_at = ? WHERE id = ?", (now, tid))
    conn.execute("INSERT INTO task (conversation_id, title, owner_person_id, due, area, confidence, created_at, updated_at) "
                 "VALUES (NULL, 'Renew the parking permit', 1, ?, 'personal', 1, ?, ?)", (due(4), now, now))
    # The Unsure tab: tasks the AI wasn't sure about (who owns it, or whether it's a task at all)
    for i, cid in enumerate(made[1:12:4]):
        row = conn.execute("SELECT id FROM task WHERE conversation_id = ? AND status = 'open' ORDER BY id LIMIT 1",
                           (cid,)).fetchone()
        if row:   # as extract.py does: the flag on the task plus its review item, so Today's count matches the tab
            conn.execute("UPDATE task SET inbox = 1, confidence = 0.4 WHERE id = ?", (row[0],))
            conn.execute("INSERT INTO review_item (kind, conversation_id, ref_id, payload, created_at) "
                         "VALUES ('inbox', ?, ?, ?, ?)", (cid, row[0], json.dumps({"reason": "low confidence"}), now))
    # F19 (live pass): Backups & schedules were empty. A week of made-up nightly backups to two drives, one restore test,
    # and each schedule's last run (one failed, so the "failed" look can be seen too)
    for d in range(6, -1, -1):   # F33: oldest first, as real runs are saved, so "last backup" is today's and not a week old
        for dest, size in (("Drive A (demo)", 41_000_000 + d * 900_000), ("Drive B (demo)", 41_000_000 + d * 900_000)):
            conn.execute("INSERT INTO backup_run (destination, kind, started_at, finished_at, bytes, status) VALUES (?, 'backup', ?, ?, ?, 'ok')",
                         (dest, at("02:00", d), at("02:03", d), size))
    # F35: each job's last run matches its own time ("daily 2:00 AM · last ran … 2:00 AM", not "… 11:05 pm")
    conn.execute("INSERT INTO backup_run (destination, kind, started_at, finished_at, bytes, status) VALUES "
                 "('Drive A (demo)', 'restore_test', ?, ?, 41000000, 'ok')", (at("03:00", 12), at("03:12", 12)))
    for job, hhmm, days, weekday in (("export", "01:30", 0, None), ("trash-purge", "01:45", 0, None), ("backup", "02:00", 0, None),
                                     ("reconcile", "02:30", 0, None), ("original_cleanup", "03:00", 0, None),
                                     ("restore-test", "03:00", 12, None), ("briefing", "07:00", 0, None),
                                     ("task-cleanup", "17:30", 0, 6), ("weekly-review", "18:00", 0, 6)):
        conn.execute("INSERT OR REPLACE INTO schedule_run (job_name, last_success_at, last_attempt_at, status) VALUES (?, ?, ?, ?)",
                     (job, at(hhmm, days + (1 if job == "reconcile" else 0), weekday), at(hhmm, days, weekday),
                      "failed: the phone folder was offline (made up)" if job == "reconcile" else "ok"))
    # F18 (live pass): the Glossary was empty. Three made-up terms, plus one suggestion waiting in Review
    for term, variants, ok in (("Northwind", ["north wind", "nor wind"], 1), ("OKR", ["okay are", "oh kay are"], 1),
                               ("Priya", ["pria", "pre ya"], 1), ("Kubernetes", ["cooper netties"], 0)):
        gid = conn.execute("INSERT INTO glossary_term (term, variants, approved, created_at) VALUES (?, ?, ?, ?)",
                           (term, json.dumps(variants), ok, now)).lastrowid
        if not ok:
            conn.execute("INSERT INTO review_item (kind, conversation_id, ref_id, payload, created_at) VALUES ('glossary', ?, ?, ?, ?)",
                         (made[2], gid, json.dumps({"term": term}), now))
    # F24 (live pass): Add recording › Recent said "Nothing captured yet"; a few made-up shares and notes
    for kind, text, when in (("text", "Ask Priya (demo) whether the Q3 numbers include the contractor costs", ago(0, 2)),
                              ("link", "https://example.com/articles/better-one-on-ones (made up)", ago(1, 5)),
                              ("text", "Gift idea for Sam (demo): a cast-iron pan", ago(3, 1)),
                              ("link", "https://example.com/recipes/weeknight-chili (made up)", ago(6, 4))):
        conn.execute("INSERT INTO capture (kind, text, created_at, processed) VALUES (?, ?, ?, 1)", (kind, text, when))

    # F27 (live pass): Search found only transcript lines on the demo; the made-up summaries, tasks, decisions, notes,
    # people and captures are now indexed the way the app indexes its own (extract.reindex_ai_text, people, capture)
    _index_everything(conn)

    # F22 (live pass): the Status page said "Needs attention", "No readings yet" and "Worker not running" (there is no
    # worker or M14 here). Thirty days of made-up healthy M14 readings, every 15 minutes, so the charts have a shape.
    _machine_rows(conn, datetime.now(timezone.utc) - timedelta(days=30), datetime.now(timezone.utc), timedelta(minutes=15))

    # States: one private talk, two in Trash, one still being transcribed, one that failed
    # F25 (live pass): the private talk had AI notes and an AI task in Review; private talks never go to the AI, so it is
    # now one of the transcript-only talks
    conn.execute("UPDATE conversation SET private = 1, title = 'DEMO: Private talk (stays on this computer)' WHERE id = ?", (made[11],))
    for cid in made[-2:]:
        conn.execute("UPDATE conversation SET deleted_at = ? WHERE id = ?", (ago(3), cid))
    for title, status, err in (("DEMO: Recording Nov 25 at 8_10 AM", "running", None),
                               ("DEMO: Recording Nov 21 at 6_45 PM", "failed", "The audio file was empty (made-up error)")):
        rid = conn.execute("INSERT INTO recording (source, original_path, sha256, duration_s, received_at, status, title) "
                           "VALUES ('recorder', ?, ?, 1260, ?, 'processing', ?)",
                           (f"demo/{title}.m4a", f"demo-{title}", ago(0, 2), title)).lastrowid
        conn.execute("INSERT INTO job (kind, recording_id, status, created_at, updated_at, error) VALUES ('process', ?, ?, ?, ?, ?)",
                     (rid, status, ago(0, 2), ago(0, 1), err))
    # The Inbox: recordings waiting for an OK (and two older ones), each with a short tone as its "audio"
    audio = (folder / "demo-audio") if folder else None
    if audio:
        audio.mkdir(parents=True, exist_ok=True)
    conn.execute("UPDATE recording SET status = 'awaiting_approval' WHERE title = 'DEMO: Coffee with Sam'")
    for row in conn.execute("SELECT id, title, duration_s FROM recording WHERE title = 'DEMO: Coffee with Sam'").fetchall():
        ingest.request_approval(conn, row[0], row[1], row[2], False)
    for i, (title, minutes, private) in enumerate(WAITING):
        path = audio / f"demo-{i}.wav" if audio else None
        if path:
            _tone(path, 1.5, 220 + 40 * i)
        rid = conn.execute("INSERT INTO recording (source, original_path, sha256, duration_s, received_at, status, title, "
                           "vault_path, private) VALUES ('recorder', ?, ?, ?, ?, ?, ?, ?, ?)",
                           (str(path) if path else f"demo/{i}.wav", f"demo-waiting-{i}", minutes * 60, ago(i % 4, i + 1),
                            "backlog" if private is None else "awaiting_approval", "DEMO: " + title,
                            str(path) if path else None, int(bool(private)))).lastrowid
        if private is not None:
            ingest.request_approval(conn, rid, "DEMO: " + title, minutes * 60, private)
    # The bell: a few recent notifications (titles and links only, like the real ones)
    for i, (title, link) in enumerate([("Ready: DEMO: 1:1 with Priya, Q3 budget review", "/conversations/2"),
                                       ("3 tasks due today", "/tasks"), ("Overnight: 4 recordings transcribed", "/"),
                                       ("A voice to name in DEMO: Team weekly sync", "/conversations/3"),
                                       ("AI spending reached 50% of this month's cap", "/costs"),
                                       ("Backup finished (made up)", "/status")]):
        conn.execute("INSERT INTO notification (channel, title, link, status, created_at, sent_at, dismissed_at) "
                     "VALUES (?, ?, ?, 'sent', ?, ?, ?)", ("alerts" if i == 4 else "bot", title, link, ago(0, i * 3),
                                                           ago(0, i * 3), ago(0, 1) if i == 5 else None))
    conn.execute("UPDATE person SET last_spoken_at = ? WHERE last_spoken_at IS NULL AND name LIKE '%(demo)'", (ago(9),))


def _index_everything(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM search_index WHERE kind != 'segment'")
    rows = []
    for c in conn.execute("SELECT id, title, summary FROM conversation"):
        if c[2]:
            rows += [("summary", c[0], c[0], c[2]), ("summary", c[0], c[0], c[1])]
    rows += [("task", r[0], r[1], r[2]) for r in conn.execute("SELECT id, conversation_id, title FROM task WHERE status != 'dropped'")]
    rows += [("decision", r[0], r[1], r[2]) for r in conn.execute("SELECT id, conversation_id, text FROM decision")]
    rows += [("fact", r[0], r[1], f"{r[2]}: {r[3]}") for r in conn.execute(
        "SELECT id, conversation_id, coalesce(person_name, ''), value FROM person_fact")]
    rows += [("person", r[0], None, " ".join(x for x in (r[1], r[2]) if x)) for r in conn.execute("SELECT id, name, notes FROM person")]
    rows += [("capture", r[0], None, r[1]) for r in conn.execute("SELECT id, text FROM capture WHERE text IS NOT NULL")]
    conn.executemany("INSERT INTO search_index (kind, ref_id, conversation_id, text) VALUES (?, ?, ?, ?)",
                     [r for r in rows if r[3]])


MACHINE_COLS = ("ts", "cpu_pct", "load1", "mem_used_pct", "mem_free_pct", "swap_mb", "temp_c", "fan_rpm", "speed_limit",
                "wifi_dbm", "on_battery", "disk_used_pct", "smart_ok")


def machine_reading(at: datetime) -> dict:
    """A made-up, healthy M14 reading for the moment `at` (smooth daily waves, same answer for the same minute)."""
    h = at.timestamp() / 3600
    day = math.sin(2 * math.pi * (h % 24) / 24 - 1.2)          # busier in the afternoon
    jitter = math.sin(h * 7.3) * 0.5 + math.sin(h * 2.1) * 0.5
    return {"ts": at.isoformat(timespec="seconds"), "cpu_pct": round(14 + 9 * day + 5 * jitter, 1),
            "load1": round(0.7 + 0.3 * day + 0.1 * jitter, 2), "mem_used_pct": round(62 + 5 * day + 2 * jitter, 1),
            "mem_free_pct": round(58 - 4 * day - 2 * jitter, 1), "swap_mb": round(180 + 60 * day),
            "temp_c": round(56 + 7 * day + 3 * jitter, 1), "fan_rpm": int(2300 + 500 * day + 150 * jitter),
            "speed_limit": 100, "wifi_dbm": -55, "on_battery": 0, "disk_used_pct": 38.4, "smart_ok": 1}


def _machine_rows(conn: sqlite3.Connection, start: datetime, end: datetime, step: timedelta) -> None:
    at, rows = start, []
    while at <= end:
        r = machine_reading(at)
        rows.append([r[c] for c in MACHINE_COLS])
        at += step
    conn.executemany(f"INSERT INTO machine_sample ({', '.join(MACHINE_COLS)}) VALUES ({', '.join('?' * len(MACHINE_COLS))})", rows)


def keep_alive(conn: sqlite3.Connection) -> None:
    """Public demo only: stands in for the worker that isn't there. A fresh heartbeat, and a made-up reading every
    5 minutes, so the Status page reads Healthy however long the demo has been running."""
    now = datetime.now(timezone.utc)
    conn.execute("INSERT OR REPLACE INTO setting (key, value) VALUES ('worker_heartbeat', ?)", (now.isoformat(timespec="seconds"),))
    last = conn.execute("SELECT max(ts) FROM machine_sample").fetchone()[0]
    since = datetime.fromisoformat(last) if last else now - timedelta(days=1)
    if now - since >= timedelta(minutes=5):
        _machine_rows(conn, max(since + timedelta(minutes=5), now - timedelta(days=1)), now, timedelta(minutes=5))
    conn.commit()


def live_reading() -> dict:
    """Public demo only: what the M14 card's live check would show (made up, healthy)."""
    r = machine_reading(datetime.now(timezone.utc))
    return {**{k: r[k] for k in MACHINE_COLS if k != "ts"}, "cpus": 8, "mem_used_gb": round(16 * r["mem_used_pct"] / 100, 1),
            "mem_total_gb": 16.0, "pressure": "green", "fans": [r["fan_rpm"]], "temp_tool": True,
            "battery": {"cycles": 412, "health_pct": 86, "failed": False}, "smart": "Verified", "uptime_s": 6 * 86400 + 5 * 3600,
            "procs": {"worker": {"running": True, "cpu_pct": 1.2, "mem_mb": 210}, "web": {"running": True, "cpu_pct": 0.8, "mem_mb": 140}}}


def seed(conn: sqlite3.Connection, folder: Path | None = None) -> int:
    """Load every demo meeting into an empty or demo-only database. Safe to run twice. Returns the conversation count."""
    real = conn.execute("SELECT count(*) FROM conversation WHERE title NOT LIKE 'DEMO:%'").fetchone()[0]
    if real:
        raise NotADemoDatabase("this database holds non-demo conversations; demo data goes in its own folder")
    demo.load(conn)   # the built-in one-conversation demo (Help page: Load / Remove)
    if not conn.execute("SELECT 1 FROM conversation WHERE title = ?", (MARKER,)).fetchone():
        with db.transaction(conn):
            _seed_meetings(conn, folder)
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
        n = seed(conn, folder)
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
