"""`python -m scribe_bot.health`: print health as JSON; exit 1 if unhealthy (used by scripts/deploy.sh)."""
import json
import sys

from scribe_bot.core import health, settings


def main() -> int:
    result = health.check(settings.load())
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
