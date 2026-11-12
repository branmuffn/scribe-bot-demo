import pytest

from scribe_bot.core import db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.migrate(c)
    # Most tests exercise automatic processing; ask-first mode (the real default) has its own tests.
    c.execute("INSERT INTO setting VALUES ('processing_mode', 'auto')")
    yield c
    c.close()


@pytest.fixture(autouse=True)
def no_machine_commands(monkeypatch):
    """The worker's M14 health sampler never runs real macOS commands in tests."""
    from scribe_bot import worker

    def missing(args, **kw):
        raise FileNotFoundError(args[0])
    monkeypatch.setattr(worker, "MACHINE_RUN", missing)
    from scribe_bot.core import machine
    monkeypatch.setattr(machine, "cpu_ticks", lambda: None)   # nor reads the real CPU counters (see test_machine)
    monkeypatch.setattr(machine, "_LAST_TICKS", {})
