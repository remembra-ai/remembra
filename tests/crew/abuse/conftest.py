"""Fixtures of the WP-17 abuse suite: a real red-team server, a dashboard login and an agent crew."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.crew.abuse.driver import Crew
from tests.crew.abuse.server import RedTeamServer, running_server


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RedTeamServer]:
    with running_server(tmp_path, monkeypatch) as srv:
        yield srv


@pytest.fixture
def limited_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RedTeamServer]:
    """The same server with every production rate limiter switched on."""
    with running_server(tmp_path, monkeypatch, rate_limits=True) as srv:
        yield srv


@pytest.fixture
def crew(server: RedTeamServer) -> Crew:
    """A crew with POS held by cc-1 (task T-1) and a second session cc-2, created through the API."""
    c = Crew(server.http, server.login(), server.admin_key)
    c.join("a")
    c.join("b")
    c.zone("pos", ["src/app/pos/**"], title="POS section")
    c.zone("reports", ["src/app/reports/**"], title="Reports")
    c.task("t1", "a", "POS split tender", ["pos"])
    c.start("t1", "a")
    return c
