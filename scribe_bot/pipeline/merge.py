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
        new_id = ingest.add_file(conn, cfg, dst)
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

