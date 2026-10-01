"""Hot Crew requests should not scan unrelated API endpoint matchers."""

import re

import pytest
from fastapi.routing import APIRoute
from starlette.routing import Match

from remembra import config, main
from remembra.crew.access import _walk_routes
from tests.security_harness import make_settings


@pytest.fixture
def app(monkeypatch):
    monkeypatch.setenv(main.CREW_MODE_ENV, "1")
    monkeypatch.setattr(config, "_settings", make_settings(auth_enabled=False))
    return main.create_app()


def match(app, method, path, monkeypatch):
    original = APIRoute.matches
    visits, selected = [], []

    def measured(route, scope):
        visits.append(route.endpoint.__module__)
        result = original(route, scope)
        if result[0] == Match.FULL:
            selected.append(route.endpoint)
        return result

    scope = {"type": "http", "method": method, "path": path, "root_path": "", "headers": []}
    with monkeypatch.context() as patch:
        patch.setattr(APIRoute, "matches", measured)
        for route in app.routes:
            if route.matches(scope)[0] == Match.FULL:
                break
    return visits, selected[-1] if selected else None


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/crew/heartbeat",
        "/api/v1/crews/crw_0000000000000a01/claims",
        "/api/v1/crews/crw_0000000000000a01/events",
    ],
)
def test_hot_crew_requests_do_not_scan_unrelated_api_endpoints(app, monkeypatch, path):
    visits, selected = match(app, "POST", path, monkeypatch)
    assert selected is not None
    unrelated = [v for v in visits if v.startswith("remembra.api.v1.") and not v.startswith("remembra.api.v1.crew")]
    assert unrelated == [], f"Unrelated endpoint matcher calls: {len(unrelated)}"


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/v1/crews/join"),
        ("POST", "/api/v1/crews/resolve"),
        ("POST", "/api/v1/session/close"),
        ("POST", "/api/v1/auth/login"),
        ("POST", "/api/v1/memories"),
        ("GET", "/health"),
    ],
)
def test_specific_routes_keep_their_original_handlers(app, monkeypatch, method, path):
    expected = next(route.endpoint for full, route, _ in _walk_routes(app.routes) if full == path and method in route.methods)
    _, selected = match(app, method, path, monkeypatch)
    assert selected is expected


def test_every_existing_http_route_keeps_its_selected_handler(monkeypatch):
    monkeypatch.setenv(main.CREW_MODE_ENV, "0")
    monkeypatch.setattr(config, "_settings", make_settings(auth_enabled=False))
    before = main.create_app()
    baseline = []
    for template, route, _ in _walk_routes(before.routes):
        path = re.sub(r"\{[^}:]+(?::([^}]+))?\}", lambda m: "1" if m[1] in {"int", "float"} else "example", template)
        for method in route.methods:
            _, original = match(before, method, path, monkeypatch)
            baseline.append((method, template, path, original))
    main.install_crew(before)
    for method, template, path, original in baseline:
        _, current = match(before, method, path, monkeypatch)
        assert current is original, (method, template)
    assert len(baseline) > 150
