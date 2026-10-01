"""Keep cold-key cryptography off the loop and honor the caller's HTTP budget."""

import asyncio
import threading

import pytest
from unittest.mock import AsyncMock, MagicMock

import httpx

from remembra.auth import keys
from remembra.auth.keys import APIKeyManager
from remembra.relay.config import RelayConfig
from remembra.relay.crew.crewd import Api


async def test_call_applies_budget_to_http_transport():
    captured = []

    async def handler(request):
        captured.append(request.extensions["timeout"])
        return httpx.Response(200, json={"ok": True})

    api = Api(
        RelayConfig(url="https://example.invalid", api_key="synthetic", agent_id=None, source="test"),
        agent_id=None,
        transport=httpx.MockTransport(handler),
    )
    try:
        assert (await api.call("GET", "/sample", timeout=30)).ok
        assert captured == [{"connect": 3.0, "read": 30.0, "write": 30.0, "pool": 30.0}]
    finally:
        await api.close()


@pytest.mark.parametrize("operation", ["verify", "create"])
async def test_independent_request_progresses_while_crypto_is_paused(monkeypatch, operation):
    loop = asyncio.get_running_loop()
    entered, progressed = asyncio.Event(), asyncio.Event()
    release = threading.Event()
    monkeypatch.setattr(keys, "_key_cache", {})
    db = MagicMock()
    db.get_active_api_key_by_lookup = AsyncMock(return_value={"id": "synthetic", "user_id": "u", "key_hash": "hash"})
    db.save_api_key = AsyncMock()
    manager = APIKeyManager(db)

    def paused(*_args):
        loop.call_soon_threadsafe(entered.set)
        release.wait(timeout=2)  # bounded even on the old, blocking implementation
        return True if operation == "verify" else "synthetic-hash"

    monkeypatch.setattr(manager, "verify_key" if operation == "verify" else "hash_key", paused)
    crypto = asyncio.create_task(
        manager.validate_key("rem_synthetic", record_use=False) if operation == "verify" else manager.create_key("synthetic-user")
    )

    async def independent_request():
        await entered.wait()
        progressed.set()

    other = asyncio.create_task(independent_request())
    try:
        await asyncio.wait_for(progressed.wait(), timeout=3)
        assert not crypto.done(), "cryptography blocked independent work until it finished"
        release.set()
        assert await crypto is not None
    finally:
        release.set()
        await asyncio.gather(crypto, other, return_exceptions=True)


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("change", ["revoke", "permissions", "replace_hash"])
async def test_inflight_verify_observes_authorization_change(monkeypatch, legacy, change):
    loop = asyncio.get_running_loop()
    entered = asyncio.Event()
    release = threading.Event()
    monkeypatch.setattr(keys, "_key_cache", {})
    original = {"id": "synthetic", "user_id": "u", "key_hash": "hash", "scopes": "write", "project_ids": "before"}
    current = dict(original)
    db = MagicMock()
    db.get_active_api_key_by_lookup = AsyncMock(
        side_effect=lambda *_args: None if legacy and not db.set_api_key_lookup.await_count else current
    )
    db.get_unmigrated_active_api_keys = AsyncMock(return_value=[original])
    db.set_api_key_lookup = AsyncMock()
    manager = APIKeyManager(db)

    def paused(*_args):
        loop.call_soon_threadsafe(entered.set)
        release.wait(timeout=2)
        return True

    monkeypatch.setattr(manager, "verify_key", paused)
    task = asyncio.create_task(manager.validate_key("rem_synthetic", record_use=False))
    try:
        await asyncio.wait_for(entered.wait(), timeout=3)
        assert not task.done(), "validation must leave time for a concurrent authorization change"
        if change == "revoke":
            current = None
        elif change == "replace_hash":
            current = {**original, "key_hash": "replacement"}
        else:
            current = {**original, "scopes": "read", "project_ids": "after"}
        release.set()
        result = await task
        if change == "permissions":
            assert result["scopes"] == ["read"] and result["project_ids"] == ["after"]
        else:
            assert result is None
            assert not keys._key_cache
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
