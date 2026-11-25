"""Merge before processing (Brian, 2026-11-21): two or more waiting recordings become ONE new recording (their audio
joined in the order they were recorded), so speech, speakers and the AI run once and make one talk. Each part is kept in
recording_part with where it starts, so the transcript can show which recording each part came from. The originals are
kept, hidden from the Inbox (status 'merged'); Unmerge restores them while the joined one hasn't been processed."""
from __future__ import annotations

import shutil
import sqlite3
import subprocess
import wave
from pathlib import Path

from scribe_bot.core import settings
from scribe_bot.core.db import now, transaction
from scribe_bot.pipeline import ingest, paths, speech

WAITING = ("awaiting_approval", "backlog")


class MergeError(ValueError):
    pass


def _to_wav(src: Path, dst: Path) -> None:
    speech.engine().to_wav(src, dst)


def _encode(src_wav: Path, dst: Path) -> None:
    """The archive format (HE-AAC mono 24 kbit/s .m4a): plays everywhere, ~11 MB per audio hour. Plain AAC at 64 kbit/s
    is refused for 16 kHz mono audio (found testing on the M14, 2026-11-21)."""
    tmp = dst.with_name(dst.stem + ".tmp.m4a")
    subprocess.run(["afconvert", "-f", "m4af", "-d", "aach", "-c", "1", "-b", "24000", str(src_wav), str(tmp)], check=True)
    tmp.replace(dst)


def _start(r) -> str:
    """When a recording began: a Voice Memos name ("20261110 134647") is its start; else the Recorder name or file time."""
    from datetime import datetime
    try:
        return datetime.strptime((r["title"] or "")[:15], "%Y%m%d %H%M%S").astimezone().isoformat(timespec="seconds")
    except ValueError:
        pass
    p = Path(r["original_path"])
    return ingest._started_at(p, r["duration_s"] or 0) if p.exists() else r["received_at"]


