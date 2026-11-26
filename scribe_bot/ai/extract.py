"""Meeting extraction (M3), the contract between M2 and M3.

extract_conversation(conn, conversation_id) reads the conversation's segments (with speaker names), sends them to the
AI per the router/stage rules (never when conversation.private = 1), validates the JSON, writes summary/takeaways,
task, decision, person_fact rows (and search_index rows), records the Data Ledger entry, and returns
{"status": "done" | "skipped_private" | "blocked_cap" | "blocked" | "pending_approval" | "inbox" | "failed",
 "cost_usd": float}.
It is safe to re-run: the conversation's AI rows are replaced, except items Brian already acted on (done/dropped or
rated tasks, rated decisions, reviewed facts).
"""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

from scribe_bot.core.names import first_name
from scribe_bot.ai import costs, prompts, router
from scribe_bot.core import people
from scribe_bot.core.db import now, transaction

ME = 1                      # person id of Brian
INBOX_BELOW = 0.6           # OUTPUT_DESIGN.md: confidence below this goes to the Inbox
AREA_SURE = 0.8             # the AI files Work/Personal itself at or above this; below, its guess waits under Not sorted
TASK = "extract"


def _lines(conn: sqlite3.Connection, conversation_id: int) -> list[tuple[int, str, str]]:
    rows = conn.execute(
        "SELECT s.idx, s.text, p.name, sp.cluster, "
        "  (SELECT count(*) FROM speaker x WHERE x.conversation_id = s.conversation_id AND x.id <= sp.id) AS n "
        "FROM segment s LEFT JOIN speaker sp ON sp.id = s.speaker_id LEFT JOIN person p ON p.id = sp.person_id "
        "WHERE s.conversation_id = ? ORDER BY s.idx", (conversation_id,)).fetchall()
    fixes = glossary_fixes(conn)
    return [(r["idx"], r["name"] or (f"Speaker {r['n']}" if r["cluster"] is not None else "Unknown"),
             prompts.apply_glossary(r["text"], fixes)) for r in rows]


def glossary_fixes(conn: sqlite3.Connection) -> dict[str, str]:
    fixes = {}
    for r in conn.execute("SELECT term, variants FROM glossary_term WHERE approved = 1"):
        for v in json.loads(r["variants"]):
            fixes[v] = r["term"]
    return fixes


def _prompt(conn: sqlite3.Connection, conversation_id: int) -> tuple[str, str, int]:
    lines = _lines(conn, conversation_id)
    me = conn.execute("SELECT name FROM person WHERE id = ?", (ME,)).fetchone()[0]
    kind, chosen = conn.execute("SELECT type, type_chosen FROM conversation WHERE id = ?", (conversation_id,)).fetchone()
    custom = conn.execute("SELECT value FROM setting WHERE key = ?", (f"template:{kind}",)).fetchone()
    from scribe_bot.core import tags as tags_svc
    long_s = conn.execute("SELECT coalesce((SELECT r.duration_s FROM recording r JOIN conversation c ON c.recording_id = r.id "
                          "WHERE c.id = ?), (SELECT max(end_s) FROM segment WHERE conversation_id = ?), 0)",
                          (conversation_id, conversation_id)).fetchone()[0] or 0
    return (prompts.extract_system(me, kind, custom[0] if custom else None, detect_learning=not chosen,
                                   tags=tags_svc.names(conn), detailed=long_s >= prompts.DETAIL_MIN_S),
            prompts.render(lines), len(lines))


def estimate_cost(conn: sqlite3.Connection, conversation_id: int, ending_private: bool = False) -> dict:
    """For the web app's re-run preview: model, account, estimated cost, and whether it would be blocked.
    ending_private: the price of "Run AI now" on a Private conversation (Brian ends Private on the confirm step)."""
    conv = conn.execute("SELECT private FROM conversation WHERE id = ?", (conversation_id,)).fetchone()
    system, user, n = _prompt(conn, conversation_id)
    cfg = router.task_config(conn, TASK)
    provider, model = cfg["chain"][0]
    est = costs.estimate(model, system, user)
    accounts = [a for a in router.accounts_for(conn, provider) if not a["may_train"]]
    blocked = None
    if conv["private"] and not ending_private:
        blocked = "private: stays on the M14"
    elif router.offline(conn):
        blocked = "offline switch is on"
    elif not accounts:
        blocked = "no usable account"
    else:
        try:
            costs.check(conn, accounts[0], est["cost_usd"])
        except costs.CapExceeded as e:
            blocked = str(e)
    return {"model": model, "account": accounts[0]["nickname"] if accounts else None, "lines": n,
            "tokens_in_est": est["tokens_in"], "cost_usd_est": est["cost_usd"], "blocked": blocked}


