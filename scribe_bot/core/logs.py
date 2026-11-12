"""Logging with redaction (CLAUDE.md rules 1 and 4). Anything that looks like a key, token, or webhook link,
and any secret value read this run, is replaced with [REDACTED] before a line is written.
Logs hold events and numbers only; never transcript text (callers' job; the redactor is the safety net for keys).
"""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),                                  # Anthropic
    re.compile(r"sk-[A-Za-z0-9_\-]{20,}"),                                      # OpenAI-style
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),                                     # Google
    re.compile(r"https://(?:canary\.|ptb\.)?discord(?:app)?\.com/api/webhooks/\S+"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{10,}"),
    re.compile(r"(?i)\b(x-api-key|api[_-]?key|authorization)(\"?\s*[:=]\s*\"?)[^\s\",}]+"),
]
REDACTED = "[REDACTED]"
_known: set[str] = set()


def register_secret(value: str) -> None:
    if len(value) >= 8:
        _known.add(value)


def redact(text: str) -> str:
    for v in _known:
        text = text.replace(v, REDACTED)
    for p in PATTERNS:
        text = p.sub(lambda m: (m.group(1) + m.group(2) + REDACTED) if p.groups == 2 else REDACTED, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def setup(log_dir: Path, level: int = logging.INFO) -> logging.Logger:
    """Root 'scribe_bot' logger writing to log_dir/scribe-bot.log (5 x 5 MB) with redaction on every handler."""
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("scribe_bot")
    logger.setLevel(level)
    if not logger.handlers:
        h = RotatingFileHandler(log_dir / "scribe-bot.log", maxBytes=5_000_000, backupCount=5)
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        h.addFilter(RedactingFilter())
        logger.addHandler(h)
    return logger
