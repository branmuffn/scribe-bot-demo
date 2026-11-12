import logging
import subprocess

import pytest

from scribe_bot.core import logs, secrets

# Synthetic, obviously fake values (real keys never appear in tests).
FAKE_KEY = "sk-ant-FAKEFAKEFAKEFAKEFAKE1234"


def fake_security(stdout="", returncode=0):
    calls = []

    def run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, returncode, stdout=stdout, stderr="secret-ish stderr")

    return run, calls


def test_get_reads_keychain_and_registers_for_redaction():
    run, calls = fake_security(FAKE_KEY + "\n")
    assert secrets.KeychainStore(run).get("anthropic") == FAKE_KEY
    assert calls[0] == ["security", "find-generic-password", "-a", "brian-personal", "-s", "scribe-bot.anthropic", "-w"]
    assert logs.redact(f"oops {FAKE_KEY[7:]}") == "oops " + FAKE_KEY[7:]  # partial value isn't a match
    assert FAKE_KEY not in logs.redact(f"oops {FAKE_KEY}")


def test_missing_secret_raises_without_details():
    run, _ = fake_security(returncode=44)
    with pytest.raises(secrets.SecretMissing) as e:
        secrets.KeychainStore(run).get("anthropic")
    assert "stderr" not in str(e.value)


def test_exists():
    assert secrets.KeychainStore(fake_security()[0]).exists("discord")
    assert not secrets.KeychainStore(fake_security(returncode=44)[0]).exists("discord")


def test_mask():
    assert secrets.mask(FAKE_KEY) == "sk-••••1234"
    assert secrets.mask("short") == "••••"


@pytest.mark.parametrize("line", [
    "key=sk-ant-api03-abcdefghijklmnop",
    "using sk-proj-abcdefghijklmnopqrstuvwx",
    "AIzaSyA-abcdefghijklmnopqrstuvwxyz12345",
    "posting to https://discord.com/api/webhooks/123/abcDEF",
    "Authorization: Bearer abcdefghijklmnop",
    '{"x-api-key": "whatever-it-is"}',
])
def test_redact_patterns(line):
    out = logs.redact(line)
    assert "[REDACTED]" in out
    for secret in ("abcdefghijklmnop", "abcDEF", "whatever-it-is"):
        assert secret not in out


def test_log_file_is_redacted(tmp_path):
    logger = logs.setup(tmp_path)
    try:
        logger.info("calling with %s", "sk-ant-api03-zzzzzzzzzzzzzzzz")
        try:
            raise RuntimeError("failed with key sk-ant-api03-yyyyyyyyyyyyyyyy")
        except RuntimeError:
            logger.exception("boom")
        for h in logger.handlers:
            h.flush()
        text = (tmp_path / "scribe-bot.log").read_text()
        assert "zzzz" not in text and "yyyy" not in text
        assert "calling with [REDACTED]" in text and "RuntimeError: failed with key [REDACTED]" in text
    finally:
        for h in list(logger.handlers):
            logger.removeHandler(h)
            h.close()
