"""The 'recording' pipeline: audio -> transcribe -> diarize -> identify -> file -> ai_extract -> notify.

Ingest (scribe_bot/pipeline/ingest.py) creates the recording row and queues the job. Every stage saves its output in
the job folder before it is marked done, and is safe to re-run after a crash. The temporary 16 kHz WAV is deleted
after identify (and re-made from the vault copy if a later re-run needs it). "file" runs before "ai_extract"
because extraction reads the saved transcript lines.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scribe_bot.backup import export as readable
from scribe_bot.core import progress, queue, scheduler, settings
from scribe_bot.core.db import now, transaction
from scribe_bot.core.queue import StageContext
from scribe_bot.discord_bot import notify
from scribe_bot.pipeline import identify, ingest, paths, recorder_names, speech
from scribe_bot.pipeline.lines import kept_clusters, speaker_lines, talk_seconds

CLIP_SECONDS = 5
KEEP_ORIGINAL_DAYS = 30


# --- helpers ---

def _recording(ctx: StageContext) -> sqlite3.Row:
    return ctx.conn.execute("SELECT * FROM recording WHERE id = ?", (ctx.recording_id,)).fetchone()


def _conversation_id(ctx: StageContext) -> int:
    return ctx.conn.execute("SELECT id FROM conversation WHERE recording_id = ?", (ctx.recording_id,)).fetchone()[0]


def _wav(ctx: StageContext) -> Path:
    """The temporary WAV, re-made from the vault copy if it was already cleaned up."""
    wav = ctx.work_dir / "audio16k.wav"
    if not wav.exists():
        speech.engine().to_wav(Path(_recording(ctx)["vault_path"]), wav)
    return wav


def _json(path: str | Path):
    return json.loads(Path(path).read_text())


def _save(ctx: StageContext, name: str, data) -> str:
    out = ctx.work_dir / name
    paths.atomic_write(out, json.dumps(data, indent=1))
    return str(out)


def _set_status(conn: sqlite3.Connection, conversation_id: int, status: str) -> None:
    conn.execute("UPDATE conversation SET status = ?, updated_at = ? WHERE id = ?", (status, now(), conversation_id))


# --- stages ---

def _no_ai(ctx: StageContext) -> bool:
    """"Transcript Only (No AI)" or an explicit private flag on the job: the conversation is Private for good."""
    return ctx.payload.get("ai") is False or bool(ctx.payload.get("private"))


def _make_private(ctx: StageContext) -> None:
    ctx.conn.execute("UPDATE recording SET private = 1 WHERE id = ?", (ctx.recording_id,))
    ctx.conn.execute("UPDATE conversation SET private = 1, updated_at = ? WHERE recording_id = ?", (now(), ctx.recording_id))


def audio(ctx: StageContext) -> str:
    rec = _recording(ctx)
    t = now()
    # Title until the AI writes a topic title: the Recorder name without its date ("Conversation" if it was only a date).
    ctx.conn.execute(
        "INSERT OR IGNORE INTO conversation (recording_id, title, started_at, duration_s, private, status, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, 'transcribing', ?, ?)",
        (rec["id"], recorder_names.fallback_title(rec["title"] or ""), ctx.payload.get("started_at"), rec["duration_s"],
         rec["private"], t, t),
    )
    if _no_ai(ctx):
        _make_private(ctx)
    ctx.conn.execute("UPDATE recording SET status = 'processing' WHERE id = ?", (rec["id"],))
    started_ping(ctx, rec["duration_s"])
    return str(_wav(ctx))


def started_ping(ctx: StageContext, duration_s: float | None) -> None:
    """One 'Processing started · N min recording · ready ~4:09 pm' per job (never the title). Safe on re-runs."""
    link = f"/?job={ctx.job_id}"
    if ctx.conn.execute("SELECT 1 FROM notification WHERE link = ?", (link,)).fetchone():
        return
    eta = datetime.now(timezone.utc) + timedelta(seconds=progress.estimate(ctx.conn, duration_s)["total_s"])
    notify.enqueue(ctx.conn, "bot", f"Processing started · {round((duration_s or 0) / 60, 1)} min recording · "
                                    f"ready ~{progress.local_time(eta)}", link, discord=False)   # app only (Brian 2026-10-22)


def transcribe(ctx: StageContext) -> str:
    eng = speech.engine()
    return _save(ctx, "transcript.json", eng.transcribe(eng.load(_wav(ctx))))


def diarize(ctx: StageContext) -> str:
    """Automatic speaker count, unless the job forces one (the "This was a 1:1" button re-runs with speakers=2)."""
    eng = speech.engine()
    return _save(ctx, "speakers_auto.json", eng.diarize(eng.load(_wav(ctx)), ctx.payload.get("speakers")))


def _identify_turns(samples, turns: list[dict], prints) -> tuple[dict, dict, dict]:
    keep = kept_clusters(turns)
    talk = {c: s for c, s in talk_seconds(turns).items() if c in keep}
    embs = {c: v for c, v in speech.engine().embeddings(samples, turns).items() if c in keep}
    return talk, embs, identify.match(embs, prints)


def identify_stage(ctx: StageContext) -> str:
    cfg = settings.load()
    eng = speech.engine()
    conv_id = _conversation_id(ctx)
    samples = eng.load(_wav(ctx))
    turns = _json(ctx.outputs["diarize"])
    prints = identify.known_prints(ctx.conn)
    talk, embs, matches = _identify_turns(samples, turns, prints)
    auto_count = len(talk)
    one_on_one = not ctx.payload.get("speakers") and identify.looks_one_on_one(talk, matches)
    if one_on_one:  # DECISIONS 2026-10-19 option A: treat as exactly 2 speakers
        # Fast path: fold the extra clusters into Brian's or the other main speaker's, reusing the first pass's
        # embeddings (a second full speaker separation took ~9 min for 18 min of audio on the M14).
        me = next(c for c, (pid, _) in matches.items() if pid == identify.ME)
        turns = identify.merge_to_two(turns, talk, embs, me)
        talk = {c: s for c, s in talk_seconds(turns).items() if c in kept_clusters(turns)}
        embs = {c: v for c, v in embs.items() if c in talk}
        matches = identify.match(embs, prints)
    _save(ctx, "speakers.json", turns)

    clip_dir = paths.clips(cfg) / str(conv_id)
    unknown = []
    with transaction(ctx.conn):
        # A re-run replaces the old speakers; transcript lines point at them, so those go too (file re-makes them).
        ctx.conn.execute("DELETE FROM review_item WHERE conversation_id = ? AND kind = 'speaker'", (conv_id,))
        ctx.conn.execute("DELETE FROM search_index WHERE conversation_id = ? AND kind = 'segment'", (conv_id,))
        ctx.conn.execute("DELETE FROM segment WHERE conversation_id = ?", (conv_id,))
        ctx.conn.execute("DELETE FROM speaker WHERE conversation_id = ?", (conv_id,))
        for cluster in sorted(talk):
            pid, score = matches.get(cluster, (None, None))
            sid = ctx.conn.execute(
                "INSERT INTO speaker (conversation_id, cluster, person_id, match_score, talk_seconds) VALUES (?, ?, ?, ?, ?)",
                (conv_id, cluster, pid, score, round(talk[cluster], 1)),
            ).lastrowid
            if pid is None:
                unknown.append((sid, cluster, None))
            elif pid != identify.ME and score < identify.CONFIRM_BELOW:  # a weak match: keep the name, ask to confirm
                unknown.append((sid, cluster, pid))
        picked = ctx.payload.get("type")  # Brian's pick when he approved it
        kind = picked or {0: None, 1: "voice_note", 2: "1:1"}.get(len(talk), "multi")
        ctx.conn.execute("UPDATE conversation SET type = ?, type_chosen = ?, updated_at = ? WHERE id = ?",
                         (kind, 1 if picked else 0, now(), conv_id))

    identify.save_embeddings(cfg, conv_id, embs)
    for sid, cluster, guess in unknown:  # 5-second sample from the speaker's longest turn, for the review queue
        longest = max((t for t in turns if t["speaker"] == cluster), key=lambda t: t["end"] - t["start"])
        clip = clip_dir / f"speaker{cluster}.wav"
        eng.write_clip(samples, longest["start"], min(longest["end"], longest["start"] + CLIP_SECONDS), clip)
        ctx.conn.execute("UPDATE speaker SET clip_path = ? WHERE id = ?", (str(clip), sid))  # the Review page plays it
        ctx.conn.execute(
            "INSERT INTO review_item (kind, conversation_id, ref_id, payload, created_at) VALUES ('speaker', ?, ?, ?, ?)",
            (conv_id, sid, json.dumps({"clip": str(clip), "talk_seconds": round(talk[cluster], 1),
                                       **({"guess_id": guess, "score": matches[cluster][1]} if guess else {})}), now()),
        )
    (ctx.work_dir / "audio16k.wav").unlink(missing_ok=True)  # the temporary WAV is not kept (SPEC §6)
    return _save(ctx, "identify.json", {
        "auto_speakers": auto_count, "one_on_one_rerun": one_on_one, "speakers": len(talk),
        "matched": {str(c): m for c, m in matches.items()}, "unknown": sum(1 for u in unknown if u[2] is None),
        "to_confirm": sum(1 for u in unknown if u[2] is not None),
    })


def file_stage(ctx: StageContext) -> str:
    cfg = settings.load()
    conv_id = _conversation_id(ctx)
    conv = ctx.conn.execute("SELECT * FROM conversation WHERE id = ?", (conv_id,)).fetchone()
    lines = speaker_lines(_json(ctx.outputs["transcribe"]), _json(ctx.work_dir / "speakers.json"))
    speakers = {r["cluster"]: r for r in ctx.conn.execute(
        "SELECT s.*, p.name FROM speaker s LEFT JOIN person p ON p.id = s.person_id WHERE s.conversation_id = ?", (conv_id,))}
    with transaction(ctx.conn):
        ctx.conn.execute("DELETE FROM search_index WHERE conversation_id = ? AND kind = 'segment'", (conv_id,))
        ctx.conn.execute("DELETE FROM segment WHERE conversation_id = ?", (conv_id,))
        for i, x in enumerate(lines):
            end = lines[i + 1]["start"] if i + 1 < len(lines) else x["last"] + 0.5
            sp = speakers.get(x["cluster"])
            seg_id = ctx.conn.execute(
                "INSERT INTO segment (conversation_id, idx, start_s, end_s, speaker_id, text) VALUES (?, ?, ?, ?, ?, ?)",
                (conv_id, i, round(x["start"], 2), round(end, 2), sp["id"] if sp else None, x["text"]),
            ).lastrowid
            ctx.conn.execute("INSERT INTO search_index (kind, ref_id, conversation_id, text) VALUES ('segment', ?, ?, ?)",
                             (seg_id, conv_id, x["text"]))
        for sp in speakers.values():
            if sp["person_id"]:
                ctx.conn.execute("UPDATE person SET last_spoken_at = ? WHERE id = ?", (conv["started_at"] or now(), sp["person_id"]))

    # Plain Markdown export (A5), in the nightly export's folder and format: renames and Trash clean it up there
    conv = ctx.conn.execute("SELECT * FROM conversation WHERE id = ?", (conv_id,)).fetchone()
    out = paths.export(cfg) / "conversations" / readable.filename(conv)
    paths.atomic_write(out, readable.render(ctx.conn, conv))

    # Archive copy (HE-AAC .m4a), kept forever (D8)
    rec = _recording(ctx)
    archive = paths.archive(cfg) / f"{rec['sha256']}.m4a"
    if not archive.exists():
        archive.parent.mkdir(parents=True, exist_ok=True)
        speech.engine().to_archive(Path(rec["vault_path"]), archive)
    ctx.conn.execute("UPDATE recording SET archive_path = ? WHERE id = ?", (str(archive), rec["id"]))
    _set_status(ctx.conn, conv_id, "transcribed")
    return str(out)


def ai_extract(ctx: StageContext) -> str:
    conv_id = _conversation_id(ctx)
    if _no_ai(ctx):
        _make_private(ctx)  # Transcript Only (No AI): never sent to AI, now or later
        return "skipped_transcript_only"
    if ctx.conn.execute("SELECT private FROM conversation WHERE id = ?", (conv_id,)).fetchone()[0]:
        return "skipped_private"  # Private meetings never leave the M14
    from scribe_bot.ai.extract import extract_conversation

    try:
        result = extract_conversation(ctx.conn, conv_id)
    except NotImplementedError:
        return "not_available"  # AI layer not available; the conversation stays 'transcribed'
    if result.get("status") == "pending_approval" and ctx.conn.execute(
            "SELECT 1 FROM review_item WHERE kind = 'approval' AND conversation_id = ? AND status = 'open'",
            (conv_id,)).fetchone() is None:
        ctx.conn.execute("INSERT INTO review_item (kind, conversation_id, payload, created_at) VALUES ('approval', ?, ?, ?)",
                         (conv_id, json.dumps({"estimate": result.get("estimate")}), now()))
    return json.dumps(result)


def notify_stage(ctx: StageContext) -> str:
    conv_id = _conversation_id(ctx)
    conv = ctx.conn.execute("SELECT title, private FROM conversation WHERE id = ?", (conv_id,)).fetchone()
    link = f"/conversations/{conv_id}"
    if ctx.conn.execute("SELECT 1 FROM notification WHERE link = ? AND channel = 'bot'", (link,)).fetchone() is None:
        unknown = ctx.conn.execute("SELECT count(*) FROM review_item WHERE conversation_id = ? AND kind = 'speaker' "
                                   "AND status = 'open'", (conv_id,)).fetchone()[0]
        title = "🔒 Private recording ready" if conv["private"] else f"Ready: {conv['title']}"
        mine, theirs, unsure = ctx.conn.execute(
            "SELECT coalesce(sum(owner_person_id = 1 AND inbox = 0), 0), coalesce(sum(owner_person_id != 1 AND inbox = 0), 0), "
            "coalesce(sum(inbox = 1 OR coalesce(confidence, 1) < 0.6), 0) FROM task WHERE conversation_id = ? AND status = 'open'",
            (conv_id,)).fetchone()
        parts = [f"{mine} for you" if mine else "", f"{theirs} owed to you" if theirs else "", f"{unsure} unsure" if unsure else ""]
        if not conv["private"] and any(parts):
            title += " · " + " · ".join(x for x in parts if x)
        if unknown:
            title += f" · {unknown} speaker{'s' if unknown > 1 else ''} to name"
        first = ctx.conn.execute("SELECT min(started_at) FROM stage_run WHERE job_id = ?", (ctx.job_id,)).fetchone()[0]
        if first:
            took = (datetime.now(timezone.utc) - datetime.fromisoformat(first)).total_seconds()
            title += f" · took {progress.duration_label(took)}"
        notify.enqueue(ctx.conn, "bot", title, link)  # title and counts only, never content; goes to Discord (Brian 2026-10-22)
    ctx.conn.execute("UPDATE recording SET status = 'done' WHERE id = ?", (ctx.recording_id,))
    return link


PIPELINE: queue.Pipeline = [
    ("audio", audio),
    ("transcribe", transcribe),
    ("diarize", diarize),
    ("identify", identify_stage),
    ("file", file_stage),
    ("ai_extract", ai_extract),
    ("notify", notify_stage),
]


def rerun(conn: sqlite3.Connection, conversation_id: int, speakers: int | None = None) -> int:
    """Process a conversation again from the vault copy (e.g. "This was a 1:1" -> speakers=2). Returns the job id."""
    conv = conn.execute("SELECT recording_id, started_at FROM conversation WHERE id = ?", (conversation_id,)).fetchone()
    payload = {"started_at": conv["started_at"]}
    if speakers:
        payload["speakers"] = speakers
    return queue.enqueue(conn, "recording", conv["recording_id"], payload)


# --- scheduled jobs ---

def reconcile(conn: sqlite3.Connection, cfg: settings.Settings | None = None) -> dict:
    """Nightly: anything in the inbound folder that isn't in the database is queued, and any queued recording that
    lost its job gets one. Reports counts only."""
    cfg = cfg or settings.load()
    new = ingest.scan_inbound(conn, cfg, full=True)
    orphans = [r["id"] for r in conn.execute(
        "SELECT r.id FROM recording r WHERE r.status = 'queued' AND NOT EXISTS "
        "(SELECT 1 FROM job j WHERE j.recording_id = r.id AND j.kind = 'recording')")]
    for rec_id in orphans:
        queue.enqueue(conn, "recording", rec_id)
    found = len(new) + len(orphans)
    if found:
        notify.enqueue(conn, "alerts", f"Reconcile found {found} missed recording{'s' if found > 1 else ''}; now queued", "/")
    return {"new": len(new), "requeued": len(orphans)}


def cleanup_originals(conn: sqlite3.Connection, cfg: settings.Settings | None = None,
                      days: int = KEEP_ORIGINAL_DAYS, at: datetime | None = None) -> int:
    """D8: delete the vault's original audio 30 days after arrival, once the archive copy exists.
    The phone's copy and the Syncthing inbound copy are not touched."""
    cutoff = ((at or datetime.now(timezone.utc)) - timedelta(days=days)).isoformat(timespec="seconds")
    deleted = 0
    for r in conn.execute("SELECT id, vault_path, archive_path FROM recording WHERE original_deleted_at IS NULL "
                          "AND archive_path IS NOT NULL AND vault_path IS NOT NULL AND received_at < ?", (cutoff,)).fetchall():
        archive, original = Path(r["archive_path"]), Path(r["vault_path"])
        if not archive.exists() or archive.stat().st_size == 0:
            continue
        original.unlink(missing_ok=True)
        conn.execute("UPDATE recording SET original_deleted_at = ? WHERE id = ?", (now(), r["id"]))
        deleted += 1
    return deleted


def make_schedules(conn: sqlite3.Connection, cfg: settings.Settings | None = None) -> list[scheduler.Schedule]:
    """Reconcile at 2:30 AM (SPEC §10) and the D8 original cleanup at 3:00 AM."""
    return [
        scheduler.Schedule("reconcile", hour=2, minute=30, run=lambda missed: reconcile(conn, cfg)),
        scheduler.Schedule("original_cleanup", hour=3, run=lambda missed: cleanup_originals(conn, cfg)),
    ]
