"""LiteLLM adapter, profiles, and Gemini/OpenAI accounts (DECISIONS 2026-10-19, F). Synthetic only: litellm.completion
and the Keychain are faked; no network."""
import os
import subprocess
from types import SimpleNamespace

import pytest

from scribe_bot.ai import accounts, prices, providers, router
from scribe_bot.core.secrets import KeychainStore

FAKE_KEY = "AIzaFAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0000"


def store(key=FAKE_KEY):
    calls = []

    def run(args, **kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=key + "\n", stderr="")

    return KeychainStore(run), calls


def response(text="ok", tokens_in=1000, tokens_out=200):
    msg = SimpleNamespace(content=text)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop")],
                           usage=SimpleNamespace(prompt_tokens=tokens_in, completion_tokens=tokens_out))


class StatusError(Exception):
    def __init__(self, msg, status_code):
        super().__init__(msg)
        self.status_code = status_code


class APIConnectionError(Exception):  # same name as LiteLLM's, which the adapter checks
    status_code = 500


# --- adapter ---

def test_litellm_adapter_passes_key_per_call_and_parses():
    seen = {}

    def completion(**kw):
        seen.update(kw)
        return response("hello", 1234, 56)

    s, calls = store()
    res = providers.LiteLLM("google", "scribe-bot.google", s, completion).call("gemini-3.8-flash", "sys", "user", 300)
    assert seen["model"] == "gemini/gemini-3.8-flash"
    assert seen["api_key"] == FAKE_KEY and seen["num_retries"] == 0 and seen["max_tokens"] == 300
    assert seen["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "user"}]
    assert calls[0][-3:] == ["-s", "scribe-bot.google", "-w"]
    assert (res.text, res.tokens_in, res.tokens_out, res.stop_reason, res.model) == ("hello", 1234, 56, "stop",
                                                                                     "gemini-3.8-flash")


@pytest.mark.parametrize("provider,prefix", [("anthropic", "anthropic"), ("openai", "openai"), ("google", "gemini")])
def test_litellm_prefixes(provider, prefix):
    seen = {}
    providers.LiteLLM(provider, f"scribe-bot.{provider}", store()[0],
                      lambda **kw: seen.update(kw) or response()).call("m", "s", "u")
    assert seen["model"] == f"{prefix}/m"


@pytest.mark.parametrize("exc,status,retryable", [
    (StatusError("rate limited", 429), 429, True),
    (StatusError("overloaded", 529), 529, True),
    (StatusError("bad request", 400), 400, False),
    (APIConnectionError("connection reset"), None, True),
])
def test_litellm_errors_map_to_provider_errors(exc, status, retryable):
    def completion(**kw):
        raise exc

    with pytest.raises(providers.ProviderError) as e:
        providers.LiteLLM("openai", "scribe-bot.openai", store()[0], completion).call("gpt-6-luna", "s", "u")
    assert e.value.status == status and e.value.retryable is retryable


def test_litellm_error_never_contains_key():
    def completion(**kw):
        raise StatusError(f"invalid api key {kw['api_key']}", 401)

    with pytest.raises(providers.ProviderError) as e:
        providers.LiteLLM("google", "scribe-bot.google", store()[0], completion).call("gemini-4-argon", "s", "u")
    assert FAKE_KEY not in str(e.value)


def test_unknown_provider_refused():
    with pytest.raises(providers.ProviderError):
        providers.LiteLLM("mistral", "scribe-bot.mistral", store()[0], lambda **kw: response())


def test_litellm_is_locked_down_on_import():
    lib = providers.load_litellm()
    assert lib is not None
    assert os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] == "True"  # bundled price map, no download
    assert lib.telemetry is False
    for name in ("callbacks", "success_callback", "failure_callback", "input_callback", "service_callback"):
        assert getattr(lib, name) == []


def test_make_uses_litellm_and_falls_back_to_direct_anthropic_without_it(monkeypatch):
    assert isinstance(providers.make("google", "scribe-bot.google"), providers.LiteLLM)
    monkeypatch.setattr(providers, "load_litellm", lambda: None)
    assert isinstance(providers.make("anthropic", "scribe-bot.anthropic"), providers.Anthropic)
    with pytest.raises(providers.ProviderError):
        providers.make("openai", "scribe-bot.openai")


