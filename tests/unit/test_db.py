from pathlib import Path
import sqlite3

import pytest

from scribe_bot.core import db


def test_migrate_creates_tables_once(tmp_path):
    c = db.connect(tmp_path / "x.db")
    ids = sorted(int(p.name[:4]) for p in (Path(db.__file__).parent / "migrations").glob("*.sql"))
    assert db.migrate(c) == ids == list(range(1, len(ids) + 1))   # every migration, in order, none skipped
    assert db.migrate(c) == []  # re-running is a no-op
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"recording", "job", "stage_run", "schedule_run", "setting"} <= tables


def test_wal_and_foreign_keys_on(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO stage_run (job_id, stage, status) VALUES (999, 'x', 'done')")


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            conn.execute("INSERT INTO setting VALUES ('a', '1')")
            raise RuntimeError
    assert conn.execute("SELECT count(*) FROM setting WHERE key = 'a'").fetchone()[0] == 0


def test_failed_migration_leaves_nothing_behind(tmp_path, monkeypatch):
    mig = tmp_path / "migrations"
    mig.mkdir()
    (mig / "0001_ok.sql").write_text("CREATE TABLE a (x);")
    (mig / "0002_bad.sql").write_text("CREATE TABLE b (x); THIS IS NOT SQL;")
    monkeypatch.setattr(db, "MIGRATIONS_DIR", mig)
    c = db.connect(tmp_path / "x.db")
    with pytest.raises(sqlite3.Error):
        db.migrate(c)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert "a" in tables and "b" not in tables
    assert [r[0] for r in c.execute("SELECT version FROM schema_migrations")] == [1]


def test_web_connection_survives_a_thread_hop(tmp_path):
    """FastAPI can open a request's connection on one thread and run the page and close it on another. On 2026-10-20
    that crashed the conversation page now and then (HTMX then ignored the error, so the app looked frozen)."""
    import threading
    from types import SimpleNamespace
    from scribe_bot.web import routes
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cfg=SimpleNamespace(db_path=tmp_path / "w.db"))))
    gen, box = routes.get_conn(request), {}
    t = threading.Thread(target=lambda: box.update(conn=next(gen)))
    t.start(); t.join()
    assert box["conn"].execute("SELECT 1").fetchone()[0] == 1   # used on this (another) thread
    gen.close()                                                  # and closed here too: no ProgrammingError
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        box["conn"].execute("SELECT 1")