SPEAKER_N = re.compile(r"speaker (\d+)", re.IGNORECASE)


def placeholder_name(n: int, title: str | None, started_at: str | None) -> str:
    """"Speaker 2 from Budget sync on Oct 20": tells unnamed speakers apart in People and on tasks."""
    title = (title or "a conversation").strip()
    title = title if len(title) <= 40 else title[:39].rstrip() + "…"
    when = ""
    if started_at:
        when = " on " + datetime.fromisoformat(started_at).astimezone().strftime("%b %-d")
    return f"Speaker {n} from {title}{when}"


def _speaker_person(conn: sqlite3.Connection, conv: sqlite3.Row, n: int, title: str | None, area: str | None,
                    create: bool) -> int | None:
    """"Speaker N" in this conversation: their person if named, else a placeholder person standing for that speaker
    (never a shared "Speaker 2": that would be a different person in every meeting)."""
    sid = {num: s for s, num in speaker_numbers(conn, conv["id"]).items()}.get(n)
    if sid is None:
        return None
    sp = conn.execute("SELECT person_id FROM speaker WHERE id = ?", (sid,)).fetchone()
    if sp["person_id"]:
        return sp["person_id"]
    row = conn.execute("SELECT id FROM person WHERE stands_for_speaker = ?", (sid,)).fetchone()
    if row or not create:
        return row["id"] if row else None
    return conn.execute(
        "INSERT INTO person (name, area, stands_for_speaker, created_at) VALUES (?, ?, ?, ?)",
        (placeholder_name(n, title or conv["title"], conv["started_at"]),
         {"work": "work", "personal": "personal"}.get(area or "", "both"), sid, now())).lastrowid


def _person_id(conn: sqlite3.Connection, name: str, area: str | None, create: bool = True,
               conv: sqlite3.Row | None = None, title: str | None = None) -> int | None:
    """Match by name or alias (case-insensitive); a one-word name also matches a unique first name.
    "Speaker N" with a conversation: that conversation's speaker N (see _speaker_person)."""
    key = name.strip().lower()
    if not key:
        return None
    if conv is not None and (m := SPEAKER_N.fullmatch(key)):
        return _speaker_person(conn, conv, int(m.group(1)), title, area, create)
    rows = conn.execute("SELECT id, name, aliases FROM person").fetchall()
    for p in rows:
        if key in [p["name"].lower()] + [a.lower() for a in json.loads(p["aliases"])]:
            return p["id"]
    if " " not in key:
        firsts = [p["id"] for p in rows if first_name(p["name"]).lower() == key]
        if len(firsts) == 1:
            return firsts[0]
    if not create:
        return None
    cur = conn.execute("INSERT INTO person (name, area, created_at) VALUES (?, ?, ?)",
                       (name.strip(), {"work": "work", "personal": "personal"}.get(area or "", "both"), now()))
    return cur.lastrowid


def speaker_numbers(conn: sqlite3.Connection, conversation_id: int) -> dict[int, int]:
    """speaker id -> N in "Speaker N": the numbering the AI sees in _lines (by speaker id). The page uses it too."""
    ids = [r[0] for r in conn.execute("SELECT id FROM speaker WHERE conversation_id = ? ORDER BY id", (conversation_id,))]
    return {sid: i + 1 for i, sid in enumerate(ids)}


