"""TTL strings mean the same thing on the server, in the SDK and in its shadow cache.

Regressions (truth audit P-141, P-202):

- The server read only whole numbers, so the SDK's own TTLs for temporal
  phrases were dropped: "Meeting tomorrow" sent '1.5d' and "call next week"
  sent '1.4w'; the server logged them as invalid and stored the memory with no
  expiry.
- The server read 'm' as months, so "in 10 minutes" ('40m') kept the memory
  for 1,200 days, while the SDK's shadow cache read the same string as 40
  minutes.
- A TTL the server could not read was accepted (201) and silently ignored.

Server paths run over the real routes, ``MemoryService`` and SQLite
(``agent_api_harness``; only the vector store and embedder are fakes).
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from remembra.api.v1.agent_session import StatusUpsertRequest
from remembra.client.shadow_ttl import parse_ttl_string
from remembra.client.temporal_parser import TEMPORAL_PATTERNS, TemporalParser
from remembra.config import Settings
from remembra.core.time import utcnow
from remembra.models.memory import StoreRequest
from remembra.security.error_sanitizer import sanitize_error_message
from remembra.services.memory import parse_ttl
from remembra.temporal.ttl import parse_ttl as temporal_parse_ttl
from tests.agent_api_harness import build_api

MINUTE = 60
HOUR = 3600
DAY = 86400

# --------------------------------------------------------------------------
# The grammar
# --------------------------------------------------------------------------

VALID = {
    # fractions: the SDK's temporal TTLs before this change
    "1.5d": 36 * HOUR,
    "1.4w": 1.4 * 7 * DAY,
    "1.5mo": 45 * DAY,
    "3.5d": 84 * HOUR,
    "0.5h": 30 * MINUTE,
    # m is minutes, mo is months
    "40m": 40 * MINUTE,
    "40min": 40 * MINUTE,
    "90 minutes": 90 * MINUTE,
    "3mo": 90 * DAY,
    "2 months": 60 * DAY,
    # a capital M alone is months, as remembra.temporal.ttl always read it
    "1M": 30 * DAY,
    "6M": 180 * DAY,
    # the whole-number forms that always worked
    "36h": 36 * HOUR,
    "30d": 30 * DAY,
    "2w": 14 * DAY,
    "1y": 365 * DAY,
    "90s": 90,
    # case and spacing
    " 30D ": 30 * DAY,
    "24 HOURS": 24 * HOUR,
    "1 Year": 365 * DAY,
    "100y": 100 * 365 * DAY,
}

INVALID = [
    "abc",  # no number
    "30",  # no unit
    "d30",  # unit first
    "30x",  # unknown unit
    "10mm",  # unknown unit
    "0d",  # zero
    "0.0h",  # zero
    "0.5s",  # under a second
    "-1d",  # negative
    "1.5.2d",  # two decimal points
    "1,5d",  # comma decimal
    ".5d",  # no leading digit
    "1e3d",  # exponent
    "nan d",
    "inf d",
    "30 d d",
    "101y",  # longer than 100 years
    "1000000h",  # 114 years
    "9" * 40 + "d",  # longer than 32 characters
]


@pytest.mark.parametrize(("ttl", "seconds"), sorted(VALID.items()))
def test_server_reads_every_unit_and_fractions(ttl: str, seconds: float) -> None:
    assert parse_ttl(ttl) == timedelta(seconds=seconds)


@pytest.mark.parametrize("ttl", INVALID)
def test_server_rejects_what_it_cannot_read(ttl: str) -> None:
    with pytest.raises(ValueError, match="Invalid TTL"):
        parse_ttl(ttl)


@pytest.mark.parametrize("ttl", [None, "", "   "])
def test_no_ttl_is_not_an_error(ttl: str | None) -> None:
    assert parse_ttl(ttl) is None


def test_errors_say_what_to_write_and_survive_the_422_sanitizer() -> None:
    for ttl, needle in [("soon", "number and a unit"), ("30x", "unknown unit 'x'"), ("0d", "at least 1 second")]:
        with pytest.raises(ValueError) as caught:
            parse_ttl(ttl)
        message = str(caught.value)
        assert needle in message
        assert "min" in message and "mo (months)" in message
        # main.py's RequestValidationError handler passes messages through this.
        assert sanitize_error_message(f"Value error, {message}") == f"Value error, {message}"
    with pytest.raises(ValueError, match="longest TTL is 100 years"):
        parse_ttl("101y")


@pytest.mark.parametrize("ttl", [*VALID, *INVALID])
def test_sdk_shadow_cache_and_temporal_module_read_ttls_like_the_server(ttl: str) -> None:
    """One grammar: the shadow cache never disagrees with the server about expiry."""
    try:
        expected: float | None = parse_ttl(ttl).total_seconds()  # type: ignore[union-attr]
    except ValueError:
        expected = None
    assert parse_ttl_string(ttl) == expected
    if expected is None:
        with pytest.raises(ValueError):
            temporal_parse_ttl(ttl)
    else:
        assert temporal_parse_ttl(ttl).total_seconds() == expected


# --------------------------------------------------------------------------
# Request validation: an unreadable TTL is refused, never ignored
# --------------------------------------------------------------------------


def test_store_request_refuses_an_unreadable_ttl() -> None:
    with pytest.raises(ValidationError, match="Invalid TTL 'soon'"):
        StoreRequest(content="x", ttl="soon")
    assert StoreRequest(content="x", ttl=" 1.5d ").ttl == "1.5d"
    assert StoreRequest(content="x", ttl="").ttl is None


def test_status_upsert_refuses_an_unreadable_ttl_before_anything_is_charged() -> None:
    with pytest.raises(ValidationError, match="Invalid TTL"):
        StatusUpsertRequest(key="deploy", value="green", ttl="1 fortnight")
    assert StatusUpsertRequest(key="deploy", value="green", ttl="40m").ttl == "40m"


def test_server_refuses_to_start_with_an_unreadable_checkpoint_ttl() -> None:
    with pytest.raises(ValidationError, match="Invalid TTL"):
        Settings(openai_api_key="test", checkpoint_default_ttl="a week")
    assert Settings(openai_api_key="test", checkpoint_default_ttl="7d").checkpoint_default_ttl == "7d"
    # Blank keeps its old meaning: checkpoints get no default TTL.
    assert Settings(openai_api_key="test", checkpoint_default_ttl="").checkpoint_default_ttl == ""


# --------------------------------------------------------------------------
# Real routes
# --------------------------------------------------------------------------


@pytest.fixture()
def api(tmp_path):
    yield from build_api(tmp_path)


def _expires_in(expires_at: Any) -> float:
    """Seconds from now until ``expires_at`` (an ISO string or datetime)."""
    assert expires_at, "the memory was stored with no expiry"
    when = expires_at if isinstance(expires_at, datetime) else datetime.fromisoformat(str(expires_at))
    return (when.replace(tzinfo=None) - utcnow()).total_seconds()


@pytest.mark.parametrize(
    ("ttl", "seconds"),
    [("1.5d", 36 * HOUR), ("1.4w", 1.4 * 7 * DAY), ("40m", 40 * MINUTE), ("3mo", 90 * DAY), ("36h", 36 * HOUR)],
)
def test_store_sets_the_expiry_the_ttl_says(api, ttl: str, seconds: float) -> None:
    res = api["http"].post("/api/v1/memories", json={"content": f"note with ttl {ttl}", "ttl": ttl})
    assert res.status_code == 201, res.text
    assert _expires_in(res.json()["expires_at"]) == pytest.approx(seconds, abs=60)


def test_store_with_an_unreadable_ttl_is_refused_and_nothing_is_stored(api) -> None:
    res = api["http"].post("/api/v1/memories", json={"content": "keep for a while", "ttl": "a while"})
    assert res.status_code == 422, res.text
    assert "Invalid TTL 'a while'" in res.text
    assert api["http"].get("/api/v1/memories").json() == []


def test_batch_store_with_one_unreadable_ttl_is_refused_whole(api) -> None:
    items = [{"content": "fine", "ttl": "1d"}, {"content": "not fine", "ttl": "1 fortnight"}]
    res = api["http"].post("/api/v1/memories/batch", json={"items": items})
    assert res.status_code == 422, res.text
    assert api["http"].get("/api/v1/memories").json() == []


# --------------------------------------------------------------------------
# The SDK's temporal TTLs (auto_expire_temporal=True)
# --------------------------------------------------------------------------

PHRASES = {
    "Meeting tomorrow at 3pm": 36 * HOUR,
    "Deadline in 2 hours": 3 * HOUR,
    "Call next week": 10 * DAY,
    "Trip next month": 45 * DAY,
    "Exam next year": 400 * DAY,
    "Remind me tonight": 18 * HOUR,
    "Remember for 3 days: buy groceries": 84 * HOUR,
    # 10 minutes + the 30-minute buffer = 40 minutes, rounded up to 1 hour
    "Ping me in 10 minutes": 1 * HOUR,
}


@pytest.mark.parametrize(("content", "seconds"), sorted(PHRASES.items()))
def test_sdk_temporal_ttl_reaches_the_server(api, content: str, seconds: int) -> None:
    client = api["make_client"](auto_expire_temporal=True)
    stored = client.store(content)
    assert _expires_in(stored.expires_at) == pytest.approx(seconds, abs=60)


def _read_like_0_16_1(ttl: str) -> timedelta | None:
    """The TTL grammar of servers 0.13 to 0.16.1 (services/memory.py parse_ttl at v0.16.1)."""
    ttl = ttl.strip().lower()
    try:
        value, unit = int(ttl[:-1]), ttl[-1]
    except (ValueError, IndexError):
        return None
    days = {"h": value / 24, "d": value, "w": value * 7, "m": value * 30, "y": value * 365}.get(unit)
    return None if days is None else timedelta(days=days)


def _pattern_seconds() -> list[int]:
    """Every TTL a pattern can produce for small counts (1..12 of its unit)."""
    seconds: set[int] = set()
    for _pattern, calc, *_ in TEMPORAL_PATTERNS:
        for n in range(1, 13):
            match = re.match(r"(\d+)", str(n))  # stands in for the count the pattern captured
            assert match is not None
            seconds.add(calc(match) if callable(calc) else calc)
    return sorted(seconds)


@pytest.mark.parametrize("seconds", [*_pattern_seconds(), 1, 59, 3599, 3601, 86399, 90061, 31536000 + 1])
def test_sdk_ttl_string_means_the_same_on_old_and_new_servers(seconds: int) -> None:
    """The SDK sends whole hours or whole days: 0.16.1 servers (live today) read them the same way.

    Rounded up to the next whole hour, never down, so a memory never expires
    before the moment the phrase refers to.
    """
    ttl = TemporalParser()._format_ttl(seconds)
    assert re.fullmatch(r"[1-9]\d*[hd]", ttl), ttl
    new = parse_ttl(ttl)
    assert new == _read_like_0_16_1(ttl)
    assert new is not None
    assert seconds <= new.total_seconds() < seconds + HOUR
