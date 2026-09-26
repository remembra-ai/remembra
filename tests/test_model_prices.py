"""Chat price table: every entry, suffix handling, and the prefix-collision cases.

Prices are USD per 1M tokens (input, cached input, output) and were checked
against the providers' pricing pages on 2026-09-25 (sources are listed in the
``remembra.cloud.model_prices`` module docstring).
"""

from __future__ import annotations

import logging

import pytest

from remembra.cloud import model_prices
from remembra.cloud.model_prices import (
    CHAT_PRICES_PER_M,
    UNKNOWN_CHAT_PRICE_PER_M,
    anthropic_usage,
    usd_for,
)
from remembra.core import ai_spend

M = 1_000_000

# (model id as the API reports it, expected (input, cached, output))
EXPECTED = {
    # OpenAI, Standard tier, short context
    "gpt-6-luna": (0.10, 0.01, 0.50),
    "gpt-6-sol": (2.00, 0.20, 10.00),
    "gpt-6-astra": (10.00, 1.00, 50.00),
    "gpt-5.6-luna": (0.20, 0.02, 1.20),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-sol": (4.00, 0.40, 20.00),
    "gpt-5.5": (5.00, 0.50, 30.00),
    "gpt-5.5-pro": (30.00, 30.00, 180.00),
    "gpt-5.4": (2.50, 0.25, 15.00),
    "gpt-5.4-mini": (0.75, 0.075, 4.50),
    "gpt-5.4-nano": (0.20, 0.02, 1.25),
    "gpt-5.4-pro": (30.00, 30.00, 180.00),
    "gpt-5.2": (1.75, 0.175, 14.00),
    "gpt-5.2-pro": (21.00, 21.00, 168.00),
    "gpt-5.1": (1.25, 0.125, 10.00),
    "gpt-5": (1.25, 0.125, 10.00),
    "gpt-5-mini": (0.25, 0.025, 2.00),
    "gpt-5-nano": (0.05, 0.005, 0.40),
    "gpt-5-pro": (15.00, 15.00, 120.00),
    "gpt-4.1": (2.00, 0.50, 8.00),
    "gpt-4.1-mini": (0.40, 0.10, 1.60),
    "gpt-4.1-nano": (0.10, 0.025, 0.40),
    "gpt-4o": (2.50, 1.25, 10.00),
    "gpt-4o-2024-05-13": (5.00, 5.00, 15.00),
    "gpt-4o-mini": (0.15, 0.075, 0.60),
    # Anthropic (base input, cache hit, output)
    "claude-opus-5-5": (4.00, 0.20, 20.00),
    "claude-opus-5": (5.00, 0.50, 25.00),
    "claude-opus-4-8": (5.00, 0.50, 25.00),
    "claude-opus-4-7": (5.00, 0.50, 25.00),
    "claude-opus-4-6": (5.00, 0.50, 25.00),
    "claude-opus-4-5": (5.00, 0.50, 25.00),
    "claude-opus-4-1": (15.00, 1.50, 75.00),
    "claude-fable-5-1": (10.00, 0.25, 50.00),
    "claude-fable-5": (10.00, 1.00, 50.00),
    "claude-sonnet-5": (2.00, 0.20, 10.00),
    "claude-sonnet-4-6": (3.00, 0.30, 15.00),
    "claude-sonnet-4-5": (3.00, 0.30, 15.00),
    "claude-haiku-4-5": (1.00, 0.10, 5.00),
    "claude-haiku-4-5-20251001": (1.00, 0.10, 5.00),
}


@pytest.fixture(autouse=True)
def _fresh_warnings():
    model_prices._warned.clear()
    yield
    model_prices._warned.clear()


def _cost(model: str, *, fresh: int = 0, cached: int = 0, output: int = 0) -> float:
    usage = {
        "prompt_tokens": fresh + cached,
        "completion_tokens": output,
        "prompt_tokens_details": {"cached_tokens": cached},
    }
    return usd_for(usage, model)


@pytest.mark.parametrize(("model", "prices"), sorted(EXPECTED.items()))
def test_each_model_is_billed_at_its_list_price(model: str, prices: tuple[float, float, float]) -> None:
    price_in, price_cached, price_out = prices
    assert _cost(model, fresh=M) == pytest.approx(price_in)
    assert _cost(model, cached=M) == pytest.approx(price_cached)
    assert _cost(model, output=M) == pytest.approx(price_out)
    assert _cost(model, fresh=M, cached=M, output=M) == pytest.approx(price_in + price_cached + price_out)


def test_every_table_entry_is_covered_by_a_test() -> None:
    assert set(CHAT_PRICES_PER_M) - {"claude-haiku-4", "claude-sonnet-4", "claude-opus-4"} <= set(EXPECTED)


