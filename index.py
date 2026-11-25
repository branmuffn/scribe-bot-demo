"""Vercel entry point for the public demo (Vercel loads the `app` below; see docs/VERCEL.md).

Each Vercel instance builds its own Demo mode folder in /tmp (the only writable place there) from the made-up meetings in
scribe_bot/demo_data.py, then serves the normal web app on it. No API keys, no worker, no speech models: nothing is
transcribed, no AI is called and nothing costs money. Changes a visitor makes live only in that instance's /tmp copy and
are gone when Vercel recycles it.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

os.environ.setdefault("TZ", "America/New_York")   # dates on the pages read like the local app's, not UTC
if hasattr(time, "tzset"):
    time.tzset()
DATA = Path(os.environ.setdefault("SCRIBE_BOT_DATA", "/tmp/scribe-bot-demo-data"))
os.environ.setdefault("SCRIBE_BOT_PUBLIC_DEMO", "1")

from scribe_bot import demo_data  # noqa: E402
from scribe_bot.core.settings import DEMO_MARKER  # noqa: E402

if not (DATA / DEMO_MARKER).is_file():
    DATA.mkdir(parents=True, exist_ok=True)
    demo_data.main(["demo_data", str(DATA)])

from scribe_bot.web.app import create_app  # noqa: E402

app = create_app()   # the ASGI app Vercel serves
