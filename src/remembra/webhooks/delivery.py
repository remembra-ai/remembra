"""
HTTP webhook delivery with retry and HMAC signing.

Delivers webhook events via HTTP POST with:
- HMAC-SHA256 signature in X-Remembra-Signature header
- Exponential backoff retry (3 attempts)
- Configurable timeout
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0  # seconds
MAX_RETRIES = 3
BACKOFF_BASE = 2  # seconds


class WebhookDelivery:
    """HTTP delivery engine for webhook events.

    Args:
        timeout: Request timeout in seconds.
        max_retries: Maximum delivery attempts.
        user_agent: User-Agent header value.
    """

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        max_retries: int = MAX_RETRIES,
        user_agent: str = "Remembra-Webhook/1.0",
    ) -> None:
        self._timeout = timeout
        self._max_retries = max_retries
        self._user_agent = user_agent
        # Redirects are never followed: a 30x to an internal host is an SSRF vector.
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)

    async def deliver(
        self,
        url: str,
        payload: dict[str, Any],
        secret: str | None = None,
        delivery_id: str | None = None,
    ) -> bool:
        """Deliver a webhook payload to a URL.

        Args:
            url: Target URL.
            payload: JSON payload to send.
            secret: Optional HMAC signing secret.
            delivery_id: Delivery ID for logging.

        Returns:
            True if delivery succeeded (2xx response), False otherwise.
        """
        body = json.dumps(payload, default=str)
        headers = {
            "Content-Type": "application/json",
            "User-Agent": self._user_agent,
            "X-Remembra-Event": payload.get("type", "unknown"),
            "X-Remembra-Delivery": delivery_id or "",
        }

        # HMAC signature if secret is provided
        if secret:
            signature = hmac.new(
                secret.encode("utf-8"),
                body.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()
            headers["X-Remembra-Signature"] = f"sha256={signature}"

        # SSRF: re-validate at delivery time and connect to the *validated* IP,
        # so a DNS answer that changes after registration (rebinding) or a URL
        # stored before validation existed can never reach an internal address.
        from remembra.webhooks.manager import resolve_webhook_target

        try:
            target = await resolve_webhook_target(url)
        except ValueError as e:
            logger.warning("Webhook delivery blocked (SSRF policy): delivery=%s reason=%s", delivery_id, e)
            return False
        pinned_url, request_kwargs = _pinned_request(target, headers)

        # Attempt delivery with retries
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._client.post(pinned_url, content=body, **request_kwargs)

                if 200 <= response.status_code < 300:
                    logger.info(
                        "Webhook delivered: url=%s status=%d delivery=%s",
                        url,
                        response.status_code,
                        delivery_id,
                    )
                    return True

                logger.warning(
                    "Webhook delivery failed: url=%s status=%d attempt=%d/%d",
                    url,
                    response.status_code,
                    attempt,
                    self._max_retries,
                )

            except Exception as e:
                last_error = e
                logger.warning(
                    "Webhook delivery error: url=%s error=%s attempt=%d/%d",
                    url,
                    str(e),
                    attempt,
                    self._max_retries,
                )

            # Exponential backoff (skip on last attempt)
            if attempt < self._max_retries:
                import asyncio

                backoff = BACKOFF_BASE**attempt
                await asyncio.sleep(backoff)

        logger.error(
            "Webhook delivery exhausted retries: url=%s delivery=%s last_error=%s",
            url,
            delivery_id,
            str(last_error),
        )
        return False

    async def close(self) -> None:
        """Close the persistent HTTP client."""
        await self._client.aclose()

    @staticmethod
    def verify_signature(
        payload: bytes,
        signature: str,
        secret: str,
    ) -> bool:
        """Verify a webhook signature (for consumers to validate).

        Args:
            payload: Raw request body bytes.
            signature: X-Remembra-Signature header value.
            secret: Shared secret.

        Returns:
            True if signature is valid.
        """
        if not signature.startswith("sha256="):
            return False

        expected = hmac.new(
            secret.encode("utf-8"),
            payload,
            hashlib.sha256,
        ).hexdigest()

        provided = signature[7:]  # Strip "sha256=" prefix
        return hmac.compare_digest(expected, provided)


def _pinned_request(target: Any, headers: dict[str, str]) -> tuple[str, dict[str, Any]]:
    """Build a request to the validated IP while preserving Host/SNI for the real hostname."""
    ip = target.ips[0]
    parts = urlsplit(target.url)
    host_for_url = f"[{ip}]" if ":" in ip else ip
    default_port = 443 if target.scheme == "https" else 80
    netloc = host_for_url if target.port == default_port else f"{host_for_url}:{target.port}"
    pinned_url = urlunsplit((parts.scheme, netloc, parts.path or "/", parts.query, ""))
    host_header = target.hostname if target.port == default_port else f"{target.hostname}:{target.port}"
    request_headers = {**headers, "Host": host_header}
    kwargs: dict[str, Any] = {"headers": request_headers}
    if target.scheme == "https":
        # TLS still verifies the certificate against the real hostname.
        kwargs["extensions"] = {"sni_hostname": target.hostname}
    return pinned_url, kwargs