def relabel_speakers(conn: sqlite3.Connection, cid: int) -> None:
    """Patch, don't rewrite: once a speaker is named, "Speaker N" in this conversation's AI text becomes their name
    (title, summary, key points, tasks, decisions, people notes), and notes/tasks the AI gave to "Speaker N" move to
    that person. No AI call."""
    for ph, into in conn.execute("SELECT p.id, s.person_id FROM person p JOIN speaker s ON s.id = p.stands_for_speaker "
                                 "WHERE s.conversation_id = ? AND s.person_id IS NOT NULL AND s.person_id != p.id",
                                 (cid,)).fetchall():
        people.merge(conn, ph, into)   # the placeholder's tasks and notes go to the person the speaker was named as
    nums = speaker_numbers(conn, cid)
    named = conn.execute("SELECT s.id, s.person_id, p.name FROM speaker s JOIN person p ON p.id = s.person_id "
                         "WHERE s.conversation_id = ?", (cid,)).fetchall()
    subs = [(re.compile(rf"\bSpeaker {nums[r['id']]}\b(?!\d)", re.IGNORECASE), r["name"], r["person_id"], nums[r["id"]])
            for r in named]
    if not subs:
        return

    with transaction(conn):
        _patch_text(conn, cid, [(pat, name) for pat, name, _, _ in subs])
        for _, name, pid, n in subs:
            conn.execute("UPDATE person_fact SET person_id = ?, person_name = ? WHERE conversation_id = ? "
                         "AND lower(person_name) = ?", (pid, name, cid, f"speaker {n}"))
            conn.execute("UPDATE task SET owner_person_id = ? WHERE conversation_id = ? AND owner_person_id IN "
                         "(SELECT id FROM person WHERE lower(name) = ?)", (pid, cid, f"speaker {n}"))
        reindex_ai_text(conn, cid)


_PAREN_SAME = re.compile(r"\b([A-Z][\w'-]*(?: [A-Z][\w'-]*)*) \((?:\1|Speaker \d+)\)")


def _patch_text(conn: sqlite3.Connection, cid: int, subs: list[tuple[re.Pattern, str]]) -> None:
    """Apply name replacements to this conversation's AI text (title, summary, key points, tasks, decisions, notes)."""
    def fix(text: str | None) -> str | None:
        for pat, name in subs:
            text = pat.sub(name, text) if text else text
        # "Casey (Casey)" / "Casey (Speaker 2)" after a swap -> "Casey" (Brian, 2026-11-23: "Casey (Morgan Example)")
        return _PAREN_SAME.sub(r"\1", text) if text else text

    # Detailed notes too (Brian, 2026-11-23: Detailed still said "Speaker 1" after he named Morgan)
    c = conn.execute("SELECT title, summary, takeaways, detailed FROM conversation WHERE id = ?", (cid,)).fetchone()
    conn.execute("UPDATE conversation SET title = ?, summary = ?, takeaways = ?, detailed = ? WHERE id = ?",
                 (fix(c["title"]), fix(c["summary"]), json.dumps([fix(t) for t in json.loads(c["takeaways"] or "[]")]),
                  fix(c["detailed"]), cid))
    for table, col in (("task", "title"), ("decision", "text"), ("person_fact", "value")):
        for r in conn.execute(f"SELECT id, {col} FROM {table} WHERE conversation_id = ?", (cid,)).fetchall():
            if fix(r[col]) != r[col]:
                conn.execute(f"UPDATE {table} SET {col} = ? WHERE id = ?", (fix(r[col]), r["id"]))
    # Short titles for lists too (Brian, 2026-11-22: "You owe" still said "Speaker 1" after he named her Morgan).
    # They stay short: the first name only ("Dr. Riley Example" -> "Riley").
    def first(name: str) -> str:
        parts = [w for w in name.split() if not re.fullmatch(r"(?i)(dr|mr|mrs|ms|prof)\.?", w)]
        return parts[0] if parts else name
    def fix_short(text: str | None) -> str | None:
        for pat, name in subs:
            text = pat.sub(first(name), text) if text else text
        return text
    for table, where in (("task", "conversation_id = ?"), ("conversation", "id = ?")):
        for r in conn.execute(f"SELECT id, short_title FROM {table} WHERE {where}", (cid,)).fetchall():
            if r["short_title"] and fix_short(r["short_title"]) != r["short_title"]:
                conn.execute(f"UPDATE {table} SET short_title = ? WHERE id = ?", (fix_short(r["short_title"]), r["id"]))


