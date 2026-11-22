"""Inbox (Brian, 2026-11-20): recordings waiting to be processed get their own page instead of filling Today.

Plain code, no AI and nothing sent anywhere:
- A Google Recorder transcript (.txt, "Share › Transcript") that synced next to a recording is matched to it by name.
- From it, a quick title (the most-used words) and a preview (the first lines), so Brian can choose what to process.
- The whole transcript can be read on its own page, unedited.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from scribe_bot.pipeline import voicememo

LONG_S, SHORT_S = 20 * 60, 5 * 60
SORTS = {"newest": "Newest", "oldest": "Oldest", "longest": "Longest", "shortest": "Shortest"}
FILTERS = {"all": "All", "transcript": "Has transcript", "long": "Long (20+ min)", "short": "Short (under 5 min)",
           "older": "Older"}
SOURCES = {"recorder": "phone", "share": "shared", "upload": "upload", "app": "recorded here"}
STOP = set("""a about above after again against all also am an and any are aren't as at be because been before being
below between both but by can can't cannot could couldn't did didn't do does doesn't doing don't down during each few
for from further get got gonna had hadn't has hasn't have haven't having he he'd he'll he's her here here's hers herself
him himself his how how's i i'd i'll i'm i've if in into is isn't it it's its itself just know let's like little lot
make me mean more most much mustn't my myself need no nor not now of off oh okay ok on once one only or other ought our
ours ourselves out over own really right said same say see she she'd she'll she's should shouldn't so some something
such sure than that that's the their theirs them themselves then there there's these they they'd they'll they're
they've thing things think this those through to too um uh under until up us very want was wasn't way we we'd we'll
we're we've well were weren't what what's when when's where where's which while who who's whom why why's will with
won't would wouldn't yeah yes you you'd you'll you're you've your yours yourself yourselves going go kind actually
maybe probably also still even back good great thank thanks guess gotta stuff anything everything
two three four five first second last next new old people person someone somebody everyone anyone nothing time times
day days week year years today tomorrow yesterday come came look looking looked feel felt take took give gave put
tell told talk talking talked ask asked work working use used try trying tried start started keep kept lot lots bit
pretty basically literally definitely totally exactly whatever though although around another every many few able
wanna kinda sorta alright cool wow hmm hey mhm guys guy'll can't'll""".split())   # 2026-11-21: "Two, people, first"
SPEAKER = re.compile(r"^\[(.{1,40})\]$")       # Recorder writes "[Speaker 1]" on its own line before each turn


def norm(name: str) -> str:
    """'Intro Meeting _ Practicum - Oct 14 at 11_30' and '... - Oct 14 at 11-30' compare equal."""
    return " ".join(re.findall(r"[a-z0-9]+", name.lower()))


def transcript_index(folders: list[Path]) -> dict[str, Path]:
    """Every .txt in the synced folders, by normalized file name (looked up once per page)."""
    out: dict[str, Path] = {}
    for f in folders:
        try:
            for p in f.glob("*.txt"):
                out.setdefault(norm(p.stem), p)
        except OSError:
            continue
    return out


def find(index: dict[str, Path], title: str | None, original_path: str | None) -> Path | None:
    for name in (Path(original_path).stem if original_path else None, title):
        if name and norm(name) in index:
            return index[norm(name)]
    return None


def parse(text: str) -> list[dict]:
    """Recorder's export as turns: [{'speaker': 'Speaker 1' or '', 'text': ...}]. Plain text without labels becomes
    one turn per paragraph. Nothing is changed or removed except blank lines."""
    turns: list[dict] = []
    speaker, buf = "", []

    def flush():
        if buf:
            turns.append({"speaker": speaker, "text": " ".join(buf)})
            buf.clear()
    for line in text.splitlines():
        s = line.strip()
        m = SPEAKER.match(s)
        if m:
            flush()
            speaker = m.group(1)
        elif not s:
            flush()
        else:
            buf.append(s)
    flush()
    return turns


def read(path: Path, limit: int = 2_000_000) -> str:
    """A transcript file, or the transcript inside an Apple Voice Memos .m4a."""
    if path.suffix.lower() != ".txt":
        return (voicememo.transcript(path) or "")[:limit]
    return path.read_text(encoding="utf-8", errors="replace")[:limit]


def quick_title(text: str, n: int = 3, min_count: int = 2) -> str:
    """The most-used meaningful words, e.g. "Venue, budget, kickoff". Empty when there's too little to go on."""
    body = "\n".join(line for line in text.splitlines() if not SPEAKER.match(line.strip()))   # not the "[Speaker 1]" labels
    words = [w for w in re.findall(r"[a-zA-Z][a-zA-Z'\-]{2,}", body) if w.lower() not in STOP]
    counts = Counter(w.lower() for w in words)
    top = [w for w, c in counts.most_common(n * 3) if c >= min_count][:n]
    if not top:
        return ""
    return ", ".join(top).capitalize()


def preview(turns: list[dict], chars: int = 170) -> str:
    """The first words people said: skips Recorder's title line (the text before the first "[Speaker N]")."""
    if any(t["speaker"] for t in turns):
        turns = turns[next(i for i, t in enumerate(turns) if t["speaker"]):]
    text = " ".join(t["text"] for t in turns)
    return text if len(text) <= chars else text[:chars].rsplit(" ", 1)[0] + "…"


def matches(item: dict, show: str) -> bool:
    d = item["duration_s"] or 0
    return {"all": True, "transcript": item["transcript"] is not None, "long": d >= LONG_S, "short": d < SHORT_S,
            "older": item["older"]}.get(show, True)


# FX-A (Brian, 2026-11-20): one Filter button opens dropdowns, and several filters can be on at once.
# name -> (label, {value: option label}); "" is always "Any".
FX = {"has": ("Transcript", {"transcript": "Has transcript", "none": "No transcript yet"}),
      "length": ("Length", {"long": "Long (20+ min)", "short": "Short (under 5 min)"}),
      "kind": ("Status", {"waiting": "Waiting for your OK", "older": "Older"}),
      "source": ("Source", {"phone": "Phone", "Voice Memos": "Voice Memos", "upload": "Uploaded", "shared": "Shared"})}
OLD_SHOW = {"transcript": ("has", "transcript"), "long": ("length", "long"), "short": ("length", "short"), "older": ("kind", "older")}


def fx_match(item: dict, name: str, value: str) -> bool:
    d = item["duration_s"] or 0
    if name == "has":
        return (item["transcript"] is not None) == (value == "transcript")
    if name == "length":
        return d >= LONG_S if value == "long" else d < SHORT_S
    if name == "kind":
        return item["older"] == (value == "older")
    if name == "source":
        return item.get("source") == value
    return True


def fx_filter(items: list[dict], chosen: dict[str, str]) -> list[dict]:
    return [x for x in items if all(fx_match(x, k, v) for k, v in chosen.items())]


def sort(items: list[dict], how: str) -> list[dict]:
    if how == "oldest":
        return sorted(items, key=lambda x: x["when"])
    if how in ("longest", "shortest"):
        return sorted(items, key=lambda x: x["duration_s"] or 0, reverse=how == "longest")
    return sorted(items, key=lambda x: x["when"], reverse=True)
