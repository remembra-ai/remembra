"""DEP-1: an unauthenticated crafted form body must not stall the API's only worker.

FastAPI parses a form or upload body before it runs the route's dependencies,
and auth is a dependency, so the parse runs for anyone. Two layers close it:

1. The lock pins python-multipart and Starlette releases that stop parsing at
   the field cap (python-multipart >= 0.0.31, Starlette >= 1.3.1).
2. ``remembra.core.body_limit.BodySizeLimitMiddleware`` refuses an oversized
   body before any of it is read (Content-Length) and stops reading a streamed
   body as soon as it passes the cap. 1 MiB by default, 64 KiB for urlencoded
   forms, and a larger cap only on the routes whose own limits need one.

These run the real ``create_app()`` stack with auth on and no key.
"""

from __future__ import annotations

import time
import tomllib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from packaging.version import Version
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

import remembra.config as config_module
from tests.security_harness import make_settings

ROOT = Path(__file__).resolve().parent.parent
MiB = 1024 * 1024
FORM_CAP = 64 * 1024
UPLOAD_CAP = 51 * MiB


@asynccontextmanager
async def api_client() -> AsyncIterator[httpx.AsyncClient]:
    previous = config_module._settings
    config_module._settings = make_settings()
    try:
        from remembra.main import create_app

        app = create_app()
        # No lifespan here: stand-ins for the services that routes resolve before auth.
        for name in ("memory_service", "audit_logger", "sanitizer"):
            setattr(app.state, name, SimpleNamespace())
        transport = httpx.ASGITransport(app=app, client=("203.0.113.10", 51234))
        async with httpx.AsyncClient(transport=transport, base_url="http://test", timeout=60) as client:
            yield client
    finally:
        config_module._settings = previous


# ---------------------------------------------------------------------------
# 1. The lock pins the fixed releases
# ---------------------------------------------------------------------------


def _locked() -> dict[str, Version]:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    return {pkg["name"]: Version(pkg["version"]) for pkg in lock["package"]}


@pytest.mark.parametrize(
    ("name", "floor"),
    [("python-multipart", "0.0.31"), ("starlette", "1.3.1"), ("fastapi", "0.139.2")],
)
def test_lock_pins_the_fixed_form_parser_releases(name: str, floor: str) -> None:
    assert _locked()[name] >= Version(floor), f"{name} {_locked()[name]} is below the fixed {floor}"


def test_pyproject_floors_keep_pip_installs_on_the_fixed_releases() -> None:
    server = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]["server"]
    assert "python-multipart>=0.0.31" in server
    assert "starlette>=1.3.1" in server
    assert "fastapi>=0.139.2" in server


# ---------------------------------------------------------------------------
# 2. The repro: 1 MiB of "a;" as a form to the upload route, no key
# ---------------------------------------------------------------------------


