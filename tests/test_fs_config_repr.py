"""A config object's repr never carries its secret (F55).

A traceback with locals, a pytest assertion diff or a stray ``print`` shows
``repr(obj)``. These dataclasses hold an API key, a webhook secret or a host
token; their repr names the object without the secret. Construction, equality
and ``asdict`` are unchanged.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

import pytest

from remembra.auth.keys import APIKey
from remembra.cloud.paddle_config import PaddleConfig, PaddleEnvironment, PaddleSettings
from remembra.cloud.plans import PlanTier
from remembra.cloud.provisioning import ProvisionResult
from remembra.crew.hosts import RegisteredHost
from remembra.extraction.extractor import ExtractionConfig
from remembra.mcp.crew import Seat
from remembra.relay.config import RelayConfig
from remembra.tools.bridge import BridgeConfig
from remembra.tools.doctor import DoctorTarget

SECRET = "rem_SYNTHETICnotARealKey0123456789abcdefXYZ"
OTHER = "whsec_SYNTHETICnotARealSecret0123456789"


def _relay() -> RelayConfig:
    return RelayConfig("https://api.remembra.test", SECRET, "codex", "env", project="alpha")


def _target() -> DoctorTarget:
    return DoctorTarget("codex", Path("/tmp/config.toml"), "remembra-mcp", "https://api.remembra.test", SECRET, "alpha", "u1")


def _bridge() -> BridgeConfig:
    return BridgeConfig(upstream="https://api.remembra.test", api_key=SECRET)


def _paddle() -> PaddleSettings:
    config = PaddleConfig(environment=PaddleEnvironment.SANDBOX, api_base="https://sandbox-api.paddle.test")
    return PaddleSettings(api_key=SECRET, client_token="test_token", webhook_secret=OTHER, sandbox=True, config=config)


def _provision() -> ProvisionResult:
    return ProvisionResult(user_id="u1", api_key=SECRET, api_key_id="k1", plan=PlanTier.FREE)


def _host() -> RegisteredHost:
    return RegisteredHost(row={"id": "h1", "host_label": "laptop"}, token=SECRET)


def _extraction() -> ExtractionConfig:
    return ExtractionConfig(api_key=SECRET)


def _api_key() -> APIKey:
    return APIKey(id="k1", key=SECRET, user_id="u1", name="laptop", created_at=datetime(2026, 11, 10, tzinfo=UTC))


def _seat() -> Seat:
    return Seat(
        scope=("u1", "alpha"),
        project_id="alpha",
        agent_id="codex",
        client_session_id="s1",
        crew_id="c1",
        session_id="cs1",
        token=SECRET,
        callsign="codex-1",
        verified=True,
        observe_only=False,
        status_cursor=0,
        notice_cursor=0,
        last_touch=0.0,
    )


BUILDERS = [_relay, _target, _bridge, _provision, _host, _extraction, _api_key, _seat]


@pytest.mark.parametrize("build", BUILDERS, ids=lambda b: b.__name__.lstrip("_"))
def test_repr_and_str_leave_the_secret_out(build) -> None:
    obj = build()
    assert SECRET not in repr(obj)
    assert SECRET not in str(obj)
    assert SECRET not in f"{obj!r} {obj}"
    assert type(obj).__name__ in repr(obj)  # still a useful repr


def test_paddle_settings_repr_leaves_both_secrets_out() -> None:
    settings = _paddle()
    text = repr(settings)
    assert SECRET not in text and OTHER not in text
    assert "sandbox=True" in text and "client_token='test_token'" in text  # the public client token is no secret
    assert settings.api_key == SECRET and settings.webhook_secret == OTHER


def test_the_secret_is_still_there_for_the_code_that_needs_it() -> None:
    relay, target, bridge = _relay(), _target(), _bridge()
    assert relay.api_key == SECRET and target.api_key == SECRET and bridge.api_key == SECRET
    assert _provision().to_dict()["api_key"] == SECRET  # returned once to the new tenant
    assert _host().token == SECRET and _api_key().key == SECRET and _seat().token == SECRET
    assert asdict(relay)["api_key"] == SECRET
    assert relay == _relay() and relay != RelayConfig("https://api.remembra.test", "rem_other", "codex", "env", project="alpha")
    assert relay.redacted()["api_key"] == "set"


def test_positional_construction_is_unchanged() -> None:
    relay = RelayConfig("u", "k", "a", "s")
    assert (relay.url, relay.api_key, relay.agent_id, relay.source, relay.project, relay.project_aliases) == (
        "u",
        "k",
        "a",
        "s",
        None,
        "",
    )
    bridge = BridgeConfig("up", "127.0.0.1", 9, "k")
    assert (bridge.upstream, bridge.host, bridge.port, bridge.api_key) == ("up", "127.0.0.1", 9, "k")
    assert BridgeConfig("up").api_key is None and ExtractionConfig().api_key is None


def test_no_secret_in_a_traceback_with_locals() -> None:
    """What pytest and rich tracebacks print for a failing frame: the locals' reprs."""
    config = _relay()
    try:
        raise RuntimeError(f"could not reach the server with {config}")
    except RuntimeError as e:
        assert SECRET not in str(e)
