"""Page-speed budget: every main page renders in under 1 s on the M14 with a year or two of data (synthetic only).

Tests run on the M4, roughly 4x faster than the 2014 M14, so the budget here is 1 s / M14_SLOWER. GitHub's CI runners
are slower and noisier than the M4, so there the budget is doubled: it still catches a big slowdown without failing on
a few milliseconds of jitter. benchmarks/page_speed.py measures the real thing on the M14.
"""
import os
import random
import time
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from scribe_bot.core import db
from scribe_bot.core.settings import Settings
from scribe_bot.web.app import create_app
from test_web_pages import FakeKeys, fake_pmset

M14_SLOWER = 4
BUDGET_S = 1.0 / M14_SLOWER * (2 if os.environ.get("CI") else 1)
CONVERSATIONS, LINES, PEOPLE, TASKS_EACH = 300, 900, 400, 10   # ~a year of daily meetings, hour-long transcripts
FIRST = "Alex Sam Jordan Pat Lee Kim Robin Casey Morgan Taylor Jamie Drew Avery Riley Quinn Reese Sky Rowan Emery Hayden".split()
LAST = "Example Sample Test Demo Fixture Mock Stub Dummy Placeholder Model".split()
WORDS = "launch budget plan review deck team design timeline vendor contract hiring roadmap metrics customer quarter".split()


def seed_big(conn) -> None:
    rnd = random.Random(7)
    t = db.now()
    say = lambda n: " ".join(rnd.choice(WORDS) for _ in range(n))  # noqa: E731
    conn.executemany("INSERT INTO person (id, name, area, created_at, last_spoken_at) VALUES (?, ?, 'work', ?, ?)",
                     [(i, f"{FIRST[i % len(FIRST)]} {LAST[i // len(FIRST) % len(LAST)]} {i}", t, t)   # varied, like real names
                      for i in range(2, PEOPLE + 2)])   # 1 = Brian, made by the migration
    for c in range(1, CONVERSATIONS + 1):
        conn.execute("INSERT INTO recording (id, source, original_path, sha256, received_at, status) "
                     "VALUES (?, 'recorder', ?, ?, ?, 'done')", (c, f"/synthetic/{c}.m4a", f"sha{c}", t))
        day = (datetime.now(timezone.utc) - timedelta(days=c - 1)).isoformat(timespec="seconds")   # one a day, going back
        conn.execute("INSERT INTO conversation (id, recording_id, title, started_at, duration_s, area, summary, status, "
                     "created_at, updated_at) VALUES (?, ?, ?, ?, 3600, 'work', ?, 'done', ?, ?)",
                     (c, c, f"Synthetic meeting {c}", day, say(80), t, t))
        p1, p2 = rnd.randrange(2, PEOPLE + 2), rnd.randrange(2, PEOPLE + 2)
        conn.executemany("INSERT INTO speaker (conversation_id, cluster, person_id, talk_seconds) VALUES (?, ?, ?, 1200)",
                         [(c, 0, 1), (c, 1, p1), (c, 2, p2)])
        sp = [r[0] for r in conn.execute("SELECT id FROM speaker WHERE conversation_id = ?", (c,))]
        seg = [(c, i, i * 4.0, i * 4.0 + 4, sp[i % 3], say(14) + (" school" if i % 100 == 7 else "")) for i in range(LINES)]
        conn.executemany("INSERT INTO segment (conversation_id, idx, start_s, end_s, speaker_id, text) VALUES (?, ?, ?, ?, ?, ?)", seg)
        conn.execute("INSERT INTO search_index (kind, ref_id, conversation_id, text) "
                     "SELECT 'segment', id, conversation_id, text FROM segment WHERE conversation_id = ?", (c,))
        conn.executemany("INSERT INTO task (conversation_id, title, owner_person_id, status, area, created_at, updated_at) "
                         "VALUES (?, ?, ?, ?, 'work', ?, ?)",
                         [(c, say(6), rnd.choice([1, p1, p2]), rnd.choice(["open", "done"]), t, t) for _ in range(TASKS_EACH)])
        conn.executemany("INSERT INTO decision (conversation_id, text, created_at) VALUES (?, ?, ?)", [(c, say(8), t)] * 2)
        conn.executemany("INSERT INTO person_fact (person_id, conversation_id, value, created_at) VALUES (?, ?, ?, ?)",
                         [(p1, c, say(6), t), (p2, c, say(6), t)])


@pytest.fixture(scope="module")
def big(tmp_path_factory):
    cfg = Settings(tmp_path_factory.mktemp("big"))
    conn = db.connect(cfg.db_path)
    db.migrate(conn)
    with db.transaction(conn):
        seed_big(conn)
    client = TestClient(create_app(cfg, run=fake_pmset, secrets_store=FakeKeys()))
    yield client
    conn.close()


PAGES = ["/", "/conversations", "/conversations/150", "/tasks", "/tasks?tab=waiting", "/tasks?show=done", "/decisions",
         "/people", "/people/42", "/review", "/review?tab=done", "/search?q=roadmap", "/search?q=school", "/search?q=school&sort=new&said=1",
         "/search?q=school&conv=7", "/settings", "/costs", "/help",
         "/status", "/conversations?trash=1", "/speaking", "/people/1"]


@pytest.mark.parametrize("path", PAGES)
def test_page_under_budget(big, path):
    big.get(path)                                   # warm up (templates compile, SQLite cache)
    best = min(_timed(big, path) for _ in range(3))
    assert best < BUDGET_S, f"{path} took {best * 1000:.0f} ms (budget {BUDGET_S * 1000:.0f} ms here = 1 s on the M14)"


def _timed(client, path) -> float:
    start = time.perf_counter()
    r = client.get(path)
    elapsed = time.perf_counter() - start
    assert r.status_code == 200, path
    return elapsed



def test_long_lists_show_more_instead_of_stopping(big):
    """No list silently stops: past a page, "Show more" loads the next step in place (300 conversations here)."""
    assert "Show more · 250 left" in big.get("/conversations").text
    assert "Show more · 200 left" in big.get("/conversations?n=100").text
    assert "Show more" not in big.get("/conversations?n=300").text
    assert "Show more · " in big.get("/tasks?show=done").text