# --- profiles ---

def test_every_profile_model_has_a_price():
    for p in router.PROFILES.values():
        for chain in p["chains"].values():
            for _, model in chain:
                assert model in prices.PRICES
    for model in router.PING_MODELS.values():
        assert model in prices.PRICES


def test_default_profile_is_claude_sonnet_for_extract(conn):
    assert router.profile(conn) == "all_claude"
    assert router.task_config(conn, "extract") == {"stage": 3, "chain": [["anthropic", "claude-sonnet-5-5"]]}


def test_profile_switch_and_per_task_override(conn):
    router.set_profile(conn, "budget_mix")
    assert [m for _, m in router.task_config(conn, "extract")["chain"]] == ["claude-haiku-5-5", "gemini-3.8-flash",
                                                                            "gpt-6-luna"]
    conn.execute("""INSERT INTO setting VALUES ('ai_task:extract', '{"chain": [["openai", "gpt-6.1-sol"]]}')""")
    assert router.task_config(conn, "extract")["chain"] == [["openai", "gpt-6.1-sol"]]
    with pytest.raises(ValueError):
        router.set_profile(conn, "all_local")


class Fake:
    def __init__(self, account, log):
        self.account, self.log = account, log

    def call(self, model, system, user, max_tokens=16000):
        self.log.append((self.account["provider"], model))
        if self.account["provider"] == "anthropic":
            raise providers.ProviderError("overloaded", 529)
        return providers.Result("ok", 100, 10, "stop", 0.1, model)


def test_quality_mix_falls_back_across_providers(conn):
    accounts.add_account(conn, "google", "Brian Gemini", type="paid")
    router.set_profile(conn, "quality_mix")
    log = []
    call = router.run(conn, "extract", "s", "u", sent_summary="synthetic", factory=lambda a: Fake(a, log))
    assert log == [("anthropic", "claude-sonnet-5-5"), ("google", "gemini-4-argon")]
    assert call.model == "gemini-4-argon" and call.cost_usd == prices.cost("gemini-4-argon", 100, 10)
    statuses = [r[0] for r in conn.execute("SELECT status FROM ledger_entry ORDER BY id")]
    assert statuses == ["failed", "ok"]


# --- accounts ---

def test_gemini_free_tier_is_may_train_and_blocked_for_real_data(conn):
    aid = accounts.add_account(conn, "google", "Gemini free", type="free")
    a = conn.execute("SELECT * FROM account WHERE id = ?", (aid,)).fetchone()
    assert a["may_train"] == 1 and "MAY TRAIN" in a["training_policy"] and a["secret_ref"] == "scribe-bot.google"
    router.set_profile(conn, "all_gemini")
    log = []
    with pytest.raises(router.Blocked) as e:
        router.run(conn, "extract", "s", "u", sent_summary="real", factory=lambda a: Fake(a, log))
    assert e.value.kind == "may_train" and log == []
    # Demo mode on synthetic data may use it
    router.run(conn, "extract", "s", "u", sent_summary="demo", real_data=False, factory=lambda a: Fake(a, log))
    assert log == [("google", "gemini-4-argon")]


def test_paid_tiers_are_not_may_train(conn):
    for provider in ("google", "openai"):
        aid = accounts.add_account(conn, provider, f"{provider} paid")
        assert conn.execute("SELECT may_train FROM account WHERE id = ?", (aid,)).fetchone()[0] == 0
    with pytest.raises(ValueError):
        accounts.add_account(conn, "openai", "x", type="free")


def test_test_key_uses_cheapest_model_per_provider(conn):
    aid = accounts.add_account(conn, "openai", "Brian OpenAI")
    seen = []

    class Ping:
        def call(self, model, system, user, max_tokens=16000):
            seen.append(model)
            return providers.Result("OK", 5, 1, "stop", 0.1, model)

    assert accounts.test_key(conn, aid, factory=lambda a: Ping())["ok"]
    assert seen == ["gpt-6-luna"]
