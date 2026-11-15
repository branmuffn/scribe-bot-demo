"""Where Scribe Bot keeps its data. Real data lives on the M14 under ~/scribe-bot-data (never in git)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEMO_MARKER = "DEMO_MODE"   # scribe_bot.demo_data writes this file into a demo data folder


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

    @property
    def demo(self) -> bool:
        """True for a Demo mode folder (made-up meetings, no worker): the app hides worker warnings there."""
        return (self.data_dir / DEMO_MARKER).is_file()


def load() -> Settings:
    """SCRIBE_BOT_DATA overrides the data folder (tests point it at a temp folder)."""
    return Settings(data_dir=Path(os.environ.get("SCRIBE_BOT_DATA", "~/scribe-bot-data")).expanduser())
