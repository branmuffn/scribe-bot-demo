import pytest

from pipeline_fakes import FakeEngine, drop_file
from scribe_bot.core import settings
from scribe_bot.pipeline import ingest, paths, speech


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setenv("SCRIBE_BOT_DATA", str(tmp_path / "data"))
    ingest._seen.clear()
    speech.set_engine(FakeEngine([], [], {}))
    yield settings.load()
    speech.set_engine(None)


def jobs(conn):
    return conn.execute("SELECT count(*) FROM job WHERE kind = 'recording'").fetchone()[0]


def test_first_scan_holds_existing_files_as_backlog(conn, cfg):
    inbound = paths.inbound(cfg)
    drop_file(inbound, "old one.m4a", b"a")
    drop_file(inbound, "old two.m4a", b"b")
    assert ingest.scan_inbound(conn, cfg) == []
    assert jobs(conn) == 0
    rows = conn.execute("SELECT status, vault_path FROM recording").fetchall()
    assert [r["status"] for r in rows] == ["backlog", "backlog"]
    assert all(r["vault_path"] is None for r in rows)

    drop_file(inbound, "new.m4a", b"c")  # arrives after the baseline: processed
    assert len(ingest.scan_inbound(conn, cfg)) == 1
    assert jobs(conn) == 1


def test_new_file_is_copied_to_vault_and_queued(conn, cfg):
    ingest.scan_inbound(conn, cfg)  # baseline on an empty folder
    src = drop_file(paths.inbound(cfg), "Team sync.m4a", b"hello")
    [rec_id] = ingest.scan_inbound(conn, cfg)
    rec = conn.execute("SELECT * FROM recording WHERE id = ?", (rec_id,)).fetchone()
    assert rec["title"] == "Team sync" and rec["status"] == "queued" and rec["private"] == 0
    assert rec["sha256"] == ingest.sha256(src)
    assert open(rec["vault_path"], "rb").read() == b"hello"
    src.unlink()  # phone deletion never affects the vault copy
    assert open(rec["vault_path"], "rb").read() == b"hello"


def test_duplicate_content_is_ignored(conn, cfg):
    ingest.scan_inbound(conn, cfg)
    drop_file(paths.inbound(cfg), "a.m4a", b"same")
    drop_file(paths.inbound(cfg), "copy of a.m4a", b"same")
    assert len(ingest.scan_inbound(conn, cfg)) == 1
    assert conn.execute("SELECT count(*) FROM recording").fetchone()[0] == 1


@pytest.mark.parametrize("title,private", [
    ("Private chat with Sam", 1), ("call_PRIVATE", 1), ("Privately noted", 1), ("Team sync", 0)])
def test_private_by_title(conn, cfg, title, private):
    ingest.scan_inbound(conn, cfg)
    drop_file(paths.inbound(cfg), f"{title}.m4a")
    [rec_id] = ingest.scan_inbound(conn, cfg)
    assert conn.execute("SELECT private FROM recording WHERE id = ?", (rec_id,)).fetchone()[0] == private


def test_skips_files_still_syncing_and_hidden_folders(conn, cfg):
    ingest.scan_inbound(conn, cfg)
    drop_file(paths.inbound(cfg), "fresh.m4a", b"x", age_s=0)
    drop_file(paths.inbound(cfg) / ".stversions", "old.m4a", b"y")
    drop_file(paths.inbound(cfg), "notes.txt", b"z")
    assert ingest.scan_inbound(conn, cfg) == []


def test_process_backlog_queues_chosen_ones(conn, cfg):
    drop_file(paths.inbound(cfg), "old.m4a", b"a")
    ingest.scan_inbound(conn, cfg)
    rec_id = conn.execute("SELECT id FROM recording").fetchone()[0]
    assert ingest.process_backlog(conn, [rec_id], cfg) == [rec_id]
    assert jobs(conn) == 1
    assert ingest.process_backlog(conn, [rec_id], cfg) == []  # not backlog any more
