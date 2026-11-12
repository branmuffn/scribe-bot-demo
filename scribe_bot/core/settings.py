"""Where Scribe Bot keeps its data. Real data lives on the M14 under ~/scribe-bot-data (never in git)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    data_dir: Path

    @property
    def db_path(self) -> Path:
        return self.data_dir / "scribe-bot.db"

    @property
    def jobs_dir(self) -> Path:
        """Each job's stage outputs are saved under jobs_dir/<job id>/."""
        return self.data_dir / "jobs"


def load() -> Settings:
    """SCRIBE_BOT_DATA overrides the data folder (tests point it at a temp folder)."""
    return Settings(data_dir=Path(os.environ.get("SCRIBE_BOT_DATA", "~/scribe-bot-data")).expanduser())