def unname_in_text(conn: sqlite3.Connection, sid: int, pid: int) -> None:
    """"This isn't X" (Brian, 2026-11-23: the summary and tasks still said Morgan after he un-named her). In this
    talk, X's name in the AI text goes back to "Speaker N", and X's tasks and notes from this talk go to that
    speaker's placeholder. Naming the right person later patches "Speaker N" to them (relabel_speakers), so naming
    before or after the AI notes ends the same, with no AI call or cost. Call after speaker.person_id is cleared."""
    sp = conn.execute("SELECT conversation_id FROM speaker WHERE id = ?", (sid,)).fetchone()
    person = conn.execute("SELECT name FROM person WHERE id = ?", (pid,)).fetchone()
    if sp is None or person is None:
        return
    cid = sp["conversation_id"]
    if conn.execute("SELECT 1 FROM speaker WHERE conversation_id = ? AND person_id = ?", (cid, pid)).fetchone():
        return   # X really is another voice in this talk: their name stays
    conv = conn.execute("SELECT * FROM conversation WHERE id = ?", (cid,)).fetchone()
    n = speaker_numbers(conn, cid).get(sid)
    if n is None:
        return
    ph = _speaker_person(conn, conv, n, None, conv["area"], create=True)
    name = person["name"].strip()
    word = lambda w: re.compile(rf"(?<![\w-]){re.escape(w)}(?![\w-])")  # noqa: E731
    subs = [(word(name), f"Speaker {n}")]
    first = first_name(name)
    others = conn.execute("SELECT p.name FROM speaker s JOIN person p ON p.id = s.person_id WHERE s.conversation_id = ?",
                          (cid,)).fetchall()
    if first != name and not any(first_name(o[0]) == first for o in others):
        subs.append((word(first), f"Speaker {n}"))
    _patch_text(conn, cid, subs)   # the caller holds the transaction
    ph_name = conn.execute("SELECT name FROM person WHERE id = ?", (ph,)).fetchone()[0]
    conn.execute("UPDATE task SET owner_person_id = ? WHERE conversation_id = ? AND owner_person_id = ?", (ph, cid, pid))
    conn.execute("UPDATE person_fact SET person_id = ?, person_name = ? WHERE conversation_id = ? AND person_id = ?",
                 (ph, ph_name, cid, pid))
    reindex_ai_text(conn, cid)


def rename_in_text(conn: sqlite3.Connection, pid: int, old: str, new: str) -> int:
    """Brian renamed a person: their old name becomes the new one in the AI text of every conversation they're linked
    to (spoke in, owns a task, has a note, is in a decision). No AI call. The old full name is replaced; the old first
    name too, unless someone else in that conversation shares it. Adding a surname ("Alex" → "Alex Rivera") changes
    nothing: "Alex" is still right. Returns how many conversations were patched."""
    old, new = old.strip(), new.strip()
    old_first, new_first = first_name(old), first_name(new)
    if old == new or old_first.lower() == "speaker":   # placeholders are patched by relabel_speakers
        return 0
    cids = [r[0] for r in conn.execute(
        "SELECT conversation_id FROM speaker WHERE person_id = ? UNION SELECT conversation_id FROM task WHERE owner_person_id = ? "
        "UNION SELECT conversation_id FROM person_fact WHERE person_id = ? UNION SELECT d.conversation_id FROM decision d, "
        "json_each(d.people) j WHERE j.value = ?", (pid, pid, pid, pid)) if r[0] is not None]
    word = lambda w: re.compile(rf"(?<![\w-]){re.escape(w)}(?![\w-])")  # noqa: E731
    for cid in cids:
        subs = []
        if " " in old and not new.startswith(old + " "):
            subs.append((word(old), new))
        if old_first != new_first:
            others = conn.execute("SELECT p.name FROM speaker s JOIN person p ON p.id = s.person_id "
                                  "WHERE s.conversation_id = ? AND p.id != ?", (cid, pid)).fetchall()
            if not any(first_name(o[0]) == old_first for o in others):
                subs.append((word(old_first), new_first))
        if subs:
            _patch_text(conn, cid, subs)
            reindex_ai_text(conn, cid)
    conn.execute("UPDATE person_fact SET person_name = ? WHERE person_id = ?", (new, pid))
    return len(cids)


