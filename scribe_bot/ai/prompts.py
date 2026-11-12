"""Prompts and output validation for meeting extraction (OUTPUT_DESIGN.md v1).

EXTRACT_V2 is the prompt Brian rated 28/28 on recording #3 (benchmarks/llm_bench.py); keep wording changes rare and
re-score the benchmark set after any change. v2.1 (2026-10-19) adds only a topic title; the v2 text is unchanged.
v2.2 (2026-10-20) adds only area + area_confidence (auto-file). Not re-scored, by Brian's DECISION: his older-recording
tests after the rev 2 deploy are the check; revert this if those pages are worse.
Glossary fixes are applied in code, never by the AI.
"""
from __future__ import annotations

import json
import re

EXTRACT_V2 = """You turn a meeting transcript into notes for {me}. Each line is "[number] Speaker: text".
Work through the whole transcript from first line to last. For every line, ask:
1. Does it settle or agree something? -> decisions
2. Does {me} say he will do something? -> tasks (owner is always {me})
3. Does someone else say they will do something for {me} or for the group? -> waiting_on (owner = that person)
4. Does it reveal a fact about a person or organization (job, school, location, history, role, plans)? -> people_facts
Rules:
- Who a fact is about: when a speaker says "I ...", the fact is about THAT speaker, not about {me}. Check the speaker name on the line.
- Owner: "I'll send it" said by Alex means owner Alex. Never move another person's commitment into {me}'s tasks.
- Skip things that were completed during the meeting (e.g. "I just emailed you the file").
- due: copy the time phrase exactly as said ("before November 15th", "later next week"), or null. Never invent a date.
- Be thorough: a real item with lower confidence is better than a missing item. A 30-minute 1:1 usually has several of each.
- summary: 3 to 6 sentences, naming who will do what.
- line: the transcript line number where it was said."""

PROMPT_VERSION = "v2.2"
TITLE_RULE = ("\n- title: 3 to 8 words saying what the conversation was about (its topic). No date, and no names unless "
              "the topic needs them.")
TITLE_MAX = 80
AREA_RULE = ('\n- area: "work" if this conversation is mainly about {me}\'s job, "personal" if it is about his personal life '
             "(family, friends, health, home, hobbies); area_confidence: 0 to 1, how sure you are.")

SPEAKER_NOTE = ("\nSpeaker labels come from automatic speaker separation and may be wrong or split one person "
                "into several labels; use context.\nOutput ONLY one JSON object with this shape, no prose:\n")

SCHEMA_TEXT = ('{"title": str, "area": "work"|"personal", "area_confidence": number, "summary": str,"decisions": [{"text": str, "line": int}], '
               '"tasks": [{"title": str, "owner": str, "due": str|null, "confidence": number, "line": int}], '
               '"waiting_on": [{"title": str, "owner": str, "due": str|null, "confidence": number, "line": int}], '
               '"people_facts": [{"person": str, "fact": str, "line": int}]}')

FACT_TYPES = ("birthday", "family", "event", "preference", "work", "other")

# F6: one template per meeting type, added after the v2 rules. 1:1 stays empty so the rated v2 prompt is unchanged.
# Brian can edit each in Settings → Summary templates (setting "template:<type>"); {me} becomes his name.
MEETING_TYPES = {"1:1": "1:1", "multi": "Group", "intro": "Intro", "personal": "Personal", "voice_note": "Voice note",
                 "learning": "Learning"}
TEMPLATES = {
    "1:1": "",
    "multi": ("This is a group meeting. summary: one sentence on the purpose, then one short paragraph per main topic, "
              "each starting with the topic name and a colon, separated by a blank line. Name who owns each next step."),
    "intro": ("This is a first meeting or introduction. summary: who the other person is (role, organization, background), "
              "how they connect to {me}, what each side is looking for, and the agreed next step. Put every fact about "
              "them in people_facts."),
    "personal": ("This is a personal conversation, not a work meeting. summary: warm, plain language about what was talked "
                 "about and any plans; no business words like \"action items\". tasks only for real promises. people_facts "
                 "matter most: family, birthdays, events, preferences."),
    "voice_note": ("This is a voice note: {me} talking to himself. summary: 1 to 3 sentences on what the note is about. "
                   "tasks: what {me} says he needs to do. decisions, waiting_on and people_facts: only if clearly stated, "
                   "usually empty."),
    "learning": ("This is a talk, class, sermon, training or podcast {me} is learning from, not a meeting. summary: one "
                 "sentence on the topic and who is teaching, then the key ideas as short paragraphs, each starting with "
                 "the idea's name and a colon, separated by a blank line; include examples or quotes worth remembering. "
                 "tasks: only things {me} himself says he will do, usually none; never turn advice to the audience into "
                 "tasks. decisions and waiting_on: usually empty. people_facts: only about people {me} knows personally."),
}


