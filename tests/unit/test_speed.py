"""Speed (Worker 2, 2026-11-26): every page on the made-up demo data stays well under Brian's bar, the busy pages don't
read the same thing over and over, and the reads every page makes use an index."""
import time

import pytest
from fastapi.testclient import TestClient

from scribe_bot import demo_data
from scribe_bot.core import db
from scribe_bot.core.settings import Settings
from scribe_bot.web.app import create_app
from test_web_pages import FakeKeys, fake_pmset

PAGES = ["/", "/inbox", "/review", "/tasks", "/tasks?tab=waiting", "/tasks?tab=inbox", "/tasks?tab=decisions",
         "/conversations", "/conversations/2", "/conversations?trash=1", "/people", "/people/1", "/people/2",
         "/search?q=budget", "/notifications", "/settings", "/status"]


@pytest.fixture(scope="module")
def demo(tmp_path_factory):
    d = tmp_path_factory.mktemp("demo")
    assert demo_data.main(["x", str(d)]) == 0
    mp = pytest.MonkeyPatch()
    mp.setenv("SCRIBE_BOT_DATA", str(d))
    cfg = Settings(d)
    client = TestClient(create_app(cfg, run=fake_pmset, secrets_store=FakeKeys()))
    client.cfg = cfg
    yield client
    mp.undo()


@pytest.mark.parametrize("path", PAGES)
def test_every_page_is_quick_on_the_demo_data(demo, path):
    """Brian's bar: about 150 ms on the M4-class machine. Best of three, so one slow moment on CI doesn't count."""
    demo.get(path)
    best = min(_timed(demo, path) for _ in range(3))
    assert best < 0.150, f"{path} took {best * 1000:.0f} ms"


def _timed(client, path):
    t = time.perf_counter()
    assert client.get(path).status_code == 200
    return time.perf_counter() - t


def _statements(client, path, monkeypatch) -> list[str]:
    log = []
    real = db.connect

    def tracing(*a, **k):
        c = real(*a, **k)
        c.set_trace_callback(log.append)
        return c

    monkeypatch.setattr(db, "connect", tracing)
    client.get(path)
    log.clear()
    client.get(path)
    monkeypatch.setattr(db, "connect", real)
    return log


@pytest.mark.parametrize("path, budget", [("/", 80), ("/inbox", 60), ("/review", 60)])
def test_busy_pages_read_the_step_times_once(demo, path, budget, monkeypatch):
    """F79: how long each M14 step takes was read again for every recording card and every job (7 reads each time):
    122 reads for Home, 99 for the Inbox, 104 for Review."""
    log = _statements(demo, path, monkeypatch)
    step_reads = [s for s in log if "FROM stage_run s JOIN job j" in s]
    assert len(step_reads) <= 7, len(step_reads)
    assert len([s for s in log if not s.startswith("-- ")]) <= budget


@pytest.mark.parametrize("sql", [
    "SELECT * FROM undo_action WHERE undone_at IS NULL AND created_at >= '2026-01-01' ORDER BY id DESC LIMIT 1",
    "SELECT id FROM conversation WHERE purge_at IS NOT NULL AND purge_at <= '2026-01-01'",
])
def test_reads_every_page_makes_use_an_index(demo, sql):
    """F80: the Undo bar's read and the Trash clean-up's read went through the whole table on every page."""
    conn = db.connect(demo.cfg.db_path)
    plan = " ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql))
    conn.close()
    assert "USING" in plan and "INDEX" in plan, plan
