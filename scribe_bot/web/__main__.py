"""`python -m scribe_bot.web`: serve the web app (launchd service com.scribebot.web).

Listens on 127.0.0.1 only until the M4-milestone firewall change (DECISIONS 2026-10-19: Tailscale address only).
"""
import os

import uvicorn

from scribe_bot.web.app import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host=os.environ.get("SCRIBE_BOT_HOST", "127.0.0.1"),
                port=int(os.environ.get("SCRIBE_BOT_PORT", "8000")), access_log=False)
