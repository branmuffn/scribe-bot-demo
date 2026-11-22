"""Apple Voice Memos files (Brian, 2026-11-20). macOS / iOS Voice Memos keeps its transcript INSIDE the .m4a: a "tsrp"
box in moov/trak/udta holding JSON {"locale", "attributedString": {"runs": [text, attr#, ...], "attributeTable":
[{"timeRange": [start, end]}, ...]}} (one run per word or space, with its time). So a memo needs no copy-and-paste: the
Inbox reads the transcript straight from the file. The memo's file name is its date ("20261113 201200.m4a").

Only the box headers are read until the moov box (about 1 MB), so a 30 MB memo costs one small read. Read-only.
"""
from __future__ import annotations

import json
import re
import struct
from datetime import datetime
from functools import lru_cache
from pathlib import Path

CONTAINERS = {b"moov", b"trak", b"udta", b"mdia", b"minf"}
NAME_DATE = re.compile(r"\b(20\d{2})(\d{2})(\d{2})[ _](\d{2})(\d{2})(\d{2})\b")   # "20261113 201200"


def _boxes(data: bytes, off: int, end: int):
    while off + 8 <= end:
        size, kind = struct.unpack(">I4s", data[off:off + 8])
        hdr = 8
        if size == 1:
            size, hdr = struct.unpack(">Q", data[off + 8:off + 16])[0], 16
        elif size == 0:
            size = end - off
        if size < hdr:
            return
        yield kind, off + hdr, off + size
        off += size


def _moov(path: Path, cap: int = 16_000_000) -> bytes | None:
    with path.open("rb") as f:
        pos = 0
        while True:
            f.seek(pos)
            head = f.read(16)
            if len(head) < 8:
                return None
            size, kind = struct.unpack(">I4s", head[:8])
            hdr = 8
            if size == 1:
                size, hdr = struct.unpack(">Q", head[8:16])[0], 16
            if size < hdr:
                return None
            if kind == b"moov":
                f.seek(pos)
                return f.read(min(size, cap))[hdr:]
            pos += size


def _find(data: bytes, off: int, end: int, want: bytes):
    for kind, a, b in _boxes(data, off, end):
        if kind == want:
            return data[a:b]
        if kind in CONTAINERS:
            got = _find(data, a, b, want)
            if got is not None:
                return got
    return None


@lru_cache(maxsize=256)
def _cached(path: str, mtime: float, size: int) -> list[tuple[str, float]] | None:
    try:
        moov = _moov(Path(path))
        raw = _find(moov, 0, len(moov), b"tsrp") if moov else None
        if not raw:
            return None
        doc = json.loads(raw.decode("utf-8", errors="replace"))
        s = doc.get("attributedString") or {}
        if isinstance(s, list):   # short memos: [text, {"timeRange": [a, b]}, text, {...}, ...] (no table)
            runs, table = s, []
        else:
            runs, table = s.get("runs") or [], s.get("attributeTable") or []
        out = []
        for i in range(0, len(runs) - 1, 2):
            text, attr = runs[i], runs[i + 1]
            if isinstance(attr, dict):
                rng = attr.get("timeRange") or [0, 0]
            elif isinstance(attr, int) and 0 <= attr < len(table) and isinstance(table[attr], dict):
                rng = table[attr].get("timeRange") or [0, 0]
            else:
                rng = [0, 0]
            t = rng[0] if isinstance(rng, list) and rng else 0
            if isinstance(text, str):
                out.append((text, float(t or 0)))
        return out or None
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError, struct.error):
        return None


def words(path: Path) -> list[tuple[str, float]] | None:
    """[(text, start seconds), ...] from a Voice Memos .m4a, or None (not a memo, or no transcript yet)."""
    if path.suffix.lower() != ".m4a":
        return None
    try:
        st = path.stat()
    except OSError:
        return None
    return _cached(str(path), st.st_mtime, st.st_size)


def transcript(path: Path, para_s: float = 45.0) -> str | None:
    """The memo's transcript as plain text, a new paragraph at a sentence end roughly every `para_s` seconds."""
    w = words(path)
    if not w:
        return None
    paras, buf, start = [], [], 0.0
    for text, t in w:
        buf.append(text)
        if t - start >= para_s and text.rstrip().endswith((".", "?", "!")):
            paras.append("".join(buf).strip())
            buf, start = [], t
    if buf:
        paras.append("".join(buf).strip())
    return "\n\n".join(p for p in paras if p) or None


def name_date(name: str | None) -> datetime | None:
    """A Voice Memos file name like "20261113 201200" as a local date and time."""
    m = NAME_DATE.search(name or "")
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups())).astimezone()
    except ValueError:
        return None
