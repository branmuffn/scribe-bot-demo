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
    assert "Demo with made-up meetings" in client.get("/conversations").text
