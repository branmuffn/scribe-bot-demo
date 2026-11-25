"""The public web demo (Vercel): Keychain reads without a Keychain, and the made-up-meetings line."""
import pytest

from scribe_bot.core.secrets import KeychainStore, SecretMissing


def test_no_keychain_on_this_machine_reads_as_no_key():
    """Linux and the public demo on Vercel have no `security` command: Settings must show "No key", not crash."""
    def missing(*a, **k):
        raise FileNotFoundError("security")
    store = KeychainStore(run=missing)
    assert store.exists("anthropic") is False
    with pytest.raises(SecretMissing):
        store.get("anthropic")


def test_public_demo_line_shows_only_on_the_web_demo(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from scribe_bot import demo_data
    from scribe_bot.web import app as web
    assert demo_data.main(["x", str(tmp_path)]) == 0
    monkeypatch.setenv("SCRIBE_BOT_DATA", str(tmp_path))
    client = TestClient(web.create_app())
    assert "demo-note" not in client.get("/conversations").text
    monkeypatch.setitem(web.templates.env.globals, "public_demo", True)
    page = client.get("/conversations").text
    assert "Clickable demo with made-up meetings" in page and "#run-it-yourself" in page
    assert 'id="demo-welcome"' in page and "1-minute tour" in page   # D-12: the first-visit card (app.js shows it once)


# ---------- D-11 (Brian, 2026-11-25): "what happens if someone tries to?" Nothing runs, nothing leaves. ----------

@pytest.fixture
def demo(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from scribe_bot import demo_data
    from scribe_bot.core import db
    from scribe_bot.web import app as web
    assert demo_data.main(["x", str(tmp_path)]) == 0
    monkeypatch.setenv("SCRIBE_BOT_DATA", str(tmp_path))
    monkeypatch.setenv("SCRIBE_BOT_PUBLIC_DEMO", "1")
    monkeypatch.setitem(web.templates.env.globals, "public_demo", True)
    client = TestClient(web.create_app(), headers={"origin": "http://testserver"})
    from scribe_bot.core import settings
    client.db = lambda: db.connect(settings.load().db_path)
    return client


def jobs(client) -> int:
    with client.db() as conn:
        return conn.execute("SELECT count(*) FROM job").fetchone()[0]


def test_keychain_is_never_read_on_the_public_demo(monkeypatch):
    """A hard stop under every AI, Discord and backup call: even a Keychain that would answer is never asked."""
    monkeypatch.setenv("SCRIBE_BOT_PUBLIC_DEMO", "1")
    def would_answer(*a, **k):
        raise AssertionError("the Keychain was asked on the public demo")
    store = KeychainStore(run=would_answer)
    assert store.exists("anthropic") is False
    with pytest.raises(SecretMissing):
        store.get("anthropic")


@pytest.mark.parametrize("path, note", [
    ("/conversations/2/rerun", "demo-ai"), ("/conversations/2/detail", "demo-ai"), ("/conversations/2/compare", "demo-ai"),
])
def test_ai_buttons_end_in_a_note_and_queue_nothing(demo, path, note):
    before = jobs(demo)
    r = demo.post(path, data={"confirm": "1"}, headers={"referer": "http://testserver/conversations/2"}, follow_redirects=False)
    assert r.status_code == 303 and f"note={note}" in r.headers["location"]
    assert jobs(demo) == before
    page = demo.get(r.headers["location"]).text
    assert "Demo: the AI doesn&#39;t run here" in page or "Demo: the AI doesn't run here" in page


def test_confirm_step_says_nothing_is_sent(demo):
    assert "nothing is sent or charged" in demo.get("/conversations/2/rerun/confirm").text


def test_upload_saves_nothing(demo, tmp_path):
    r = demo.post("/capture/upload", files={"file": ("memo.m4a", b"0" * 2000, "audio/mp4")},
                  headers={"accept": "application/json"})
    assert r.json() == {"results": [{"name": "memo.m4a", "result": "demo"}]}
    assert not (tmp_path / "uploads").exists() or not any((tmp_path / "uploads").iterdir())
    r = demo.post("/capture/upload", files={"file": ("memo.m4a", b"0" * 2000, "audio/mp4")}, follow_redirects=False)
    assert "note=demo-upload" in r.headers["location"]


def test_keys_and_sends_end_in_a_note(demo):
    with demo.db() as conn:
        accounts = conn.execute("SELECT count(*) FROM account").fetchone()[0]
        sent = conn.execute("SELECT count(*) FROM notification").fetchone()[0]
    r = demo.post("/settings/accounts", data={"provider": "anthropic", "nickname": "x"}, follow_redirects=False)
    assert "note=demo-keys" in r.headers["location"]
    r = demo.post("/notifications/test", follow_redirects=False)
    assert "note=demo-send" in r.headers["location"]
    with demo.db() as conn:
        assert conn.execute("SELECT count(*) FROM account").fetchone()[0] == accounts
        assert conn.execute("SELECT count(*) FROM notification").fetchone()[0] == sent


def test_the_real_app_is_unchanged(tmp_path, monkeypatch):
    """Without SCRIBE_BOT_PUBLIC_DEMO (the M14), Summarize still queues the AI run."""
    from fastapi.testclient import TestClient
    from scribe_bot import demo_data
    from scribe_bot.web import app as web
    assert demo_data.main(["x", str(tmp_path)]) == 0
    monkeypatch.setenv("SCRIBE_BOT_DATA", str(tmp_path))
    monkeypatch.delenv("SCRIBE_BOT_PUBLIC_DEMO", raising=False)
    client = TestClient(web.create_app(), headers={"origin": "http://testserver"})
    r = client.post("/conversations/2/rerun", data={"confirm": "1"}, follow_redirects=False)
    assert "rerun=queued" in r.headers["location"]



def test_avatar_initials_skip_brackets():
    """F6 (demo pass, 2026-11-25): "Lena Fischer (demo)" showed "L(" in its circle on Today's Late promises."""
    from scribe_bot.web import app as web
    m = web.templates.env.get_template("macros.html").module
    assert ">LF<" in str(m.avatar("Lena Fischer (demo)"))
    assert ">J<" in str(m.avatar("Jordan (demo)"))
    assert ">?<" in str(m.avatar(None))


def test_demo_footer_says_made_up(demo):
    page = demo.get("/").text
    assert "Demo · made-up data" in page and "Runs on the M14" not in page
