"""Chat-model list prices used to turn ``response.usage`` into dollars.

USD per 1M tokens: (input, cached input, output), Standard tier, short context.
Model names returned by the API may carry a suffix (``gpt-4o-mini-2024-07-18``,
``claude-haiku-4-5-20251001``); the longest table entry that the name equals,
or that is followed by a ``-`` in the name, wins. A dotted version is NOT a
suffix: ``gpt-5.4-nano`` never falls back to ``gpt-5``, because a sibling
version can be priced very differently. A model with no matching entry is
metered at :data:`UNKNOWN_CHAT_PRICE_PER_M` and logged once per process as
``model_price_unknown``; add the model to the table when that warning appears.

Sources (checked 2026-09-25; update both the table and this note together):

* OpenAI: https://developers.openai.com/api/docs/pricing (Standard tab,
  short-context column; long-context rates for >272K-token prompts are not
  modelled, since enrichment prompts are short chunks).
* Anthropic: https://platform.claude.com/docs/en/about-claude/pricing (base
  input, "cache hits and refreshes", output) and model IDs from
  https://platform.claude.com/docs/en/about-claude/models/overview.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# model prefix -> (input, cached_input, output) USD per 1M tokens.
# Where a model has no cached-input price, the input price is used.
CHAT_PRICES_PER_M: dict[str, tuple[float, float, float]] = {
    # OpenAI
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "gpt-4o": (2.50, 1.25, 10.00),
    "gpt-4o-2024-05-13": (5.00, 5.00, 15.00),
    "gpt-4.1-nano": (0.10, 0.025, 0.40),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5-pro": (15.00, 15.00, 120.00),
    "gpt-5": (1.25, 0.125, 10.00),
    "gpt-5.1": (1.25, 0.125, 10.00),
    "gpt-5.2": (1.75, 0.175, 14.00),
    "gpt-5.2-pro": (21.00, 21.00, 168.00),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5.4-pro": (30.00, 30.00, 180.00),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.5-pro": (30.00, 30.00, 180.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6-astra": (10.00, 1.00, 50.00),
    # Anthropic (entity extraction can run on Claude; usage is mapped to these
    # fields). "cached" is the cache-hit price; cache writes are billed as input.
    "claude-haiku-4": (1.00, 0.10, 5.00),  # Haiku 4.5
    "claude-sonnet-4": (3.00, 0.30, 15.00),  # Sonnet 4 / 4.5 / 4.6
    "claude-sonnet-5": (2.00, 0.20, 10.00),
    "claude-opus-4": (15.00, 1.50, 75.00),  # Opus 4 / 4.1; newer Opus 4.x below
    "claude-opus-4-5": (5.00, 0.50, 25.00),
    "claude-opus-4-6": (5.00, 0.50, 25.00),
    "claude-opus-4-7": (5.00, 0.50, 25.00),
    "claude-opus-4-8": (5.00, 0.50, 25.00),
    "claude-opus-5": (5.00, 0.50, 25.00),
    "claude-opus-5-5": (4.00, 0.20, 20.00),  # cache hits at 0.05x input
    "claude-fable-5": (10.00, 1.00, 50.00),
    "claude-fable-5-1": (10.00, 0.25, 50.00),  # cache hits at 0.025x input
}
# Fallback for models not in the table (logged once per model).
UNKNOWN_CHAT_PRICE_PER_M: tuple[float, float, float] = (2.50, 1.25, 10.00)

_warned: set[str] = set()


def _lookup(model: str) -> tuple[float, float, float]:
    name = (model or "").strip().lower()
    best: str | None = None
    for prefix in CHAT_PRICES_PER_M:
        matches = name == prefix or name.startswith(prefix + "-")
        if matches and (best is None or len(prefix) > len(best)):
            best = prefix
    if best is None:
        if name not in _warned:
            _warned.add(name)
            logger.warning(
                "model_price_unknown model=%s; not in the price table, metering at the fallback price %s",
                name,
                UNKNOWN_CHAT_PRICE_PER_M,
            )
        return UNKNOWN_CHAT_PRICE_PER_M
    return CHAT_PRICES_PER_M[best]


def _field(obj: Any, name: str) -> Any:
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def anthropic_usage(usage: Any) -> dict[str, Any] | None:
    """Map an Anthropic ``usage`` block (input/output/cache-read tokens) to the OpenAI field names."""
    if usage is None:
        return None
    fresh = _int(_field(usage, "input_tokens"))
    cached = _int(_field(usage, "cache_read_input_tokens"))
    created = _int(_field(usage, "cache_creation_input_tokens"))
    return {
        "prompt_tokens": fresh + cached + created,
        "completion_tokens": _int(_field(usage, "output_tokens")),
        "prompt_tokens_details": {"cached_tokens": cached},
    }


def usd_for(usage: Any, model: str) -> float:
    """Dollar cost of one chat completion from its ``usage`` block (0.0 when absent).

    Accepts the OpenAI SDK usage object or a plain dict with ``prompt_tokens``,
    ``completion_tokens`` and optional ``prompt_tokens_details.cached_tokens``.
    """
    if usage is None:
        return 0.0
    prompt = _int(_field(usage, "prompt_tokens"))
    completion = _int(_field(usage, "completion_tokens"))
    cached = min(prompt, _int(_field(_field(usage, "prompt_tokens_details"), "cached_tokens")))
    if not prompt and not completion:
        return 0.0
    price_in, price_cached, price_out = _lookup(model)
    return ((prompt - cached) * price_in + cached * price_cached + completion * price_out) / 1_000_000
