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
        self._client = httpx.AsyncClient(timeout=timeout)

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

        # Attempt delivery with retries
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._client.post(url, content=body, headers=headers)

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
                backoff = BACKOFF_BASE ** attempt
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
