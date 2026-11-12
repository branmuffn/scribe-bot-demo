"""Turn word timestamps + speaker turns into transcript lines (ported from benchmarks/speech_bench.py).

ONE numbering only: every line carries `cluster`, the number the speaker-separation step gave the speaker (the
same number stored in speaker.cluster and used for voiceprints). The M0 bug came from a second, "order of first
appearance" numbering being mixed with this one, which swapped names on 2-person calls; it no longer exists.
"""
from __future__ import annotations

MIN_SPEAKER_SECONDS = 8  # speakers with less total talk time are fragments; their words go to the nearest real speaker
SPLIT_AFTER_SECONDS = 12  # a line this long starts a new line at the next sentence end (F7, 2026-10-20: avg was ~34 s)


def talk_seconds(turns: list[dict]) -> dict[int, float]:
    talk: dict[int, float] = {}
    for t in turns:
        talk[t["speaker"]] = talk.get(t["speaker"], 0.0) + t["end"] - t["start"]
    return talk


def kept_clusters(turns: list[dict], min_seconds: float = MIN_SPEAKER_SECONDS) -> set[int]:
    return {c for c, s in talk_seconds(turns).items() if s >= min_seconds}


def speaker_lines(segments: list[dict], turns: list[dict], min_seconds: float = MIN_SPEAKER_SECONDS) -> list[dict]:
    """Assign each word to a speaker cluster by its timestamp; merge same-speaker runs (gaps < 8 s) into lines,
    splitting a run at a sentence end once its line is SPLIT_AFTER_SECONDS long."""
    keep = kept_clusters(turns, min_seconds)
    kept = [t for t in turns if t["speaker"] in keep]

    def who(at: float) -> int | None:
        best, best_gap = None, float("inf")
        for t in kept:
            gap = 0 if t["start"] <= at <= t["end"] else min(abs(at - t["start"]), abs(at - t["end"]))
            if gap < best_gap:
                best, best_gap = t["speaker"], gap
        return best

    words: list[dict] = []
    for seg in segments:
        for tok, at in zip(seg["tokens"], seg["token_times"]):
            if tok.startswith(" ") or not words or words[-1]["seg"] is not seg:
                words.append({"seg": seg, "start": at, "text": tok})
            else:
                words[-1]["text"] += tok
    lines: list[dict] = []
    for w in words:
        cluster = who(w["start"])
        if lines and lines[-1]["cluster"] == cluster and w["start"] - lines[-1]["last"] < 8 and not (
                w["start"] - lines[-1]["start"] >= SPLIT_AFTER_SECONDS and lines[-1]["text"].rstrip().endswith((".", "?", "!"))):
            lines[-1]["text"] += w["text"]
            lines[-1]["last"] = w["start"]
        else:
            lines.append({"start": w["start"], "last": w["start"], "cluster": cluster, "text": w["text"]})
    for line in lines:
        line["text"] = line["text"].strip()
    return [x for x in lines if x["text"]]
