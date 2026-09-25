"""OpenAI list prices used to turn ``response.usage`` into dollars.

USD per 1M tokens: (input, cached input, output). Model names returned by the
API may carry a date suffix (``gpt-4o-mini-2024-07-18``); the longest matching
prefix wins. An unknown model is priced at the most expensive known chat
model so metering errs on the side of the account ceiling, never below it.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# model prefix -> (input, cached_input, output) USD per 1M tokens
CHAT_PRICES_PER_M: dict[str, tuple[float, float, float]] = {
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    "gpt-4o": (2.50, 1.25, 10.00),
    "gpt-4.1-nano": (0.10, 0.025, 0.40),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5": (1.25, 0.125, 10.00),
}
# Conservative fallback for models not in the table.
UNKNOWN_CHAT_PRICE_PER_M: tuple[float, float, float] = (2.50, 1.25, 10.00)

_warned: set[str] = set()


def _lookup(model: str) -> tuple[float, float, float]:
    name = (model or "").strip().lower()
    best: str | None = None
    for prefix in CHAT_PRICES_PER_M:
        if name.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    if best is None:
        if name not in _warned:
            _warned.add(name)
            logger.warning("model_price_unknown model=%s; metering at the conservative fallback price", name)
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
