"""Voiceprints (stage 5): match speaker clusters to known people, the 1:1 rule, and enrolment.

A voiceprint is an averaged, normalised voice embedding (numbers, not audio) stored in the voiceprint table.
Everything here uses speaker.cluster, the speaker-separation number, so names can't swap (M0 bug, 2026-10-19).
"""
from __future__ import annotations

import json
import math
import sqlite3
from array import array
from pathlib import Path

from scribe_bot.core import settings
from scribe_bot.core.db import now, transaction
from scribe_bot.pipeline import paths

ME = 1                       # person id of Brian
MATCH_THRESHOLD = 0.5        # M0 #3: Brian 0.87 vs other speaker 0.07
CONFIRM_BELOW = 0.85         # weaker voice matches keep the name but ask Brian to confirm it (F7, 2026-10-20)
ONE_ON_ONE_SHARE = 0.95      # DECISIONS 2026-10-19 option A (#3 measured 98%)
MERGE_MIN, MERGE_MARGIN = 0.3, 0.05  # 1:1 fast path: voice match must be clear, else go by time (check on the M14)


def to_blob(vec: list[float]) -> bytes:
    return array("f", vec).tobytes()


def from_blob(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return list(a)


def normalise(vec: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in vec)) or 1.0
    return [x / n for x in vec]


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both normalised


def known_prints(conn: sqlite3.Connection) -> dict[int, list[float]]:
    return {r["person_id"]: from_blob(r["embedding"]) for r in conn.execute("SELECT person_id, embedding FROM voiceprint")}


def match(clusters: dict[int, list[float]], prints: dict[int, list[float]],
          threshold: float = MATCH_THRESHOLD) -> dict[int, tuple[int, float]]:
    """cluster -> (person_id, score). Each person gets at most one cluster: best pairs first."""
    pairs = sorted(((cosine(v, p), c, pid) for c, v in clusters.items() for pid, p in prints.items()), reverse=True)
    out: dict[int, tuple[int, float]] = {}
    used: set[int] = set()
    for score, c, pid in pairs:
        if score < threshold or c in out or pid in used:
            continue
        out[c] = (pid, round(score, 3))
        used.add(pid)
    return out


def looks_one_on_one(talk: dict[int, float], matches: dict[int, tuple[int, float]]) -> bool:
    """Brian's voice was found and Brian + one other cluster hold >= 95% of all talk time."""
    me = [c for c, (pid, _) in matches.items() if pid == ME]
    total = sum(talk.values())
    if not me or total <= 0 or len(talk) <= 2:
        return False
    others = sorted((s for c, s in talk.items() if c != me[0]), reverse=True)
    return (talk[me[0]] + others[0]) / total >= ONE_ON_ONE_SHARE


def merge_to_two(turns: list[dict], talk: dict[int, float], embs: dict[int, list[float]], me: int) -> list[dict]:
    """1:1 fast path: relabel every turn to Brian's cluster (`me`) or the other main speaker's (most talk time).

    A kept extra cluster with a voice embedding goes to whichever of the two it clearly sounds like; anything else
    (fragments, no embedding, no clear match) goes to the main speaker whose turn is nearest in time. The two main
    clusters keep their speakers.json numbers, so names and voiceprints stay on the same numbering."""
    other = max((c for c in talk if c != me), key=talk.get)
    main = (me, other)
    sounds_like = {}
    if all(m in embs for m in main):
        for c, v in embs.items():
            if c in main:
                continue
            scores = sorted(((cosine(v, embs[m]), m) for m in main), reverse=True)
            if scores[0][0] >= MERGE_MIN and scores[0][0] - scores[1][0] >= MERGE_MARGIN:  # a clear match only
                sounds_like[c] = scores[0][1]
    anchors = [t for t in turns if t["speaker"] in main]

    def nearest(t: dict) -> int:
        def gap(a: dict) -> float:
            return 0.0 if a["start"] <= t["end"] and t["start"] <= a["end"] else min(abs(a["start"] - t["end"]),
                                                                                     abs(t["start"] - a["end"]))
        return min(anchors, key=gap)["speaker"] if anchors else me

    def target(t: dict) -> int:
        if t["speaker"] in main:
            return t["speaker"]
        c = sounds_like.get(t["speaker"])
        return c if c is not None else nearest(t)  # cluster 0 is a real cluster: no truthiness tests

    return [{**t, "speaker": target(t)} for t in turns]


