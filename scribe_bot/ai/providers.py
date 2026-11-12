"""Provider adapters (DECISIONS 2026-10-19, F): every provider goes through LiteLLM; Anthropic, Google Gemini, OpenAI.

Keys come from the Keychain via KeychainStore (CLAUDE.md rule 4), are passed per call as api_key=, and are never
logged, put in errors, or handed to LiteLLM's own config/env. LiteLLM is locked down before first use: telemetry
off, no callbacks, and its price map is the bundled copy (no download). Our prices.py is the only cost source.
If LiteLLM can't be imported, Anthropic falls back to the direct urllib adapter (proven in benchmarks/cloud_bench.py)
and the fallback is logged; Gemini and OpenAI then fail with a clear error.
`completion` / `opener` are injectable so tests never touch the network.
"""
from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from scribe_bot.core import logs
from scribe_bot.core.secrets import KeychainStore

log = logging.getLogger("scribe_bot.ai")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
# Server-side refusal fallback ("default" routing), recommended for Sonnet 5.5 on the Claude API (urllib path only).
FALLBACK_BETA = "server-side-fallback-2026-07-01"
LITELLM_PREFIX = {"anthropic": "anthropic", "google": "gemini", "openai": "openai"}


@dataclass
class Result:
    text: str
    tokens_in: int
    tokens_out: int
    stop_reason: str | None
    seconds: float
    model: str


class ProviderError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(logs.redact(message))
        self.status = status

    @property
    def retryable(self) -> bool:
        """Rate limits, overload, server errors, and network failures are worth trying elsewhere."""
        return self.status is None or self.status in (408, 409, 429) or self.status >= 500


Opener = Callable[..., object]
_litellm = None


def load_litellm():
    """Import LiteLLM once, locked down. Returns None if it isn't installed."""
    global _litellm
    if _litellm is None:
        os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"  # must be set before import: no price-map download
        try:
            import litellm
        except ImportError:
            return None
        litellm.telemetry = False
        litellm.suppress_debug_info = True
        litellm.turn_off_message_logging = True
        litellm.drop_params = True  # ignore params a provider doesn't support instead of failing
        for name in ("callbacks", "success_callback", "failure_callback", "input_callback", "service_callback",
                     "_async_success_callback", "_async_failure_callback"):
            setattr(litellm, name, [])
        _litellm = litellm
    return _litellm


class LiteLLM:
    def __init__(self, provider: str, secret_ref: str, store: KeychainStore | None = None,
                 completion: Callable | None = None):
        if provider not in LITELLM_PREFIX:
            raise ProviderError(f"unknown provider {provider!r}", 400)
        self.name = provider
        self.secret_ref = secret_ref
        self.store = store or KeychainStore()
        self._completion = completion

    def call(self, model: str, system: str, user: str, max_tokens: int = 16000) -> Result:
        completion = self._completion
        if completion is None:
            lib = load_litellm()
            if lib is None:
                raise ProviderError("LiteLLM is not installed", 500)
            completion = lib.completion
        key = self.store.get(self.secret_ref.removeprefix("scribe-bot."))
        t = time.monotonic()
        try:
            resp = completion(model=f"{LITELLM_PREFIX[self.name]}/{model}", api_key=key, max_tokens=max_tokens,
                              timeout=600, num_retries=0,  # the router does fallbacks and records each attempt
                              messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
        except Exception as e:  # LiteLLM maps provider errors to exceptions carrying status_code
            status = getattr(e, "status_code", None)
            if type(e).__name__ in ("APIConnectionError", "Timeout") and status in (None, 500):
                status = None  # network trouble: retryable elsewhere
            raise ProviderError(f"{self.name} error {status or ''}: {type(e).__name__}: {str(e)[:300]}", status)
        choice = resp.choices[0]
        usage = getattr(resp, "usage", None)
        return Result(choice.message.content or "", getattr(usage, "prompt_tokens", 0) or 0,
                      getattr(usage, "completion_tokens", 0) or 0, getattr(choice, "finish_reason", None),
                      round(time.monotonic() - t, 1), model)


class Anthropic:
    """Direct Messages API over urllib: used only when LiteLLM can't be imported."""
    name = "anthropic"

    def __init__(self, secret_ref: str, store: KeychainStore | None = None, opener: Opener = urllib.request.urlopen,
                 fallbacks: bool = True):
        self.secret_ref = secret_ref
        self.store = store or KeychainStore()
        self.opener = opener
        self.fallbacks = fallbacks

    def call(self, model: str, system: str, user: str, max_tokens: int = 16000) -> Result:
        body: dict = {"model": model, "max_tokens": max_tokens, "system": system,
                      "messages": [{"role": "user", "content": user}]}
        headers = {"x-api-key": self.store.get(self.secret_ref.removeprefix("scribe-bot.")),
                   "anthropic-version": API_VERSION, "content-type": "application/json"}
        if self.fallbacks:
            body["fallbacks"] = "default"
            headers["anthropic-beta"] = FALLBACK_BETA
        req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), headers=headers)
        t = time.monotonic()
        try:
            with self.opener(req, timeout=600) as r:
                resp = json.load(r)
        except urllib.error.HTTPError as e:
            raise ProviderError(f"Anthropic API error {e.code}: {e.read().decode(errors='replace')[:300]}", e.code)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise ProviderError(f"network error: {type(e).__name__}")
        usage = resp.get("usage", {})
        text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
        return Result(text, usage.get("input_tokens", 0), usage.get("output_tokens", 0), resp.get("stop_reason"),
                      round(time.monotonic() - t, 1), resp.get("model", model))


def make(provider: str, secret_ref: str):
    """The adapter for an account. LiteLLM for everything; direct Anthropic only if LiteLLM is missing."""
    if load_litellm() is None:
        if provider == "anthropic":
            log.warning("LiteLLM not importable; using the direct Anthropic adapter")
            return Anthropic(secret_ref)
        raise ProviderError(f"LiteLLM is not installed, so {provider} can't be used", 500)
    return LiteLLM(provider, secret_ref)
