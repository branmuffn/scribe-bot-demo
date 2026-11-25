"""T1 (Brian, 2026-11-20): every recording waiting in the Inbox gets a quick title and preview, hands-free.

The first 2 minutes are turned into text on the M14 (the same speech model as full processing; no AI, nothing sent).
The Inbox then shows the most-used words as a quick title and the first words as a preview. One recording at a time,
only while no processing job is running and never more often than every PEEK_GAP_S, so the M14 stays cool. Past
recordings are caught up the same way, newest first. A Recorder .txt (T2) that arrives later wins on the Inbox,
because it covers the whole recording; once a recording is processed, its full transcript replaces both.
Brian, 2026-11-20 (S1): when a recording already has a transcript (a Recorder .txt, one added by hand, or a Voice
Memo's own), no speech work is done for it: its start is read from that transcript instead. Settings has an on/off
switch (setting 'quick_titles' = '0' turns quick titles off); full processing is unchanged either way.
"""
from __future__ import annotations

import sqlite3
import tempfile
import time
from pathlib import Path

from scribe_bot.core import db
from scribe_bot.pipeline import speech, voicememo
from scribe_bot.web import inbox as inbox_svc

PEEK_SECONDS = 120
PEEK_WORDS = 300         # about 2 minutes of talk, when the start comes from an existing transcript
PEEK_GAP_S = 90          # at most one peek every 1.5 min (each takes ~15-30 s of M14 work)
WAITING = ('awaiting_approval', 'backlog')
_last = float("-inf")   # monotonic time can be small right after a boot: the first peek is never held back


def _transcribe_start(src: str, seconds: int) -> str:
    """In a child process (the speech model holds Python's lock): convert, keep the first `seconds`, transcribe."""
    eng = speech.engine()
    with tempfile.TemporaryDirectory(prefix="peek-") as d:
        wav = Path(d) / "peek.wav"
        eng.to_wav(Path(src), wav)
        samples = eng.load(wav)[: seconds * speech.SAMPLE_RATE]
    return " ".join(s["text"] for s in eng.transcribe(samples) if s["text"]).strip()


def next_recording(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT r.id, r.title, r.original_path, coalesce(r.vault_path, r.original_path) AS path FROM recording r "
        f"WHERE r.status IN {WAITING} AND NOT EXISTS (SELECT 1 FROM recording_peek p WHERE p.recording_id = r.id) "
        "ORDER BY r.received_at DESC, r.id DESC LIMIT 1").fetchone()


def enabled(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM setting WHERE key = 'quick_titles'").fetchone()
    return not row or row[0] != "0"


def existing_transcript(rec, data_dir: Path | None) -> str | None:
    """The start of a transcript the recording already has (hand-added, Recorder .txt beside it, or synced inbound)."""
    folders = [p for p in ((data_dir / "uploads") if data_dir else None, (data_dir / "inbound") if data_dir else None,
                           Path(rec["original_path"]).parent if rec["original_path"] else None) if p]
    path = inbox_svc.find(inbox_svc.transcript_index(folders), rec["title"], rec["original_path"])
    if not path:
        return None
    turns = inbox_svc.parse(inbox_svc.read(path, 200_000))
    return " ".join(" ".join(t["text"] for t in turns).split()[:PEEK_WORDS]) or None


def run_one(conn: sqlite3.Connection, log=None, now: float | None = None, child: bool = True,
            data_dir: Path | None = None) -> int | None:
    """Make one peek if one is due. Returns the recording id, or None when there was nothing to do (or too soon)."""
    global _last
    now = time.monotonic() if now is None else now
    if now - _last < PEEK_GAP_S or not enabled(conn):
        return None
    rec = next_recording(conn)
    if rec is None:
        return None
    _last = now
    text, error = None, None
    try:
        if not rec["path"] or not Path(rec["path"]).exists():
            raise FileNotFoundError("recording file not found")
        memo = voicememo.words(Path(rec["path"]))   # a Voice Memo carries its own transcript: no speech work needed
        if memo:
            text = "".join(w for w, t in memo if t < PEEK_SECONDS).strip()
        elif (have := existing_transcript(rec, data_dir)):   # a Recorder .txt or an added one: no speech work either
            text = have
        else:
            text = (speech.in_child(_transcribe_start, rec["path"], PEEK_SECONDS) if child
                    else _transcribe_start(rec["path"], PEEK_SECONDS))
    except Exception as e:  # noqa: BLE001 - one bad file must not stop the worker; it's marked and skipped
        error = f"{type(e).__name__}: {e}"[:300]
        if log:
            log.warning("peek of recording %d failed: %s", rec["id"], error)
    with db.transaction(conn):
        conn.execute("INSERT OR REPLACE INTO recording_peek (recording_id, text, error, made_at) VALUES (?, ?, ?, ?)",
                     (rec["id"], text, error, db.now()))
    if log:
        log.info("peek made for recording %d (%d words)", rec["id"], len((text or "").split()))
    return rec["id"]


def peeks(conn: sqlite3.Connection) -> dict[int, str]:
    return {r[0]: r[1] for r in conn.execute("SELECT recording_id, text FROM recording_peek WHERE text IS NOT NULL AND text != ''")}
