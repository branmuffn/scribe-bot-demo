"""Test recordings for the Inbox (Brian, 2026-11-25, M7): three clearly labelled, made-up recordings in different states,
so Inbox picking, preview, search, Transcribe and progress can be tried without touching a real recording.

- Made only from synthetic text. On the M14 the audio is spoken by macOS's built-in `say` (real speech, so Transcribe
  has words to find); anywhere else it is a short tone. Nothing is sent anywhere.
- Every file lives in ~/scribe-bot-data/test-inbox/ (never the synced Recordings folder, so nothing reaches the phone),
  and every title starts with "TEST". remove() deletes exactly those: matched by BOTH the folder and the title, so a
  real recording can never be caught. Talks made from them (if Transcribe was pressed) go too, with no device-cleanup
  reminder (the recordings are deleted outright, never marked for the 30-day audio clean-up).

On the M14:  ~/.local/bin/uv run --project ~/scribe-bot python -m scribe_bot.web.test_inbox add | add-more | remove | status
Or: Help › Test recordings › Add / Remove.
"""
from __future__ import annotations

import hashlib
import math
import shutil
import struct
import subprocess
import sys
import wave
from pathlib import Path

from scribe_bot.core import db
from scribe_bot.core.db import now, transaction
from scribe_bot.pipeline import ingest, paths

PREFIX = "TEST"
ITEMS = [  # (title, state, words spoken, Recorder-style transcript beside it?)
    ("TEST 1 · waiting, with a phone transcript", "awaiting_approval",
     "Hi Jordan. This is a test recording for the Scribe Bot inbox. Let's talk about the pineapple budget for the "
     "spring picnic. The pineapple budget is two hundred dollars, and the picnic is on Saturday.", True),
    ("TEST 2 · waiting, no transcript yet", "awaiting_approval",
     "This is the second test recording. It has no transcript, so Scribe Bot makes a quick title from the first "
     "minutes on its own. We are planning the volcano science fair. The volcano needs baking soda and vinegar.", False),
    ("TEST 3 · older recording", "backlog",
     "Third test recording, filed as an older one. The garden committee wants more tomato plants. Tomato seedlings "
     "arrive next week, and the committee meets on Tuesday.", False),
]


# R4-24 (Brian, 2026-11-25: "3 test recordings isn't enough to scroll"): 30 more, short tones (no speech, so they're
# quick to make), mixed waiting and older, a few with long titles. Same TEST prefix and folder, so Remove takes them too.
TOPICS = ["pineapple budget", "volcano fair", "tomato seedlings", "kite festival", "bake sale", "robot club",
          "library hours", "bike repair", "choir practice", "chess night"]
FILLERS = [(f"TEST {n} · {TOPICS[n % 10]}" + (" with a much longer title to see how rows wrap on a phone" if n % 7 == 0 else ""),
            "awaiting_approval" if n % 3 else "backlog") for n in range(4, 34)]


def folder(cfg) -> Path:
    return Path(cfg.data_dir) / "test-inbox"


def _tone(path: Path, seconds: float, freq: float) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"".join(struct.pack("<h", int(3000 * math.sin(2 * math.pi * freq * i / 16000)))
                               for i in range(int(seconds * 16000))))


def _speak(path: Path, text: str, freq: float) -> None:
    """Real speech with macOS `say` (built in on the M14), else a tone. Always a 16 kHz WAV."""
    if shutil.which("say"):
        try:
            subprocess.run(["say", "-o", str(path), "--file-format=WAVE", "--data-format=LEI16@16000", text],
                           check=True, timeout=120, capture_output=True)
            if path.exists() and path.stat().st_size > 1000:
                return
        except (OSError, subprocess.SubprocessError):
            pass
    _tone(path, 4 + len(text) / 40, freq)


def _duration(path: Path) -> float:
    with wave.open(str(path), "rb") as w:
        return w.getnframes() / float(w.getframerate())


def _transcript(text: str) -> str:
    """Google Recorder's export shape: a title line, then "[Speaker 1]" turns."""
    half = len(text) // 2
    cut = text.rfind(". ", 0, half) + 1 or half
    return f"Test transcript\n\n[Speaker 1]\n{text[:cut].strip()}\n\n[Speaker 2]\n{text[cut:].strip()}\n"


def ours(conn, cfg) -> list[int]:
    """Ids of the test recordings: inside the test folder AND titled TEST… (both, so nothing real can match)."""
    root = str(folder(cfg)) + "/"
    return [r[0] for r in conn.execute("SELECT id FROM recording WHERE title LIKE ? AND original_path LIKE ?",
                                       (PREFIX + " %", root + "%"))]


