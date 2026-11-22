import urllib.error
from datetime import datetime
from zoneinfo import ZoneInfo

from scribe_bot.discord_bot import sender
from scribe_bot.discord_bot.notify import enqueue

TZ = ZoneInfo("America/New_York")
NOON = datetime(2026, 10, 20, 12, 0, tzinfo=TZ)
NIGHT = datetime(2026, 10, 20, 23, 30, tzinfo=TZ)
FAKE_URL = "https://discord.example/webhook/fake"


class FakeDiscord:
    def __init__(self, fail=0):
        self.posts, self.fail = [], fail

    def __call__(self, url, text):
        if self.fail:
            self.fail -= 1
            raise urllib.error.URLError("offline")
        self.posts.append((url, text))


def one_row(conn, sql):
    return conn.execute(sql).fetchone()[0]


def statuses(conn):
    return [r[0] for r in conn.execute("SELECT status FROM notification ORDER BY id")]


def test_batches_alerts_into_one_message(conn):
    for i in range(3):
        enqueue(conn, "alerts", f"Alert {i}", f"http://m14/c/{i}")
    d = FakeDiscord()
    assert sender.send_pending(conn, NOON, d, lambda ch: FAKE_URL)["sent"] == 3
    assert len(d.posts) == 1
    assert d.posts[0][1].splitlines() == [f"Alert {i} <http://m14/c/{i}>" for i in range(3)]
    assert statuses(conn) == ["sent"] * 3
    assert sender.send_pending(conn, NOON, d, lambda ch: FAKE_URL)["sent"] == 0  # nothing re-sent


def test_app_only_rows_are_never_posted(conn):
    """Brian 2026-10-22: Processing started and briefings (discord=False) stay in the app; alerts, "waiting for your OK"
    and Ready reach Discord."""
    enqueue(conn, "bot", "Processing started · 3.0 min recording", discord=False)
    enqueue(conn, "bot", "New recording waiting for your OK · 3.0 min", "/review")
    enqueue(conn, "alerts", "M14 on battery (40%)")
    d = FakeDiscord()
    r = sender.send_pending(conn, NOON, d, lambda ch: FAKE_URL)
    assert r["sent"] == 2 and sorted(text.split(" <")[0] for _, text in d.posts) == \
        ["M14 on battery (40%)", "New recording waiting for your OK · 3.0 min"]
    assert statuses(conn) == ["sent", "sent", "sent"]
    assert one_row(conn, "SELECT count(*) FROM notification WHERE sent_at IS NOT NULL") == 2   # the app-only one never posted


def test_pipeline_sends_only_the_waiting_ping_to_discord():
    import inspect
    from scribe_bot.pipeline import ingest, recording
    # Brian 2026-11-21: no "waiting for your OK" and no "Processing started"; "Ready" is the one recording ping
    assert "notify.enqueue" not in inspect.getsource(ingest)
    src = inspect.getsource(recording)
    assert "return   # Brian, 2026-11-21: no \"Processing started\" note" in src


def test_quiet_hours_hold_bot_but_not_alerts(conn):
    enqueue(conn, "bot", "Ready: conversation 1")
    enqueue(conn, "alerts", "M14 on battery (40%)")
    d = FakeDiscord()
    r = sender.send_pending(conn, NIGHT, d, lambda ch: FAKE_URL)
    assert r["held"] == 1 and r["sent"] == 1
    assert statuses(conn) == ["held", "sent"]
    morning = datetime(2026, 10, 21, 7, 0, tzinfo=TZ)
    assert sender.send_pending(conn, morning, d, lambda ch: FAKE_URL)["sent"] == 1  # held row delivered after 7 AM
    assert statuses(conn) == ["sent", "sent"]


def test_unreachable_discord_retries_later(conn):
    enqueue(conn, "alerts", "Disk 86% full")
    d = FakeDiscord(fail=1)
    assert sender.send_pending(conn, NOON, d, lambda ch: FAKE_URL)["waiting"] == 1
    assert statuses(conn) == ["queued"]
    assert sender.send_pending(conn, NOON, d, lambda ch: FAKE_URL)["sent"] == 1


def test_content_guard_blocks_long_or_multiline_titles(conn):
    enqueue(conn, "alerts", "x" * (sender.TITLE_MAX + 1))
    enqueue(conn, "alerts", "line one\nline two")
    enqueue(conn, "alerts", "Disk 86% full")
    d = FakeDiscord()
    r = sender.send_pending(conn, NOON, d, lambda ch: FAKE_URL)
    assert r == {"sent": 1, "held": 0, "blocked": 2, "waiting": 0}
    assert all("x" * 20 not in text and "line two" not in text for _, text in d.posts)
    assert statuses(conn) == ["held", "held", "sent"]


def test_no_webhook_leaves_rows_queued(conn):
    enqueue(conn, "alerts", "Disk 86% full")
    assert sender.send_pending(conn, NOON, FakeDiscord(), lambda ch: None)["waiting"] == 1
    assert statuses(conn) == ["queued"]


def test_splits_at_discord_size_limit(conn):
    for i in range(30):
        enqueue(conn, "alerts", f"Alert {i:03d} " + "y" * 100)
    d = FakeDiscord()
    sender.send_pending(conn, NOON, d, lambda ch: FAKE_URL)
    assert len(d.posts) >= 2 and all(len(t) <= sender.MESSAGE_MAX for _, t in d.posts)
    assert statuses(conn) == ["sent"] * 30


def test_keychain_webhook_falls_back_to_shared_webhook(monkeypatch):
    seen = []

    class Store:
        def get(self, service):
            seen.append(service)
            if service == "discord":
                return FAKE_URL
            raise sender.secrets.SecretMissing(service)

    monkeypatch.setattr(sender.secrets, "KeychainStore", Store)
    assert sender.keychain_webhook("bot") == FAKE_URL
    assert seen == ["discord-bot", "discord"]


def test_app_paths_become_links_discord_can_open(conn):
    conn.execute("INSERT INTO setting (key, value) VALUES ('web_base_url', 'https://m14.example/')")
    enqueue(conn, "alerts", "Backup failed", "/settings/backups")
    enqueue(conn, "alerts", "Status", "https://m14.example/")
    d = FakeDiscord()
    sender.send_pending(conn, NOON, d, lambda ch: FAKE_URL)
    assert d.posts[0][1].splitlines() == ["Backup failed <https://m14.example/settings/backups>",
                                          "Status <https://m14.example/>"]


def test_app_paths_without_the_app_address_send_the_title_only(conn):
    enqueue(conn, "alerts", "Backup failed", "/settings/backups")
    d = FakeDiscord()
    sender.send_pending(conn, NOON, d, lambda ch: FAKE_URL)
    assert d.posts[0][1] == "Backup failed"        # not a dead "</settings/backups>"
