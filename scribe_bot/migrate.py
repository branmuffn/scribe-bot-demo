"""`python -m scribe_bot.migrate`: apply pending database migrations (used by scripts/deploy.sh)."""
import json
import sys

from scribe_bot.core import db, settings


def main() -> int:
    conn = db.connect(settings.load().db_path)
    try:
        print(json.dumps({"applied": db.migrate(conn)}))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
