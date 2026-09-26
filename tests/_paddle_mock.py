"""In-process fake of the Paddle Billing API, served through ``httpx.MockTransport``.

Installed by swapping ``remembra.cloud.billing_paddle.http_client_factory``, so
the real ``PaddleBillingManager._request`` (URL building, auth header,
``raise_for_status``, JSON parsing) runs on every call. It keeps just enough
state for the account and Founding flows: subscriptions and their status, the
customer each belongs to, price status, and every request received.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from remembra.cloud import billing_paddle


@dataclass
class PaddleMock:
    subscriptions: dict[str, dict[str, str]] = field(default_factory=dict)  # id -> {status, customer_id}
    prices: dict[str, str] = field(default_factory=dict)  # id -> status
    customers_by_email: dict[str, str] = field(default_factory=dict)
    calls: list[tuple[str, str, dict[str, str], Any]] = field(default_factory=list)
    # "METHOD /path" -> HTTP status to answer, or an exception to raise (a transport failure).
    failures: dict[str, Any] = field(default_factory=dict)
    transactions: int = 0

    def add_subscription(self, sub_id: str, customer_id: str, status: str = "active") -> None:
        self.subscriptions[sub_id] = {"status": status, "customer_id": customer_id}

    def requests(self, method: str | None = None, path_prefix: str = "") -> list[tuple[str, str, dict[str, str], Any]]:
        return [c for c in self.calls if (method is None or c[0] == method) and c[1].startswith(path_prefix)]

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, params, body))
        assert request.headers["authorization"].startswith("Bearer ")
        failure = self.failures.get(f"{method} {path}")
        if isinstance(failure, BaseException):
            raise failure
        if isinstance(failure, int):
            return httpx.Response(failure, json={"error": {"type": "api_error", "code": "injected"}})

        parts = path.strip("/").split("/")
        if method == "GET" and parts == ["subscriptions"]:
            wanted = set((params.get("status") or "").split(","))
            data = [
                {"id": sid, "status": s["status"]}
                for sid, s in self.subscriptions.items()
                if s["customer_id"] == params.get("customer_id") and s["status"] in wanted
            ]
            return httpx.Response(200, json={"data": data})
        if parts[:1] == ["subscriptions"] and len(parts) == 2 and method == "GET":
            sub = self.subscriptions.get(parts[1])
            if sub is None:
                return httpx.Response(404, json={"error": {"code": "entity_not_found"}})
            return httpx.Response(200, json={"data": {"id": parts[1], "status": sub["status"], "custom_data": {}}})
        if parts[:1] == ["subscriptions"] and parts[2:] == ["cancel"] and method == "POST":
            sub = self.subscriptions.get(parts[1])
            if sub is None:
                return httpx.Response(404, json={"error": {"code": "entity_not_found"}})
            if sub["status"] == "canceled":
                return httpx.Response(400, json={"error": {"code": "subscription_is_canceled_action_invalid"}})
            assert body == {"effective_from": "immediately"}
            sub["status"] = "canceled"
            return httpx.Response(200, json={"data": {"id": parts[1], "status": "canceled"}})
        if parts == ["transactions"] and method == "POST":
            self.transactions += 1
            txn = f"txn_mock_{self.transactions}"
            return httpx.Response(200, json={"data": {"id": txn, "checkout": {"url": f"https://pay.example/{txn}"}}})
        if parts[:1] == ["prices"] and len(parts) == 2 and method == "PATCH":
            self.prices[parts[1]] = str(body["status"])
            return httpx.Response(200, json={"data": {"id": parts[1], "status": body["status"]}})
        if parts == ["customers"] and method == "GET":
            cid = self.customers_by_email.get(params.get("email", ""))
            return httpx.Response(200, json={"data": [{"id": cid}] if cid else []})
        if parts[:1] == ["customers"] and parts[2:] == ["portal-sessions"] and method == "POST":
            url = f"https://portal.example/{parts[1]}"
            return httpx.Response(201, json={"data": {"urls": {"general": {"overview": url}}}})
        return httpx.Response(404, json={"error": {"code": "unexpected", "detail": f"{method} {path}"}})


def install(monkeypatch: pytest.MonkeyPatch, mock: PaddleMock | None = None) -> PaddleMock:
    mock = mock or PaddleMock()
    monkeypatch.setattr(
        billing_paddle, "http_client_factory", lambda: httpx.AsyncClient(transport=httpx.MockTransport(mock.handler))
    )
    return mock