def _short(title: str | None, n: int = 28) -> str:
    t = (title or "Recording").strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def merge(conn: sqlite3.Connection, cfg: settings.Settings, rec_ids: list[int]) -> int:
    """Join the waiting recordings `rec_ids` into a new one waiting in the Inbox. Returns its id."""
    ids = list(dict.fromkeys(int(i) for i in rec_ids))
    if len(ids) < 2:
        raise MergeError("Pick at least two recordings to merge")
    rows = [conn.execute("SELECT * FROM recording WHERE id = ?", (i,)).fetchone() for i in ids]
    if any(r is None for r in rows):
        raise MergeError("One of those recordings is gone")
    if any(r["status"] not in WAITING for r in rows):
        raise MergeError("Only recordings still waiting in the Inbox can be merged")
    starts = {r["id"]: _start(r) for r in rows}
    rows.sort(key=lambda r: starts[r["id"]] or "")
    work = cfg.data_dir / "tmp" / f"merge-{'-'.join(str(r['id']) for r in rows)}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    try:
        parts, offset, params = [], 0.0, None
        joined = work / "joined.wav"
        with wave.open(str(joined), "wb") as out:
            for i, r in enumerate(rows):
                src = Path(r["vault_path"] or r["original_path"])
                if not src.exists():
                    raise MergeError(f"The audio for “{_short(r['title'])}” is missing")
                w = work / f"{i}.wav"
                _to_wav(src, w)
                with wave.open(str(w)) as inp:
                    if params is None:
                        params = inp.getparams()
                        out.setnchannels(params.nchannels); out.setsampwidth(params.sampwidth); out.setframerate(params.framerate)
                    frames = inp.readframes(inp.getnframes())
                    dur = inp.getnframes() / inp.getframerate()
                out.writeframes(frames)
                parts.append((r, offset, dur))
                offset += dur
        title = " + ".join(_short(r["title"], 40 if len(rows) == 2 else 24) for r in rows)
        dst = paths.uploads(cfg) / f"{title.replace('/', '-')}.m4a"
        dst.parent.mkdir(parents=True, exist_ok=True)
        _encode(joined, dst)
        new_id = ingest.add_file(conn, cfg, dst, wait=True)
        if new_id is None:
            raise MergeError("Those recordings were already merged")
        with transaction(conn):
            conn.execute("UPDATE recording SET title = ?, private = ? WHERE id = ?",
                         (title, int(any(r["private"] for r in rows)), new_id))
            for i, (r, off, dur) in enumerate(parts):
                conn.execute("INSERT INTO recording_part (recording_id, source_id, idx, offset_s, duration_s, title, started_at, prev_status) "
                             "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (new_id, r["id"], i, round(off, 2), round(dur, 2), r["title"], starts[r["id"]], r["status"]))
                conn.execute("UPDATE recording SET status = 'merged' WHERE id = ?", (r["id"],))
                conn.execute("UPDATE review_item SET status = 'done', resolved_at = ? WHERE kind = 'approval' AND ref_id = ? "
                             "AND status = 'open'", (now(), r["id"]))
            conn.execute("UPDATE review_item SET payload = json_set(payload, '$.title', ?, '$.merged', ?) WHERE kind = 'approval' "
                         "AND ref_id = ? AND status = 'open'", (title, len(rows), new_id))
        return new_id
    finally:
        shutil.rmtree(work, ignore_errors=True)


def unmerge(conn: sqlite3.Connection, cfg: settings.Settings, rec_id: int) -> list[int]:
    """Undo a merge that hasn't been processed: the originals come back to the Inbox, the joined file goes."""
    rec = conn.execute("SELECT * FROM recording WHERE id = ?", (rec_id,)).fetchone()
    parts = conn.execute("SELECT * FROM recording_part WHERE recording_id = ? ORDER BY idx", (rec_id,)).fetchall()
    if rec is None or not parts:
        raise MergeError("That isn't a merged recording")
    if rec["status"] not in WAITING:
        raise MergeError("It has already been processed; it can't be split back now")
    with transaction(conn):
        for p in parts:
            conn.execute("UPDATE recording SET status = ? WHERE id = ?", (p["prev_status"], p["source_id"]))
            item = conn.execute("SELECT id FROM review_item WHERE kind = 'approval' AND ref_id = ? ORDER BY id DESC LIMIT 1",
                                (p["source_id"],)).fetchone()
            if item:
                conn.execute("UPDATE review_item SET status = 'open', resolved_at = NULL WHERE id = ?", (item[0],))
        conn.execute("DELETE FROM review_item WHERE kind = 'approval' AND ref_id = ?", (rec_id,))
        conn.execute("DELETE FROM recording WHERE id = ?", (rec_id,))
    for f, home in ((rec["vault_path"], paths.vault(cfg)), (rec["original_path"], paths.uploads(cfg))):
        if f and Path(f).resolve().is_relative_to(home.resolve()):   # only files this merge made
            Path(f).unlink(missing_ok=True)
    return [p["source_id"] for p in parts]


def parts_of(conn: sqlite3.Connection, rec_id: int | None) -> list[dict]:
    if not rec_id:
        return []
    return [dict(r) for r in conn.execute("SELECT * FROM recording_part WHERE recording_id = ? ORDER BY idx", (rec_id,))]


def first_start(conn: sqlite3.Connection, rec_id: int) -> str | None:
    r = conn.execute("SELECT started_at FROM recording_part WHERE recording_id = ? ORDER BY idx LIMIT 1", (rec_id,)).fetchone()
    return r[0] if r else None



# ---------- Merge finished talks (Brian, 2026-11-22: two talks transcribed overnight were one conversation) ----------

def _talk_audio(conn: sqlite3.Connection, c) -> Path | None:
    if not c["recording_id"]:
        return None
    r = conn.execute("SELECT vault_path, archive_path FROM recording WHERE id = ?", (c["recording_id"],)).fetchone()
    for p in (r or []):
        if p and Path(p).is_file():
            return Path(p)
    return None


def merge_talks(conn: sqlite3.Connection, cfg: settings.Settings, cids: list[int]) -> int:
    """Join finished talks into ONE new talk, in recorded order: transcript lines (times shifted), speakers (the same
    named person becomes one speaker), tags and the joined audio. The new talk has no AI notes yet (Summarize it).
    The originals go to Trash (Restore within 30 days); nothing is re-transcribed. Returns the new talk's id."""
    import hashlib
    ids = list(dict.fromkeys(int(i) for i in cids))
    if len(ids) < 2:
        raise MergeError("Pick at least two talks to merge")
    convs = [conn.execute("SELECT * FROM conversation WHERE id = ? AND deleted_at IS NULL", (i,)).fetchone() for i in ids]
    if any(c is None for c in convs):
        raise MergeError("One of those talks is gone")
    if any(c["status"] == "transcribing" for c in convs):
        raise MergeError("Wait until every picked talk has finished processing")
    convs.sort(key=lambda c: c["started_at"] or c["created_at"])
    lengths = [c["duration_s"] or conn.execute("SELECT coalesce(max(end_s), 0) FROM segment WHERE conversation_id = ?",
                                               (c["id"],)).fetchone()[0] for c in convs]
    audio = [_talk_audio(conn, c) for c in convs]
    new_rec, parts = None, []
    if all(audio):   # join the audio; a talk without audio (a pasted transcript) gives a text-only merge
        work = cfg.data_dir / "tmp" / f"talks-{'-'.join(str(c['id']) for c in convs)}"
        shutil.rmtree(work, ignore_errors=True)
        work.mkdir(parents=True)
        try:
            joined, offset, params = work / "joined.wav", 0.0, None
            with wave.open(str(joined), "wb") as out:
                for i, (c, src) in enumerate(zip(convs, audio)):
                    w = work / f"{i}.wav"
                    _to_wav(src, w)
                    with wave.open(str(w)) as inp:
                        if params is None:
                            params = inp.getparams()
                            out.setnchannels(params.nchannels); out.setsampwidth(params.sampwidth); out.setframerate(params.framerate)
                        out.writeframes(inp.readframes(inp.getnframes()))
                        dur = inp.getnframes() / inp.getframerate()
                    parts.append((c, offset, dur))
                    offset += dur
            dst = paths.archive(cfg) / f"merged-talks-{'-'.join(str(c['id']) for c in convs)}.m4a"
            dst.parent.mkdir(parents=True, exist_ok=True)
            _encode(joined, dst)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        sha = hashlib.sha256(dst.read_bytes()).hexdigest()
    else:
        offset = 0.0
        for c, n in zip(convs, lengths):
            parts.append((c, offset, n))
            offset += n
    title = " + ".join(_short(c["short_title"] or c["title"], 40 if len(convs) == 2 else 24) for c in convs)
    t = now()
    with transaction(conn):
        if all(audio):
            # archive_path only (no vault_path): the 30-day original cleanup never touches it
            new_rec = conn.execute(
                "INSERT INTO recording (source, original_path, sha256, duration_s, received_at, status, title, archive_path) "
                "VALUES ('merge', ?, ?, ?, ?, 'done', ?, ?)", (str(dst), sha, round(offset, 2), t, title, str(dst))).lastrowid
            for i, (c, off, dur) in enumerate(parts):
                if c["recording_id"]:
                    conn.execute("INSERT INTO recording_part (recording_id, source_id, idx, offset_s, duration_s, title, started_at, prev_status) "
                                 "VALUES (?, ?, ?, ?, ?, ?, ?, 'done')", (new_rec, c["recording_id"], i, round(off, 2), round(dur, 2),
                                                                          c["short_title"] or c["title"], c["started_at"]))
        first = convs[0]
        cid = conn.execute(
            "INSERT INTO conversation (recording_id, title, started_at, duration_s, type, area, status, processed_by, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, 'transcribed', 'local', ?, ?)",
            (new_rec, title, first["started_at"], round(offset, 2), first["type"], first["area"], t, t)).lastrowid
        sp_map, by_person, cluster = {}, {}, 0
        for c in convs:
            for s in conn.execute("SELECT * FROM speaker WHERE conversation_id = ? ORDER BY cluster", (c["id"],)):
                if s["person_id"] and s["person_id"] in by_person:
                    sp_map[s["id"]] = by_person[s["person_id"]]
                    conn.execute("UPDATE speaker SET talk_seconds = talk_seconds + ? WHERE id = ?", (s["talk_seconds"], sp_map[s["id"]]))
                    continue
                new = conn.execute("INSERT INTO speaker (conversation_id, cluster, person_id, match_score, confirmed, talk_seconds, clip_path, color) "
                                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (cid, cluster, s["person_id"], s["match_score"], s["confirmed"],
                                                                       s["talk_seconds"], s["clip_path"], s["color"])).lastrowid
                cluster += 1
                sp_map[s["id"]] = new
                if s["person_id"]:
                    by_person[s["person_id"]] = new
        idx = 0
        for c, off, _ in parts:
            for g in conn.execute("SELECT * FROM segment WHERE conversation_id = ? ORDER BY idx", (c["id"],)).fetchall():
                sid = conn.execute("INSERT INTO segment (conversation_id, idx, start_s, end_s, speaker_id, text, confidence) "
                                   "VALUES (?, ?, ?, ?, ?, ?, ?)", (cid, idx, round(g["start_s"] + off, 2), round(g["end_s"] + off, 2),
                                                                    sp_map.get(g["speaker_id"]), g["text"], g["confidence"])).lastrowid
                conn.execute("INSERT INTO search_index (kind, ref_id, conversation_id, text) VALUES ('segment', ?, ?, ?)", (sid, cid, g["text"]))
                idx += 1
            conn.execute("INSERT OR IGNORE INTO conversation_tag (conversation_id, tag_id, by_ai, created_at) "
                         "SELECT ?, tag_id, by_ai, ? FROM conversation_tag WHERE conversation_id = ?", (cid, t, c["id"]))
            conn.execute("UPDATE conversation SET deleted_at = ?, updated_at = ? WHERE id = ?", (t, t, c["id"]))
    return cid