def reindex_ai_text(conn: sqlite3.Connection, cid: int) -> None:
    """Rebuild this conversation's search rows for the AI text (same rows _write makes)."""
    conn.execute("DELETE FROM search_index WHERE conversation_id = ? AND kind IN ('summary', 'task', 'decision', 'fact')", (cid,))
    c = conn.execute("SELECT title, summary FROM conversation WHERE id = ?", (cid,)).fetchone()
    rows = [("summary", cid, c["summary"]), ("summary", cid, c["title"])] if c["summary"] else []
    rows += [("task", r[0], r[1]) for r in conn.execute(
        "SELECT id, title FROM task WHERE conversation_id = ? AND status != 'dropped'", (cid,))]
    rows += [("decision", r[0], r[1]) for r in conn.execute(
        "SELECT id, text FROM decision WHERE conversation_id = ? AND coalesce(rated, '') != 'wrong'", (cid,))]
    rows += [("fact", r[0], f"{r[1]}: {r[2]}") for r in conn.execute(
        "SELECT id, coalesce(person_name, ''), value FROM person_fact WHERE conversation_id = ? AND coalesce(confirmed, 1) != 0",
        (cid,))]
    conn.executemany("INSERT INTO search_index (kind, ref_id, conversation_id, text) VALUES (?, ?, ?, ?)",
                     [(k, rid, cid, text) for k, rid, text in rows if text])


def _clear_ai_rows(conn: sqlite3.Connection, cid: int) -> None:
    conn.execute("DELETE FROM task WHERE conversation_id = ? AND status = 'open' AND rated IS NULL", (cid,))
    conn.execute("DELETE FROM decision WHERE conversation_id = ? AND rated IS NULL", (cid,))
    conn.execute("DELETE FROM person_fact WHERE conversation_id = ? AND confirmed IS NULL", (cid,))
    conn.execute("DELETE FROM review_item WHERE conversation_id = ? AND kind = 'inbox' AND status = 'open'", (cid,))
    conn.execute("DELETE FROM search_index WHERE conversation_id = ? AND kind IN ('summary', 'task', 'decision', 'fact')",
                 (cid,))


def _write(conn: sqlite3.Connection, conv: sqlite3.Row, data: dict, processed_by: str) -> None:
    cid, area, t = conv["id"], conv["area"], now()
    guess = data.get("area")
    if area is None and guess and (data.get("area_confidence") or 0) >= AREA_SURE:   # auto-file; Brian's choice always wins
        area = guess
    seg = {r["idx"]: r["id"] for r in conn.execute("SELECT idx, id FROM segment WHERE conversation_id = ?", (cid,))}
    index = [("summary", cid, data["summary"])]
    if data.get("title"):  # indexed as a 'summary' row so search finds it and "mark private" removes it with the rest
        index.append(("summary", cid, data["title"]))
    with transaction(conn):
        _clear_ai_rows(conn, cid)
        # The AI topic title replaces the fallback; recording.title keeps the original Recorder name.
        if data.get("learning") is True:  # the AI spotted a talk/class (only asked when Brian didn't pick the type)
            conn.execute("UPDATE conversation SET type = 'learning' WHERE id = ? AND type_chosen = 0", (cid,))
        conn.execute("UPDATE conversation SET title = coalesce(?, title), short_title = ?, summary = ?, takeaways = '[]', processed_by = ?, "
                     "status = 'extracted', summary_type = type, updated_at = ? WHERE id = ?",
                     (data.get("title"), data.get("short_title"), data["summary"], processed_by, t, cid))
        if data.get("detailed"):   # 30 min or longer: detailed notes came in the same reply
            conn.execute("UPDATE conversation SET detailed = ?, detailed_at = ? WHERE id = ?", (data["detailed"], t, cid))
        if conv["area"] is None:
            conn.execute("UPDATE conversation SET area = ?, area_confidence = ?, area_guess = ? WHERE id = ?",
                         (area, data.get("area_confidence"), guess, cid))
        from scribe_bot.core import tags as tags_svc   # M4: the AI's picks from existing tags (never new ones)
        conn.execute("DELETE FROM conversation_tag WHERE conversation_id = ? AND by_ai = 1", (cid,))
        for name in data.get("tags") or []:
            tags_svc.add(conn, cid, name, by_ai=True)
        for item in data["tasks"] + data["waiting_on"]:
            owner = _person_id(conn, item["owner"], area, conv=conv, title=data.get("title")) or ME
            confidence = item.get("confidence")
            inbox = int(confidence is not None and confidence < INBOX_BELOW)
            tid = conn.execute(
                "INSERT INTO task (conversation_id, title, short_title, owner_person_id, due, area, confidence, inbox, "
                "source_segment_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cid, item["title"].strip(), prompts._short(item.get("short"), prompts.TASK_SHORT_MAX), owner, prompts.clean_due(item.get("due")),
                 area, confidence, inbox, seg.get(item.get("line")), t, t)).lastrowid
            if inbox:
                conn.execute("INSERT INTO review_item (kind, conversation_id, ref_id, payload, created_at) "
                             "VALUES ('inbox', ?, ?, ?, ?)", (cid, tid, json.dumps({"reason": "low confidence"}), t))
            index.append(("task", tid, item["title"]))
        for d in data["decisions"]:
            did = conn.execute("INSERT INTO decision (conversation_id, text, source_segment_id, created_at) "
                               "VALUES (?, ?, ?, ?)", (cid, d["text"].strip(), seg.get(d.get("line")), t)).lastrowid
            index.append(("decision", did, d["text"]))
        for f in data["people_facts"]:
            pid = _person_id(conn, f["person"], area, create=False, conv=conv)
            fid = conn.execute(
                "INSERT INTO person_fact (person_id, person_name, conversation_id, value, source_segment_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)", (pid, f["person"].strip(), cid, f["fact"].strip(), seg.get(f.get("line")),
                                               t)).lastrowid
            index.append(("fact", fid, f"{f['person']}: {f['fact']}"))
        conn.executemany("INSERT INTO search_index (kind, ref_id, conversation_id, text) VALUES (?, ?, ?, ?)",
                         [(k, rid, cid, text) for k, rid, text in index])


