"""Trash: conversations deleted in the app sit in Trash for 30 days, then are purged (SPEC §10 Retention).

Purging removes the conversation and everything that cascades from it (segments, speakers, decisions, people notes,
review items) plus its search rows and its tasks (Brian's DECISION Q1 A, 2026-10-20: no loose tasks left behind).
Audio (Brian, 2026-11-21: "yes, delete the audio as well"): when a talk is purged, its recording's audio files are
deleted too (Scribe Bot's vault and archive copies and the synced/uploaded file on the M14; the inbound folder is
receive-only, so the phone's own copy is never touched). The same goes for a recording Skipped in the Inbox 30 days
ago. The recording row stays, marked audio_deleted_at, so the same file is never imported again. Only files inside
the data folder are ever deleted.
"""
from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scribe_bot.core.db import transaction

TRASH_DAYS = 30


def purge(conn: sqlite3.Connection, at: datetime | None = None, days: int = TRASH_DAYS, cfg=None) -> int:
    """Remove talks trashed more than `days` ago. With cfg (the nightly job), their audio goes too (see top)."""
    at = at or datetime.now(timezone.utc)
    cutoff = (at - timedelta(days=days)).astimezone(timezone.utc).isoformat(timespec="seconds")
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM conversation WHERE deleted_at IS NOT NULL AND deleted_at < ?", (cutoff,))]
    n = _purge_talks(conn, ids) if ids else 0
    if cfg is not None:
        purge_audio(conn, cfg, cutoff)
    return n


def _purge_talks(conn: sqlite3.Connection, ids: list[int]) -> int:
    marks = ",".join("?" * len(ids))
    with transaction(conn):
        conn.execute(f"DELETE FROM search_index WHERE conversation_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM review_item WHERE kind = 'inbox' AND ref_id IN (SELECT id FROM task WHERE conversation_id IN ({marks}))", ids)
        conn.execute(f"DELETE FROM task WHERE conversation_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM conversation WHERE id IN ({marks})", ids)
    return len(ids)


def purge_audio(conn: sqlite3.Connection, cfg, cutoff: str) -> int:
    """Delete the audio of recordings nobody can reach any more: no talk left (it was purged from Trash), or Skipped in
    the Inbox before `cutoff`. Merged recordings take their original parts with them. Returns recordings cleared."""
    gone = [r[0] for r in conn.execute(
        "SELECT r.id FROM recording r WHERE r.audio_deleted_at IS NULL AND ("
        "  (r.status = 'done' AND NOT EXISTS (SELECT 1 FROM conversation c WHERE c.recording_id = r.id))"
        "  OR (r.status = 'declined' AND EXISTS (SELECT 1 FROM review_item i WHERE i.kind = 'approval' AND i.ref_id = r.id"
        "      AND i.status = 'dismissed' AND i.resolved_at < ?)"
        "      AND NOT EXISTS (SELECT 1 FROM review_item i WHERE i.kind = 'approval' AND i.ref_id = r.id AND i.status = 'open')))",
        (cutoff,))]
    for rid in list(gone):   # a merged recording's original parts are only reachable through it
        gone += [r[0] for r in conn.execute(
            "SELECT p.source_id FROM recording_part p JOIN recording s ON s.id = p.source_id "
            "WHERE p.recording_id = ? AND s.status = 'merged' AND s.audio_deleted_at IS NULL", (rid,))]
    root = Path(cfg.data_dir).resolve()
    for rid in dict.fromkeys(gone):
        r = conn.execute("SELECT original_path, vault_path, archive_path FROM recording WHERE id = ?", (rid,)).fetchone()
        for f in (r["original_path"], r["vault_path"], r["archive_path"]):
            if f and _inside(Path(f), root):
                Path(f).unlink(missing_ok=True)
        for (jid,) in conn.execute("SELECT id FROM job WHERE recording_id = ? AND status IN ('done', 'failed')", (rid,)):
            d = Path(cfg.jobs_dir) / str(jid)   # leftover temporary audio from processing
            if _inside(d, root) and d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
        conn.execute("UPDATE recording SET audio_deleted_at = ? WHERE id = ?",
                     (datetime.now(timezone.utc).isoformat(timespec="seconds"), rid))
    from scribe_bot.core import cleanup_devices   # Brian (2026-11-21): ask to delete them from the devices too
    cleanup_devices.note(conn, cleanup_devices.add(conn, list(dict.fromkeys(gone))))
    conn.commit()
    return len(dict.fromkeys(gone))


def _inside(p: Path, root: Path) -> bool:
    try:
        return p.resolve().is_relative_to(root) and p.resolve() != root
    except OSError:
        return False
