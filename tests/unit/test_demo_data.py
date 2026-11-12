"""Demo mode seed (scribe_bot/demo_data.py): made-up meetings only, safe to re-run, never mixes with real data."""
import pytest

from scribe_bot import demo_data


def test_seed_creates_only_demo_conversations(conn):
    n = demo_data.seed(conn)
    titles = [r[0] for r in conn.execute("SELECT title FROM conversation")]
    assert n == len(titles) == 6
    assert all(t.startswith("DEMO:") for t in titles)
    people = [r[0] for r in conn.execute("SELECT name FROM person WHERE id != 1")]
    assert people and all(p.endswith("(demo)") for p in people)


def test_seed_is_idempotent(conn):
    assert demo_data.seed(conn) == demo_data.seed(conn) == 6


def test_private_demo_meeting_never_sent(conn):
    demo_data.seed(conn)
    cid = conn.execute("SELECT id FROM conversation WHERE private = 1").fetchone()[0]
    assert not conn.execute("SELECT 1 FROM ledger_entry WHERE conversation_id = ?", (cid,)).fetchone()


def test_refuses_a_database_with_real_conversations(conn):
    conn.execute("INSERT INTO conversation (title, status, created_at, updated_at) VALUES ('Real meeting', 'done', 'x', 'x')")
    with pytest.raises(demo_data.NotADemoDatabase):
        demo_data.seed(conn)


def test_cli_seeds_a_folder(tmp_path, capsys):
    assert demo_data.main(["demo_data", str(tmp_path / "demo")]) == 0
    assert "6 made-up conversations" in capsys.readouterr().out
