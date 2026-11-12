"""Folders the pipeline uses, all under the data folder (real data never goes into git)."""
from __future__ import annotations

import os
from pathlib import Path

from scribe_bot.core.settings import Settings

AUDIO_EXTS = {".m4a", ".mp3", ".wav", ".aac", ".opus", ".ogg", ".flac"}


def inbound(cfg: Settings) -> Path:
    """Syncthing's receive-only copy of the Pixel's Recordings folder."""
    return cfg.data_dir / "inbound"


def uploads(cfg: Settings) -> Path:
    """Audio uploaded or recorded in the web app (kept out of inbound/, which Syncthing owns)."""
    return cfg.data_dir / "uploads"


def vault(cfg: Settings) -> Path:
    """Scribe Bot's own copy of each original, so phone deletions never affect it."""
    return cfg.data_dir / "vault"


def archive(cfg: Settings) -> Path:
    """Opus copies kept forever (D8)."""
    return cfg.data_dir / "archive"


def export(cfg: Settings) -> Path:
    """Plain Markdown transcripts (A5)."""
    return cfg.data_dir / "export"


def clips(cfg: Settings) -> Path:
    """5-second samples of unknown speakers, for the review queue."""
    return cfg.data_dir / "clips"


def models() -> Path:
    return Path(os.environ.get("SCRIBE_BOT_MODELS", "~/scribe-bot-models")).expanduser()


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file and rename, so a crash never leaves half a file (stages must be safe to re-run)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)