def add(conn, cfg, more: bool = False) -> list[int]:
    """Add the three test recordings (again, if they were removed), or with more=True the 30 scroll fillers.
    Returns the new recording ids."""
    have = {r[0] for r in conn.execute("SELECT title FROM recording WHERE id IN (%s)" % ",".join(map(str, ours(conn, cfg)) or ["0"]))}
    if more:
        todo = [(t, st, None, False) for t, st in FILLERS if t not in have]
    else:
        if have & {t for t, *_ in ITEMS}:
            return []
        todo = ITEMS
    d = folder(cfg)
    d.mkdir(parents=True, exist_ok=True)
    made = []
    for n, (title, state, words, with_txt) in enumerate(todo, 1):
        src = d / f"{title}.wav"
        if words:
            _speak(src, words, 330 + 110 * n)
        else:
            _tone(src, 3 + n % 5, 200 + 37 * n)
        digest = ingest.sha256(src)
        if conn.execute("SELECT 1 FROM recording WHERE sha256 = ?", (digest,)).fetchone():
            with open(src, "ab") as f:   # same sound as a recording already known: make it unique
                f.write(hashlib.sha256(title.encode()).digest())
            digest = ingest.sha256(src)
        if with_txt:
            (d / f"{title}.txt").write_text(_transcript(words), encoding="utf-8")
        vault = None
        if state != "backlog":
            dst = paths.vault(cfg) / digest[:2] / f"{digest}.wav"
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            vault = str(dst)
        dur = _duration(src)
        with transaction(conn):
            rid = conn.execute("INSERT INTO recording (source, original_path, sha256, duration_s, received_at, status, title, "
                               "vault_path, private) VALUES ('upload', ?, ?, ?, ?, ?, ?, ?, 0)",
                               (str(src), digest, dur, now(), state, title, vault)).lastrowid
            if state == "awaiting_approval":
                ingest.request_approval(conn, rid, title, dur, False)
        made.append(rid)
    return made


def remove(conn, cfg) -> int:
    """Delete the test recordings, any talks made from them, and their files. Returns how many recordings."""
    ids = ours(conn, cfg)
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    talks = [r[0] for r in conn.execute(f"SELECT id FROM conversation WHERE recording_id IN ({marks})", ids)]
    files = [f for r in conn.execute(f"SELECT original_path, vault_path, archive_path FROM recording WHERE id IN ({marks})", ids)
             for f in r if f]
    jobs = [r[0] for r in conn.execute(f"SELECT id FROM job WHERE recording_id IN ({marks})", ids)]
    if talks:
        from scribe_bot.backup import trash
        trash._purge_talks(conn, talks)
    with transaction(conn):
        conn.execute(f"DELETE FROM review_item WHERE kind = 'approval' AND ref_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM recording_peek WHERE recording_id IN ({marks})", ids)
        conn.execute(f"DELETE FROM recording_part WHERE recording_id IN ({marks}) OR source_id IN ({marks})", ids + ids)
        if jobs:
            jm = ",".join("?" * len(jobs))
            conn.execute(f"DELETE FROM stage_run WHERE job_id IN ({jm})", jobs)
            conn.execute(f"DELETE FROM job WHERE id IN ({jm})", jobs)
        conn.execute(f"DELETE FROM recording WHERE id IN ({marks})", ids)
    root = Path(cfg.data_dir).resolve()
    for f in files:
        p = Path(f)
        try:
            if p.resolve().is_relative_to(root) and (p.resolve().is_relative_to(folder(cfg).resolve())
                                                     or p.resolve().is_relative_to(paths.vault(cfg).resolve())):
                p.unlink(missing_ok=True)
        except OSError:
            pass
    for jid in jobs:   # leftover temporary audio from processing
        d = Path(cfg.jobs_dir) / str(jid)
        if d.is_dir() and d.resolve().is_relative_to(root):
            shutil.rmtree(d, ignore_errors=True)
    d = folder(cfg)
    if d.is_dir():
        for p in d.glob(PREFIX + " *"):
            p.unlink(missing_ok=True)
    return len(ids)


def main(argv: list[str]) -> int:
    from scribe_bot.core import settings
    cfg = settings.load()
    conn = db.connect(cfg.db_path)
    what = (argv or ["status"])[0]
    if what in ("add", "add-more"):
        n = add(conn, cfg, more=what == "add-more")
        print(f"added {len(n)} test recordings to the Inbox" if n else "the test recordings are already there")
    elif what == "remove":
        print(f"removed {remove(conn, cfg)} test recordings (and anything made from them)")
    else:
        print(f"{len(ours(conn, cfg))} test recordings in the Inbox")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