# The type wasn't picked by Brian: let the same AI call notice a talk/class and switch to the Learning rules
# (about 150 more input tokens, ~$0.0005; no second run). Brian's own pick is never overridden.
LEARNING_DETECT = ('\n- If this is clearly not a meeting but a talk, class, sermon, training or podcast {me} is listening to, '
                   'add "learning": true and follow these rules instead of the meeting type above: {rules}')


def extract_system(me: str = "Brian", meeting_type: str | None = None, template: str | None = None,
                   detect_learning: bool = False) -> str:
    """The v2 prompt, plus the meeting type's template when it has one (custom text from Settings, else the default)."""
    text = (template if template is not None else TEMPLATES.get(meeting_type or "", "")).strip()
    extra = f"\nMeeting type: {MEETING_TYPES.get(meeting_type, meeting_type)}. {text.replace('{me}', me)}" if text else ""
    if detect_learning and meeting_type != "learning":
        extra += LEARNING_DETECT.format(me=me, rules=TEMPLATES["learning"].split(", not a meeting. ", 1)[1].replace("{me}", me))
    return EXTRACT_V2.format(me=me) + extra + TITLE_RULE + AREA_RULE.format(me=me) + SPEAKER_NOTE + SCHEMA_TEXT


def apply_glossary(text: str, fixes: dict[str, str]) -> str:
    """Known mishearing -> correct term, case-insensitive."""
    for wrong, right in fixes.items():
        text = re.sub(re.escape(wrong), right, text, flags=re.IGNORECASE)
    return text


def render(lines: list[tuple[int, str, str]]) -> str:
    """(line number, speaker name, text) -> the "[n] Name: text" transcript the prompt expects."""
    return "\n".join(f"[{i}] {name}: {text}" for i, name, text in lines)


class InvalidOutput(ValueError):
    pass


def _items(data: dict, key: str, fields: dict[str, tuple]) -> list[dict]:
    rows = data.get(key)
    if not isinstance(rows, list):
        raise InvalidOutput(f"{key} is not a list")
    out = []
    for r in rows:
        if not isinstance(r, dict):
            raise InvalidOutput(f"{key} item is not an object")
        for f, types in fields.items():
            if f in r and r[f] is not None and not isinstance(r[f], types):
                raise InvalidOutput(f"{key}.{f} has the wrong type")
        required = [f for f in fields if f not in ("due", "confidence", "line")]
        if any(not r.get(f) for f in required):
            raise InvalidOutput(f"{key} item missing {required}")
        out.append(r)
    return out


def clean_title(value) -> str | None:
    """The AI's topic title, tidied; None if missing or unusable (the conversation keeps its fallback title)."""
    if not isinstance(value, str):
        return None
    title = re.sub(r"\s+", " ", value).strip().strip("\"'“”‘’").strip().rstrip(".")
    return title[:TITLE_MAX].rstrip() or None


def parse_extract(text: str) -> dict:
    """Pull the JSON object out of the reply and check its shape. Raises InvalidOutput."""
    try:
        data = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except ValueError as e:
        raise InvalidOutput("no JSON object in reply") from e
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str):
        raise InvalidOutput("summary missing")
    item = {"title": (str,), "owner": (str,), "due": (str,), "confidence": (int, float), "line": (int,)}
    area = data.get("area") if data.get("area") in ("work", "personal") else None
    conf = data.get("area_confidence")
    return {
        "title": clean_title(data.get("title")),
        "area": area,
        "area_confidence": float(conf) if area and isinstance(conf, (int, float)) and not isinstance(conf, bool) else None,
        "summary": data["summary"].strip(),
        "decisions": _items(data, "decisions", {"text": (str,), "line": (int,)}),
        "tasks": _items(data, "tasks", item),
        "waiting_on": _items(data, "waiting_on", item),
        "people_facts": _items(data, "people_facts", {"person": (str,), "fact": (str,), "line": (int,)}),
    }
