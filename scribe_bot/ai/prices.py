"""Model prices: the one place they live. USD per million tokens (input, output), standard tier, short context.

Checked 2026-10-19:
  Anthropic: claude-api reference (cached 2026-10-06).
  Google:    ai.google.dev/gemini-api/docs/pricing (paid tier). Flash prices are introductory; Google says several
             rise on 2026-12-01 or 2027-01-01, so re-check then.
  OpenAI:    developers.openai.com/api/docs/pricing.
Re-check when adding a model. LiteLLM's own price map is never used (it is downloaded from the internet).
"""
from __future__ import annotations

PRICES: dict[str, tuple[float, float]] = {
    # Anthropic
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
    "claude-opus-5-5": (4.00, 20.00),
    # Google Gemini (AI Studio API key)
    "gemini-4-argon": (2.00, 10.00),        # flagship, stable
    "gemini-3.8-flash": (0.75, 3.75),       # recommended Flash, stable
    "gemini-3.5-flash-lite": (0.30, 2.50),  # cheapest current stable
    # OpenAI
    "gpt-6-astra": (10.00, 50.00),          # flagship; too expensive for the $10/month cap, not in any profile
    "gpt-6.1-sol": (2.00, 10.00),           # quality tier
    "gpt-6-luna": (0.10, 0.50),             # budget tier
}


def cost(model: str, tokens_in: int, tokens_out: int) -> float:
    if model not in PRICES:
        raise KeyError(f"no price for {model}; add it to scribe_bot/ai/prices.py before using it")
    pin, pout = PRICES[model]
    return round((tokens_in * pin + tokens_out * pout) / 1e6, 6)