async def test_unauthenticated_form_bomb_to_the_upload_route_is_refused_fast() -> None:
    async with api_client() as client:
        start = time.perf_counter()
        r = await client.post(
            "/api/v1/transfer/import/file",
            content=b"a;" * (MiB // 2),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        elapsed = time.perf_counter() - start
    assert r.status_code in (401, 413), r.text
    assert elapsed < 0.3, f"took {elapsed:.3f}s"


async def test_a_streamed_form_bomb_without_content_length_is_cut_off_unread() -> None:
    sent = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal sent
        for _ in range(64):  # 4 MiB offered in 64 KiB chunks
            sent += 1
            yield b"a;" * 32768

    async with api_client() as client:
        start = time.perf_counter()
        r = await client.post(
            "/api/v1/transfer/import/file",
            content=body(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        elapsed = time.perf_counter() - start
    assert r.status_code == 413, r.text
    assert elapsed < 0.3, f"took {elapsed:.3f}s"
    assert sent <= 2, f"read {sent} chunks past a {FORM_CAP}-byte cap"


# ---------------------------------------------------------------------------
# 3. Every other route: 1 MiB
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/api/v1/memories", "/api/v1/auth/login", "/api/v1/billing/webhook/paddle"])
async def test_a_2_mb_body_to_any_other_route_is_413(path: str) -> None:
    async with api_client() as client:
        r = await client.post(path, content=b"{" + b" " * (2 * MiB) + b"}", headers={"Content-Type": "application/json"})
    assert r.status_code == 413, r.text
    assert r.json() == {"detail": "Request body too large. The limit for this request is 1 MiB."}
    assert r.headers["connection"] == "close"


async def test_a_body_that_lies_about_its_length_is_refused_before_it_is_read() -> None:
    reads = 0

    async def body() -> AsyncIterator[bytes]:
        nonlocal reads
        reads += 1
        yield b"{}"

    async with api_client() as client:
        r = await client.post(
            "/api/v1/memories",
            content=body(),
            headers={"Content-Type": "application/json", "Content-Length": str(60 * MiB)},
        )
    assert r.status_code == 413
    assert reads == 0


async def test_small_requests_are_untouched() -> None:
    async with api_client() as client:
        assert (await client.get("/")).status_code == 200
        r = await client.post("/api/v1/memories", json={"content": "hello"})
        assert r.status_code == 401  # reached the route: auth answered, not the size cap


# ---------------------------------------------------------------------------
# 4. The routes whose own limits need more than 1 MiB keep them
# ---------------------------------------------------------------------------


async def test_the_upload_route_takes_a_multipart_file_up_to_its_own_50_mb_limit() -> None:
    async with api_client() as client:
        files = {"file": ("notes.txt", b"x" * (3 * MiB), "text/plain")}
        r = await client.post("/api/v1/transfer/import/file?format=plaintext", files=files)
        assert r.status_code == 401, r.text  # the 3 MiB upload got through to auth
        r = await client.post(
            "/api/v1/transfer/import/file?format=plaintext",
            content=b"",
            headers={"Content-Type": "multipart/form-data; boundary=x", "Content-Length": str(UPLOAD_CAP + 1)},
        )
        assert r.status_code == 413
        assert r.json()["detail"] == "Request body too large. The limit for this request is 51 MiB."


async def test_the_bulk_json_routes_keep_their_documented_maxima() -> None:
    async with api_client() as client:
        big = b'{"items": [' + b" " * (5 * MiB) + b"]}"
        r = await client.post("/api/v1/memories/batch", content=big, headers={"Content-Type": "application/json"})
        assert r.status_code == 401, r.text
        r = await client.post(
            "/api/v1/ingest/conversation",
            content=b'{"messages": [' + b" " * (9 * MiB) + b"]}",
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 401, r.text
        # A form body gets the small form cap even on those routes.
        r = await client.post(
            "/api/v1/memories/batch", content=b"a;" * MiB, headers={"Content-Type": "application/x-www-form-urlencoded"}
        )
        assert r.status_code == 413


def test_limit_table() -> None:
    from remembra.core.body_limit import DEFAULT_MAX_BODY_BYTES, FORM_MAX_BODY_BYTES, IMPORT_FILE_MAX_BODY_BYTES, limit_for

    def scope(path: str, ctype: str | None, method: str = "POST") -> dict:
        headers = [(b"content-type", ctype.encode())] if ctype else []
        return {"type": "http", "method": method, "path": path, "headers": headers}

    assert limit_for(scope("/api/v1/memories", "application/json")) == DEFAULT_MAX_BODY_BYTES == MiB
    assert limit_for(scope("/api/v1/memories", "application/x-www-form-urlencoded")) == FORM_MAX_BODY_BYTES == FORM_CAP
    assert limit_for(scope("/api/v1/transfer/import/file", "multipart/form-data; boundary=a")) == IMPORT_FILE_MAX_BODY_BYTES
    assert IMPORT_FILE_MAX_BODY_BYTES == UPLOAD_CAP
    assert limit_for(scope("/api/v1/transfer/import/file", "application/json")) == MiB
    assert limit_for(scope("/api/v1/transfer/import/file/", "multipart/form-data; boundary=a")) == 51 * MiB
    assert limit_for(scope("/api/v1/memories/batch", "application/json")) == 8 * MiB
    assert limit_for(scope("/api/v1/memories/batch", "application/json", method="PUT")) == MiB
    assert limit_for(scope("/api/v1/ingest/conversation", "application/json; charset=utf-8")) == 12 * MiB
    assert limit_for(scope("/api/v1/ingest/conversation/stream", "application/json")) == 12 * MiB
    assert limit_for(scope("/api/v1/ingest/changelog", "application/json")) == 4 * MiB
    assert limit_for(scope("/api/v1/memories", None)) == MiB
    prefixed = scope("/remembra/api/v1/transfer/import/file", "multipart/form-data; boundary=a") | {"root_path": "/remembra"}
    assert limit_for(prefixed) == IMPORT_FILE_MAX_BODY_BYTES


# ---------------------------------------------------------------------------
# 5. The middleware on its own
# ---------------------------------------------------------------------------


def _echo_app(limit: int) -> Starlette:
    from remembra.core.body_limit import BodySizeLimitMiddleware

    async def echo(request: Request) -> JSONResponse:
        return JSONResponse({"read": len(await request.body())})

    app = Starlette(routes=[Route("/echo", echo, methods=["POST"])])
    app.add_middleware(BodySizeLimitMiddleware, default_limit=limit, form_limit=limit)
    return app


async def _post(app: Starlette, path: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as client:
        return await client.post(path, **kwargs)


async def test_middleware_passes_bodies_at_the_limit_and_refuses_one_byte_more() -> None:
    app = _echo_app(100)
    assert (await _post(app, "/echo", content=b"x" * 100)).json() == {"read": 100}
    assert (await _post(app, "/echo", content=b"x" * 101)).status_code == 413

    async def chunks(n: int) -> AsyncIterator[bytes]:
        for _ in range(n):
            yield b"x" * 10

    assert (await _post(app, "/echo", content=chunks(10))).json() == {"read": 100}
    r = await _post(app, "/echo", content=chunks(11))  # no Content-Length: cut off while streaming
    assert r.status_code == 413 and r.text == "Request body too large. The limit for this request is 100 bytes."


async def test_middleware_rejects_a_malformed_content_length() -> None:
    r = await _post(_echo_app(100), "/echo", content=b"x", headers={"Content-Length": "12abc"})
    assert r.status_code == 400


async def test_websocket_and_lifespan_scopes_pass_through() -> None:
    from remembra.core.body_limit import BodySizeLimitMiddleware

    seen: list[str] = []

    async def inner(scope, receive, send) -> None:
        seen.append(scope["type"])

    mw = BodySizeLimitMiddleware(inner)
    await mw({"type": "websocket", "path": "/ws", "headers": []}, None, None)
    await mw({"type": "lifespan"}, None, None)
    assert seen == ["websocket", "lifespan"]
