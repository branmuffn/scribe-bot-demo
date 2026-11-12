"""SQLite connection, transactions, and numbered migrations (scribe_bot/core/migrations/NNNN_name.sql)."""
from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path, any_thread: bool = False) -> sqlite3.Connection:
    """Open the database with WAL (safe across crashes) and foreign keys on. Transactions are explicit.

    any_thread: the web app's per-request connection. FastAPI may open it on one worker thread and run the page or
    close it on another; each request still uses its own connection one step at a time, so that's safe."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30, check_same_thread=not any_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """All-or-nothing block. IMMEDIATE takes the write lock up front, so two workers can't claim the same job."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def migrate(conn: sqlite3.Connection) -> list[int]:
    """Apply any migrations not yet applied, in order, each in its own transaction. Returns the versions applied."""
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for f in sorted(MIGRATIONS_DIR.glob("[0-9][0-9][0-9][0-9]_*.sql")):
        version = int(f.name[:4])
        if version in done:
            continue
        # executescript() can't run inside an open transaction, so BEGIN/COMMIT go in the script itself.
        try:
            conn.executescript(
                "BEGIN IMMEDIATE;\n" + f.read_text()
                + f"\nINSERT INTO schema_migrations VALUES ({version}, '{f.stem}', '{now()}');\nCOMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        applied.append(version)
        if "writable_schema" in f.read_text():  # a table definition was edited in place: make connections re-read it
            conn.execute(f"PRAGMA schema_version = {conn.execute('PRAGMA schema_version').fetchone()[0] + 1}")
    return applied