def extract_conversation(conn: sqlite3.Connection, conversation_id: int, *, approved: bool = False,
                         factory: router.ProviderFactory = router.default_factory, chain: list | None = None,
                         keep: bool = True) -> dict:
    conv = conn.execute("SELECT * FROM conversation WHERE id = ?", (conversation_id,)).fetchone()
    if conv is None or conv["deleted_at"]:
        return {"status": "failed", "cost_usd": 0.0, "error": "no such conversation"}
    if conv["private"]:
        conn.execute("UPDATE conversation SET processed_by = 'local', updated_at = ? WHERE id = ?", (now(), conversation_id))
        return {"status": "skipped_private", "cost_usd": 0.0}

    system, user, n = _prompt(conn, conversation_id)
    summary = f"talk #{conversation_id} transcript ({n} lines, {len(user)} chars)"
    spent = 0.0
    last_error = "invalid AI output"
    for attempt in range(2):  # one retry on invalid output
        try:
            call = router.run(conn, TASK, system, user, sent_summary=summary, conversation_id=conversation_id,
                              area=conv["area"], approved=approved, factory=factory, chain=chain)
        except router.NeedsApproval as e:
            return {"status": "pending_approval", "cost_usd": spent, "estimate": e.estimate}
        except router.Blocked as e:
            return {"status": "blocked_cap" if e.kind == "cap" else "blocked", "cost_usd": spent, "error": str(e)}
        except router.CallFailed as e:
            return {"status": "failed", "cost_usd": spent, "error": str(e)}
        spent += call.cost_usd
        if call.stop_reason == "refusal":
            last_error = "the model declined"
            break
        try:
            data = prompts.parse_extract(call.text)
        except prompts.InvalidOutput as e:
            last_error = str(e)
            continue
        if not keep:  # Compare: hold the other model's version beside the current one; nothing is replaced yet
            with transaction(conn):
                conn.execute("INSERT INTO setting (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                             (COMPARE_KEY.format(conversation_id), json.dumps(
                                 {"by": f"{call.account_nickname}:{call.model}", "data": data, "cost_usd": round(spent, 6), "at": now()})))
            return {"status": "compared", "cost_usd": round(spent, 6)}
        _write(conn, conv, data, f"{call.account_nickname}:{call.model}")
        return {"status": "done", "cost_usd": round(spent, 6)}

    with transaction(conn):
        conn.execute("INSERT INTO review_item (kind, conversation_id, payload, created_at) VALUES ('inbox', ?, ?, ?)",
                     (conversation_id, json.dumps({"reason": f"AI extraction failed: {last_error}"}), now()))
    return {"status": "inbox", "cost_usd": round(spent, 6), "error": last_error}