def enroll(conn: sqlite3.Connection, speaker_id: int, person_id: int | None = None, name: str | None = None,
           cfg: settings.Settings | None = None) -> int:
    """Name a speaker ("that's Scott"): link it to a person (created if needed) and add its voice to their voiceprint.
    Returns the person id. Naming once means the next recording recognises them."""
    sp = conn.execute("SELECT * FROM speaker WHERE id = ?", (speaker_id,)).fetchone()
    if sp is None:
        raise LookupError(f"no speaker {speaker_id}")
    emb = speaker_embedding(conn, speaker_id, cfg)
    with transaction(conn):
        if person_id is None:
            if not name:
                raise ValueError("give a person_id or a name")
            row = conn.execute("SELECT id FROM person WHERE lower(name) = lower(?)", (name,)).fetchone()
            person_id = row["id"] if row else conn.execute(
                "INSERT INTO person (name, created_at) VALUES (?, ?)", (name, now())).lastrowid
        conn.execute("UPDATE speaker SET person_id = ?, confirmed = 1 WHERE id = ?", (person_id, speaker_id))
        if emb is not None:
            vp = conn.execute("SELECT id, embedding, sample_count FROM voiceprint WHERE person_id = ?", (person_id,)).fetchone()
            if vp is None:
                conn.execute("INSERT INTO voiceprint (person_id, embedding, sample_count, updated_at) VALUES (?, ?, 1, ?)",
                             (person_id, to_blob(normalise(emb)), now()))
            else:  # running average, weighted by how many recordings already went into it
                old, n = from_blob(vp["embedding"]), vp["sample_count"]
                merged = normalise([(o * n + e) / (n + 1) for o, e in zip(old, emb)])
                conn.execute("UPDATE voiceprint SET embedding = ?, sample_count = ?, updated_at = ? WHERE id = ?",
                             (to_blob(merged), n + 1, now(), vp["id"]))
        conn.execute("UPDATE review_item SET status = 'done', resolved_at = ? WHERE kind = 'speaker' AND ref_id = ? "
                     "AND status = 'open'", (now(), speaker_id))
    return person_id


def unenroll(conn: sqlite3.Connection, speaker_id: int, person_id: int, cfg: settings.Settings | None = None) -> None:
    """ "This isn't X": take this speaker's voice back out of X's voiceprint (the reverse of enroll's running average),
    so a wrong naming doesn't teach Scribe Bot the wrong voice. A voiceprint made only from this voice is deleted."""
    emb = speaker_embedding(conn, speaker_id, cfg)
    vp = conn.execute("SELECT id, embedding, sample_count FROM voiceprint WHERE person_id = ?", (person_id,)).fetchone()
    if emb is None or vp is None:
        return
    n = vp["sample_count"]
    if n <= 1:
        conn.execute("DELETE FROM voiceprint WHERE id = ?", (vp["id"],))
        return
    old = from_blob(vp["embedding"])
    rest = normalise([(o * n - e) / (n - 1) for o, e in zip(old, normalise(emb))])
    conn.execute("UPDATE voiceprint SET embedding = ?, sample_count = ?, updated_at = ? WHERE id = ?",
                 (to_blob(rest), n - 1, now(), vp["id"]))


def embeddings_file(cfg: settings.Settings, conversation_id: int) -> Path:
    """Each conversation's per-cluster voice embeddings (numbers only), used when Brian names a speaker later.
    (A speaker.embedding column would be cleaner; proposed for a later migration.)"""
    return paths.clips(cfg) / str(conversation_id) / "embeddings.json"


def save_embeddings(cfg: settings.Settings, conversation_id: int, clusters: dict[int, list[float]]) -> None:
    paths.atomic_write(embeddings_file(cfg, conversation_id), json.dumps({str(c): v for c, v in clusters.items()}))


def speaker_embedding(conn: sqlite3.Connection, speaker_id: int, cfg: settings.Settings | None = None) -> list[float] | None:
    sp = conn.execute("SELECT conversation_id, cluster FROM speaker WHERE id = ?", (speaker_id,)).fetchone()
    f = embeddings_file(cfg or settings.load(), sp["conversation_id"])
    if not f.exists():
        return None
    return json.loads(f.read_text()).get(str(sp["cluster"]))
