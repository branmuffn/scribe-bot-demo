"""Ingest (stage 1): pick up new recordings from the Syncthing inbound folder.

Each new file is hashed (sha256 dedupe), copied into the vault (so phone deletions never affect it), recorded with its
title (from the file name) and Private flag ("private" anywhere in the title; DECISIONS 2026-10-19), and queued as a
'recording' job. Files still being synced (changed in the last 30 s) and Syncthing's hidden temp files are skipped.

Backlog: the first scan on a machine records the files already in the inbound folder as status 'backlog' without
queueing them (Brian asked not to process the existing recordings yet). process_backlog() queues chosen ones later.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import time
from pathlib import Path

from scribe_bot.core import queue, settings
from scribe_bot.core.db import now, transaction
from scribe_bot.discord_bot import notify
from scribe_bot.pipeline import paths, recorder_names, speech

SETTLE_SECONDS = 30
BASELINE_KEY = "inbound_baseline_at"
_seen: dict[str, tuple[int, int]] = {}  # path -> (size, mtime_ns) already handled this run, so ticks stay cheap


def is_private(title: str) -> bool:
    """Anywhere in the title, any case ("Private", "call_private", "Privately"): over-matching is the safe side."""
    return re.search("private", title, re.IGNORECASE) is not None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def inbound_files(cfg: settings.Settings) -> list[Path]:
    """Audio from the phone (inbound/) and from the web app (uploads/)."""
    found = []
    for root in (paths.inbound(cfg), paths.uploads(cfg)):
        if root.exists():
            found += [
                p for p in root.rglob("*")
                if p.is_file() and p.suffix.lower() in paths.AUDIO_EXTS
                and not any(part.startswith(".") or part.startswith("~") for part in p.relative_to(root).parts)
            ]
    return sorted(found)


def _known(conn: sqlite3.Connection, digest: str) -> bool:
    return conn.execute("SELECT 1 FROM recording WHERE sha256 = ?", (digest,)).fetchone() is not None


def _started_at(path: Path, duration: float) -> str:
    """From the Recorder name ("Oct 16 at 09_29"); else the file time (when recording ended) minus the length."""
    return recorder_names.started_at(path.stem, path.stat().st_mtime, duration)


# Ask-first mode (DECISIONS 2026-10-19, testing phase): new recordings wait in Review until Brian approves them.
# Rough estimates shown with the request, from M0/#3 measurements on the M14.
M14_MINUTES_PER_AUDIO_MINUTE = 0.3     # ~13 min/audio hour + the 1:1 re-separation and embeddings
AI_USD_PER_AUDIO_MINUTE = 0.0011       # #3: ~$0.045 for 42 min on Sonnet


def ask_first(conn: sqlite3.Connection) -> bool:
    """Setting 'processing_mode': 'ask' (default) or 'auto'."""
    row = conn.execute("SELECT value FROM setting WHERE key = 'processing_mode'").fetchone()
    return (row[0] if row else "ask") != "auto"


def request_approval(conn: sqlite3.Connection, rec_id: int, title: str, duration: float | None, private: bool) -> None:
    minutes = round((duration or 0) / 60, 1)
    payload = {"recording_id": rec_id, "title": title, "minutes": minutes, "private": private,
               "m14_minutes": round(minutes * M14_MINUTES_PER_AUDIO_MINUTE),
               "ai_cost": 0.0 if private else round(minutes * AI_USD_PER_AUDIO_MINUTE, 3)}
    conn.execute("INSERT INTO review_item (kind, ref_id, payload, created_at) VALUES ('approval', ?, ?, ?)",
                 (rec_id, json.dumps(payload), now()))
    # Brian, 2026-11-21: no "New recording waiting for your OK" note any more (35 a month; he made the recording, and
    # the Inbox tab shows the count). Notes are kept for things that need him or that he can't see coming.


def add_file(conn: sqlite3.Connection, cfg: settings.Settings, path: Path, backlog: bool = False) -> int | None:
    """Record one inbound file. Returns the new recording id, or None if it was already known."""
    digest = sha256(path)
    if _known(conn, digest):
        return None
    eng = speech.engine()
    duration = eng.duration(path)
    title = path.stem
    vault_path = None
    if not backlog:
        dst = paths.vault(cfg) / digest[:2] / f"{digest}{path.suffix.lower()}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            tmp = dst.with_name(dst.name + ".tmp")
            shutil.copy2(path, tmp)
            tmp.replace(dst)
        vault_path = str(dst)
    hold = not backlog and ask_first(conn)
    with transaction(conn):
        if _known(conn, digest):  # another scan got there first
            return None
        cur = conn.execute(
            "INSERT INTO recording (source, original_path, sha256, duration_s, received_at, status, title, vault_path, private) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("upload" if paths.uploads(cfg) in path.parents else "recorder", str(path), digest, duration, now(),
             "backlog" if backlog else ("awaiting_approval" if hold else "queued"), title, vault_path,
             0),  # Private is gone (Brian, 2026-11-21)
        )
        rec_id = cur.lastrowid
        if backlog:
            pass
        elif hold:
            request_approval(conn, rec_id, title, duration, False)
        else:
            queue.enqueue(conn, "recording", rec_id, {"started_at": _started_at(path, duration)})
    return rec_id


def _settled(path: Path) -> bool:
    return time.time() - path.stat().st_mtime >= SETTLE_SECONDS


def scan_inbound(conn: sqlite3.Connection, cfg: settings.Settings | None = None, full: bool = False) -> list[int]:
    """Called every worker tick. Returns ids of newly queued recordings. full=True re-hashes everything (reconcile)."""
    cfg = cfg or settings.load()
    files = inbound_files(cfg)
    if conn.execute("SELECT 1 FROM setting WHERE key = ?", (BASELINE_KEY,)).fetchone() is None:
        for p in files:
            add_file(conn, cfg, p, backlog=True)
            st = p.stat()
            _seen[str(p)] = (st.st_size, st.st_mtime_ns)
        conn.execute("INSERT INTO setting (key, value) VALUES (?, ?)", (BASELINE_KEY, now()))
        return []
    new = []
    for p in files:
        st = p.stat()
        sig = (st.st_size, st.st_mtime_ns)
        if not full and _seen.get(str(p)) == sig:
            continue
        if not _settled(p):
            continue
        rec_id = add_file(conn, cfg, p)
        _seen[str(p)] = sig
        if rec_id is not None:
            new.append(rec_id)
    return new


def approve(conn: sqlite3.Connection, rec_id: int, ai: bool = True, meeting_type: str | None = None) -> int:
    """Brian approved a waiting recording: queue it. ai=False = transcript only (no cloud call). meeting_type = his
    pick (else it comes from the speaker count, and the AI may spot a Learning talk)."""
    rec = conn.execute("SELECT original_path, duration_s FROM recording WHERE id = ?", (rec_id,)).fetchone()
    path = Path(rec["original_path"])
    started = _started_at(path, rec["duration_s"] or 0) if path.exists() else None
    part = conn.execute("SELECT started_at FROM recording_part WHERE recording_id = ? ORDER BY idx LIMIT 1", (rec_id,)).fetchone()
    if part and part[0]:   # a merged recording starts when its first part did
        started = part[0]
    conn.execute("UPDATE recording SET status = 'queued' WHERE id = ?", (rec_id,))
    return queue.enqueue(conn, "recording", rec_id, {"started_at": started, "ai": ai,
                                                     **({"type": meeting_type} if meeting_type else {})})


def backlog_to_review(conn: sqlite3.Connection, rec_id: int, cfg: settings.Settings | None = None) -> bool:
    """An older (backlog) recording Brian picked: copy it into the vault and put it in Review, waiting for his OK like a
    new recording (nothing runs or costs anything until he approves). False if it isn't a backlog recording."""
    cfg = cfg or settings.load()
    row = conn.execute("SELECT * FROM recording WHERE id = ? AND status = 'backlog'", (rec_id,)).fetchone()
    if row is None or not Path(row["original_path"]).exists():
        return False
    src = Path(row["original_path"])
    dst = paths.vault(cfg) / row["sha256"][:2] / f"{row['sha256']}{src.suffix.lower()}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not dst.exists():
        tmp = dst.with_name(dst.name + ".tmp")
        shutil.copy2(src, tmp)
        tmp.replace(dst)
    with transaction(conn):
        conn.execute("UPDATE recording SET status = 'awaiting_approval', vault_path = ? WHERE id = ?", (str(dst), rec_id))
        request_approval(conn, rec_id, row["title"], row["duration_s"], False)
    return True


def process_backlog(conn: sqlite3.Connection, recording_ids: list[int], cfg: settings.Settings | None = None) -> list[int]:
    """Queue chosen backlog recordings (copies them into the vault first). Returns the ids queued."""
    cfg = cfg or settings.load()
    queued = []
    for rec_id in recording_ids:
        row = conn.execute("SELECT * FROM recording WHERE id = ? AND status = 'backlog'", (rec_id,)).fetchone()
        if row is None or not Path(row["original_path"]).exists():
            continue
        src = Path(row["original_path"])
        dst = paths.vault(cfg) / row["sha256"][:2] / f"{row['sha256']}{src.suffix.lower()}"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            tmp = dst.with_name(dst.name + ".tmp")
            shutil.copy2(src, tmp)
            tmp.replace(dst)
        with transaction(conn):
            conn.execute("UPDATE recording SET status = 'queued', vault_path = ? WHERE id = ?", (str(dst), rec_id))
            queue.enqueue(conn, "recording", rec_id, {"started_at": _started_at(src, row["duration_s"] or 0)})
        queued.append(rec_id)
    return queued