# ---------- Compare with the other model (Brian, 2026-11-19: "until I land on the model for me") ----------
# Runs only the AI step again (the transcript is reused; no audio work) on the other Claude model, and keeps that
# version in setting 'compare:<id>' until Brian keeps one of the two. Keeping the other one replaces the AI rows the
# same way Re-run AI does (items Brian already acted on stay).
COMPARE_KEY = "compare:{}"
COMPARE_MODELS = (router.SONNET, router.HAIKU)


def other_model(conn: sqlite3.Connection, conversation_id: int) -> list:
    """[provider, model] to compare against: Haiku if Sonnet made the current notes, else Sonnet."""
    by = (conn.execute("SELECT processed_by FROM conversation WHERE id = ?", (conversation_id,)).fetchone()[0] or "")
    return router.HAIKU if "sonnet" in by else router.SONNET


def compare_estimate(conn: sqlite3.Connection, conversation_id: int) -> dict:
    system, user, _ = _prompt(conn, conversation_id)
    model = other_model(conn, conversation_id)[1]
    return {"model": model, **costs.estimate(model, system, user)}


def compared(conn: sqlite3.Connection, conversation_id: int) -> dict | None:
    row = conn.execute("SELECT value FROM setting WHERE key = ?", (COMPARE_KEY.format(conversation_id),)).fetchone()
    return json.loads(row[0]) if row else None


def keep_compared(conn: sqlite3.Connection, conversation_id: int, which: str) -> None:
    """which='other': the compared version replaces the current notes; 'current': the compared one is thrown away."""
    other = compared(conn, conversation_id)
    if other and which == "other":
        conv = conn.execute("SELECT * FROM conversation WHERE id = ?", (conversation_id,)).fetchone()
        _write(conn, conv, other["data"], other["by"])
    with transaction(conn):
        conn.execute("DELETE FROM setting WHERE key = ?", (COMPARE_KEY.format(conversation_id),))


# ---------- Short / Detailed notes (Brian, 2026-11-20) ----------
DETAIL_OUTPUT_TOKENS = 3000


def _detail_prompt(conn: sqlite3.Connection, conversation_id: int) -> tuple[str, str, int]:
    lines = _lines(conn, conversation_id)
    me = conn.execute("SELECT name FROM person WHERE id = ?", (ME,)).fetchone()[0]
    return prompts.detail_system(me), prompts.render(lines), len(lines)


def detail_estimate(conn: sqlite3.Connection, conversation_id: int) -> dict:
    system, user, _ = _detail_prompt(conn, conversation_id)
    provider, model = router.task_config(conn, TASK)["chain"][0]
    return {"model": model, **costs.estimate(model, system, user, output_tokens=DETAIL_OUTPUT_TOKENS)}


def detail_conversation(conn: sqlite3.Connection, conversation_id: int, *, approved: bool = True,
                        factory: router.ProviderFactory = router.default_factory, chain: list | None = None) -> dict:
    conv = conn.execute("SELECT * FROM conversation WHERE id = ?", (conversation_id,)).fetchone()
    if conv is None or conv["deleted_at"]:
        return {"status": "failed", "error": "no such conversation"}
    if conv["private"]:
        return {"status": "skipped_private"}
    system, user, n = _detail_prompt(conn, conversation_id)
    try:
        call = router.run(conn, TASK, system, user, sent_summary=f"talk #{conversation_id} transcript ({n} lines) for detailed notes",
                          conversation_id=conversation_id, area=conv["area"], approved=approved, factory=factory, chain=chain,
                          max_tokens=6000)
    except (router.NeedsApproval, router.Blocked, router.CallFailed) as e:
        return {"status": "failed", "error": str(e)}
    text = prompts.clean_detail(call.text)
    if not text:
        return {"status": "failed", "error": "empty notes", "cost_usd": call.cost_usd}
    with transaction(conn):
        conn.execute("UPDATE conversation SET detailed = ?, detailed_at = ? WHERE id = ?", (text, now(), conversation_id))
        conn.execute("INSERT INTO search_index (kind, ref_id, conversation_id, text) VALUES ('summary', ?, ?, ?)",
                     (conversation_id, conversation_id, text))
    return {"status": "done", "cost_usd": round(call.cost_usd, 6)}
