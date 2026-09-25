"""SEC-8: webhook SSRF (create, update, delivery with DNS pinning) and webhook:manage permission."""

from __future__ import annotations

import httpx
import pytest

from remembra.api.v1 import webhooks as webhooks_api
from remembra.webhooks import manager as manager_mod
from remembra.webhooks.delivery import WebhookDelivery
from remembra.webhooks.events import WebhookEvent
from remembra.webhooks.manager import WebhookManager
from tests.security_harness import secure_app

PUBLIC_IP = "93.184.216.34"


@pytest.fixture
def dns(monkeypatch):
    table: dict[str, list[str]] = {"hooks.example.com": [PUBLIC_IP]}

    async def fake_resolve(hostname, port):
        if hostname not in table:
            raise OSError("NXDOMAIN")
        return table[hostname]

    monkeypatch.setattr(manager_mod, "_resolve", fake_resolve)
    return table


BLOCKED_URLS = [
    "http://127.0.0.1/x",
    "http://[::ffff:127.0.0.1]/x",  # IPv4-mapped IPv6
    "http://[::ffff:a9fe:a9fe]/latest/meta-data",  # mapped 169.254.169.254
    "http://[64:ff9b::a00:1]/x",  # NAT64 10.0.0.1
    "http://0.0.0.0/x",
    "http://0.1.2.3/x",
    "http://100.64.0.10/x",  # CGNAT
    "http://169.254.169.254/latest/meta-data",
    "http://224.0.0.1/x",
    "http://10.0.0.5/x",
    "http://internal-only.example.com/x",  # resolves to RFC1918 (see fixture)
    "http://does-not-resolve.example.com/x",  # DNS failure is denied, not allowed
    "http://metadata.google.internal/x",
    "ftp://hooks.example.com/x",
]


@pytest.mark.parametrize("url", BLOCKED_URLS)
async def test_blocked_targets_rejected(url, dns):
    dns["internal-only.example.com"] = [PUBLIC_IP, "10.1.2.3"]  # any private answer poisons the host
    ok, _ = await manager_mod.validate_webhook_url(url)
    assert not ok


async def test_public_target_allowed(dns):
    target = await manager_mod.resolve_webhook_target("https://hooks.example.com/in?a=1")
    assert target.ips == (PUBLIC_IP,) and target.hostname == "hooks.example.com"


async def test_api_create_update_and_permission(tmp_path, dns):
    async with secure_app(tmp_path, [webhooks_api.router]) as h:
        manager = WebhookManager(h.db)
        await manager.init_schema()
        h.app.state.webhook_manager = manager
        editor, _ = await h.api_key("tenant-a", "editor")
        viewer, _ = await h.api_key("tenant-a", "viewer")

        r = await h.client.post(
            "/api/v1/webhooks", json={"url": "https://hooks.example.com/in", "events": ["*"]}, headers={"X-API-Key": viewer}
        )
        assert r.status_code == 403

        hdr = {"X-API-Key": editor}
        r = await h.client.post("/api/v1/webhooks", json={"url": "http://169.254.169.254/", "events": ["*"]}, headers=hdr)
        assert r.status_code == 400
        r = await h.client.post("/api/v1/webhooks", json={"url": "https://hooks.example.com/in", "events": ["*"]}, headers=hdr)
        assert r.status_code == 201, r.text
        hook_id = r.json()["id"]

        # Update used to skip validation entirely.
        r = await h.client.patch(f"/api/v1/webhooks/{hook_id}", json={"url": "http://127.0.0.1:6333/collections"}, headers=hdr)
        assert r.status_code == 400
        stored = await manager.get_webhook(hook_id, "tenant-a")
        assert stored["url"] == "https://hooks.example.com/in"


async def test_delivery_pins_validated_ip_and_blocks_rebinding(dns):
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200)

    delivery = WebhookDelivery(max_retries=1)
    await delivery._client.aclose()
    delivery._client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)

    ok = await delivery.deliver("https://hooks.example.com/in?x=1", {"type": "memory.created"}, delivery_id="d1")
    assert ok
    sent = seen[-1]
    assert sent.url.host == PUBLIC_IP and sent.url.path == "/in" and sent.url.query == b"x=1"
    assert sent.headers["host"] == "hooks.example.com"
    assert sent.extensions.get("sni_hostname") == "hooks.example.com"

    # DNS now answers with an internal address (rebinding): nothing is sent.
    dns["hooks.example.com"] = ["127.0.0.1"]
    before = len(seen)
    assert await delivery.deliver("https://hooks.example.com/in", {"type": "memory.created"}) is False
    assert len(seen) == before
    await delivery.close()


async def test_dispatch_never_reaches_legacy_internal_url(tmp_path, dns, in_memory_db):
    """A URL stored before validation existed is still refused at delivery time."""
    seen: list[httpx.Request] = []
    delivery = WebhookDelivery(max_retries=1)
    await delivery._client.aclose()
    delivery._client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200)))
    manager = WebhookManager(in_memory_db, delivery=delivery)
    await manager.init_schema()
    await in_memory_db.conn.execute(
        "INSERT INTO webhooks (id, user_id, url, events, active, created_at, updated_at) "
        "VALUES ('legacy', 'tenant-a', 'http://127.0.0.1:6333/', '*', 1, 'now', 'now')"
    )
    await in_memory_db.conn.commit()
    queued = await manager.dispatch(WebhookEvent(type="memory.created", user_id="tenant-a", payload={}))
    assert queued == 1 and seen == []
    status = await (await in_memory_db.conn.execute("SELECT status FROM webhook_deliveries")).fetchone()
    assert status[0] == "failed"
    await delivery.close()