@pytest.mark.parametrize(
    ("model", "not_priced_as"),
    [
        # Gap analysis section 11: these used to fall to a shorter prefix or the fallback.
        ("gpt-6-luna", UNKNOWN_CHAT_PRICE_PER_M),
        ("gpt-5.4-nano", CHAT_PRICES_PER_M["gpt-5"]),
        ("gpt-5.6-luna", CHAT_PRICES_PER_M["gpt-5"]),
        ("claude-opus-5-5", CHAT_PRICES_PER_M["claude-opus-5"]),
        ("gpt-5-pro", CHAT_PRICES_PER_M["gpt-5"]),
        ("gpt-5.5-pro", CHAT_PRICES_PER_M["gpt-5.5"]),
        ("gpt-4o-2024-05-13", CHAT_PRICES_PER_M["gpt-4o"]),
        ("claude-fable-5-1", CHAT_PRICES_PER_M["claude-fable-5"]),
    ],
)
def test_prefix_collisions_resolve_to_the_exact_model(model: str, not_priced_as: tuple[float, float, float]) -> None:
    assert model_prices._lookup(model) == EXPECTED[model]
    assert model_prices._lookup(model) != not_priced_as
    assert model not in model_prices._warned


@pytest.mark.parametrize(
    ("reported", "entry"),
    [
        ("gpt-4o-mini-2024-07-18", "gpt-4o-mini"),
        ("gpt-4o-2024-08-06", "gpt-4o"),
        ("gpt-5.4-nano-2026-03-17", "gpt-5.4-nano"),
        ("gpt-6-luna-2026-08-01", "gpt-6-luna"),
        ("claude-haiku-4-5-20251001", "claude-haiku-4"),
        ("claude-opus-4-1-20250805", "claude-opus-4"),
        ("GPT-4o-mini", "gpt-4o-mini"),
        ("  gpt-5.6-luna  ", "gpt-5.6-luna"),
    ],
)
def test_dash_suffixes_and_case_match_the_base_entry(reported: str, entry: str) -> None:
    assert model_prices._lookup(reported) == CHAT_PRICES_PER_M[entry]
    assert not model_prices._warned


@pytest.mark.parametrize("model", ["gpt-5.3-codex", "gpt-5.9", "gpt-6-nova", "claude-3-haiku-20240307", "", "gpt-5x"])
def test_unknown_or_unlisted_version_uses_fallback_not_a_sibling(model: str) -> None:
    assert model_prices._lookup(model) == UNKNOWN_CHAT_PRICE_PER_M


def test_unknown_model_warns_once_per_model(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="remembra.cloud.model_prices")
    usage = {"prompt_tokens": 1000, "completion_tokens": 100}
    first = usd_for(usage, "gpt-9-mystery")
    second = usd_for(usage, "gpt-9-mystery")
    usd_for(usage, "gpt-6-luna")  # known: no warning
    usd_for(usage, "other-unknown")

    expected = (1000 * UNKNOWN_CHAT_PRICE_PER_M[0] + 100 * UNKNOWN_CHAT_PRICE_PER_M[2]) / M
    assert first == second == pytest.approx(expected)
    warnings = [r.getMessage() for r in caplog.records if "model_price_unknown" in r.getMessage()]
    assert len(warnings) == 2
    assert "model=gpt-9-mystery" in warnings[0]
    assert "model=other-unknown" in warnings[1]


def test_none_and_empty_usage_cost_nothing() -> None:
    assert usd_for(None, "gpt-6-luna") == 0.0
    assert usd_for({}, "gpt-6-luna") == 0.0
    assert usd_for({"prompt_tokens": None, "completion_tokens": None}, "gpt-6-luna") == 0.0
    assert not model_prices._warned  # nothing to price, so no lookup and no warning


def test_cached_tokens_are_capped_at_prompt_tokens() -> None:
    usage = {"prompt_tokens": 100, "completion_tokens": 0, "prompt_tokens_details": {"cached_tokens": 10_000}}
    assert usd_for(usage, "gpt-6-luna") == pytest.approx(100 * 0.01 / M)


def test_anthropic_usage_prices_opus_5_5_cache_reads_at_its_own_rate() -> None:
    usage = anthropic_usage(
        {
            "input_tokens": 1_000_000,
            "cache_read_input_tokens": 1_000_000,
            "cache_creation_input_tokens": 0,
            "output_tokens": 1_000_000,
        }
    )
    assert usd_for(usage, "claude-opus-5-5") == pytest.approx(4.00 + 0.20 + 20.00)


async def test_metered_chat_records_the_exact_model_price_on_the_spend_job() -> None:
    """The real metering path (metered_chat -> record_llm_usage -> usd_for) uses the new entries."""
    from types import SimpleNamespace

    usage = {"prompt_tokens": 2_000_000, "completion_tokens": 1_000_000, "prompt_tokens_details": {"cached_tokens": 0}}

    async def create(**kwargs):  # type: ignore[no-untyped-def]
        return SimpleNamespace(usage=usage, choices=[])

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    job = ai_spend.SpendJob(user_id="u1", budget_usd=100.0)
    with ai_spend.activate(job):
        await ai_spend.metered_chat(client, model="gpt-6-luna", messages=[{"role": "user", "content": "hi"}])
    await job.wait_settled()
    assert job.usd == pytest.approx(2 * 0.10 + 1 * 0.50)
    assert job.llm_calls == 1
