"""
Webhook registration and dispatch manager.

Stores webhook registrations in SQLite, dispatches events
via HTTP POST with retry logic.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from remembra.webhooks.events import ALL_EVENT_TYPES, WebhookEvent

logger = logging.getLogger(__name__)


_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_NAT64_LOCAL = ipaddress.ip_network("64:ff9b:1::/48")

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


def is_public_ip(ip: IPAddress) -> bool:
    """True only for globally routable unicast addresses.

    Unwraps IPv4-mapped, NAT64, 6to4 and Teredo IPv6 forms so an internal IPv4
    target cannot be smuggled inside an IPv6 literal. Blocks loopback, RFC1918,
    link-local (incl. cloud metadata 169.254.169.254), CGNAT 100.64/10,
    0.0.0.0/8, multicast, reserved and unspecified addresses.
    """
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        elif ip in _NAT64:
            ip = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        elif ip in _NAT64_LOCAL:
            return False
        elif ip.sixtofour is not None:
            ip = ip.sixtofour
        elif ip.teredo is not None:
            return False
    return bool(
        ip.is_global
        and not ip.is_multicast
        and not ip.is_reserved
        and not ip.is_loopback
        and not ip.is_link_local
        and not ip.is_private
        and not ip.is_unspecified
    )


async def _resolve(hostname: str, port: int) -> list[str]:
    """Resolve ``hostname`` to IP strings (patched in tests; no network there)."""
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


@dataclass(frozen=True)
class ResolvedTarget:
    url: str
    scheme: str
    hostname: str
    port: int
    ips: tuple[str, ...]


async def resolve_webhook_target(url: str) -> ResolvedTarget:
    """Validate a webhook URL and resolve it to public IPs (deny by default).

    Raises ``ValueError`` when the URL is malformed, not http(s), does not
    resolve, or resolves to *any* non-public address. Callers must connect to
    one of the returned IPs (not re-resolve) so DNS rebinding cannot swap in an
    internal address between check and use.
    """
    try:
        parsed = urlparse(url)
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        raise ValueError("Invalid URL format") from None
    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only HTTP and HTTPS URLs are allowed")
    if not parsed.hostname:
        raise ValueError("URL must have a hostname")
    hostname = parsed.hostname.lower().rstrip(".")
    if hostname == "localhost" or hostname.endswith((".localhost", ".internal", ".local")):
        raise ValueError("Internal hostnames are not allowed")

    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        candidates = [str(literal)]
    else:
        try:
            candidates = await _resolve(hostname, port)
        except (OSError, UnicodeError):
            raise ValueError("Hostname could not be resolved") from None
    if not candidates:
        raise ValueError("Hostname could not be resolved")

    ips: list[str] = []
    for candidate in candidates:
        try:
            ip = ipaddress.ip_address(candidate.split("%", 1)[0])
        except ValueError:
            raise ValueError("Hostname resolved to an invalid address") from None
        if not is_public_ip(ip):
            raise ValueError("URL resolves to a non-public address")
        ips.append(str(ip))
    return ResolvedTarget(url=url, scheme=parsed.scheme, hostname=hostname, port=port, ips=tuple(dict.fromkeys(ips)))


async def validate_webhook_url(url: str) -> tuple[bool, str | None]:
    """Validate a webhook URL for SSRF protection. Returns (is_valid, error_message)."""
    try:
        await resolve_webhook_target(url)
    except ValueError as e:
        return False, str(e)
    return True, None


class WebhookManager:
    """Manages webhook registrations and event dispatch.

    Args:
        db: The application's Database instance.
        delivery: Optional WebhookDelivery for HTTP dispatch.
    """

    def __init__(self, db: Any, delivery: Any | None = None) -> None:
        self._db = db
        self._delivery = delivery

    async def init_schema(self) -> None:
        """Create webhook tables if they don't exist."""
        await self._db.conn.executescript("""
            CREATE TABLE IF NOT EXISTS webhooks (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                url TEXT NOT NULL,
                events TEXT NOT NULL,
                secret TEXT,
                active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_webhooks_user
                ON webhooks(user_id);

            CREATE TABLE IF NOT EXISTS webhook_deliveries (
                id TEXT PRIMARY KEY,
                webhook_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER DEFAULT 0,
                last_attempt_at TEXT,
                response_status INTEGER,
                response_body TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (webhook_id) REFERENCES webhooks(id)
            );

            CREATE INDEX IF NOT EXISTS idx_deliveries_webhook
                ON webhook_deliveries(webhook_id);

            CREATE INDEX IF NOT EXISTS idx_deliveries_status
                ON webhook_deliveries(status);
        """)
        await self._db.conn.commit()

    # -----------------------------------------------------------------------
    # Registration CRUD
    # -----------------------------------------------------------------------

    async def register(
        self,
        user_id: str,
        url: str,
        events: list[str],
        secret: str | None = None,
    ) -> dict[str, Any]:
        """Register a new webhook endpoint.

        Args:
            user_id: Owner user ID.
            url: Target URL for HTTP POST deliveries.
            events: List of event types to subscribe to.
            secret: Optional signing secret for payload verification.

        Returns:
            Webhook registration record.

        Raises:
            ValueError: If URL is invalid or points to blocked network.
        """
        # Validate URL for SSRF protection
        is_valid, error = await validate_webhook_url(url)
        if not is_valid:
            raise ValueError(f"Invalid webhook URL: {error}")

        # Validate event types
        for event in events:
            if event != "*" and event not in ALL_EVENT_TYPES:
                raise ValueError(f"Unknown event type: {event}. Valid types: {', '.join(ALL_EVENT_TYPES)} or '*'")

        webhook_id = str(uuid4())
        now = datetime.now(UTC).isoformat()
        events_str = ",".join(events)

        await self._db.conn.execute(
            """
            INSERT INTO webhooks (id, user_id, url, events, secret, active, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (webhook_id, user_id, url, events_str, secret, now, now),
        )
        await self._db.conn.commit()

        logger.info("Webhook registered: id=%s user=%s events=%s", webhook_id, user_id, events_str)

        return {
            "id": webhook_id,
            "user_id": user_id,
            "url": url,
            "events": events,
            "active": True,
            "created_at": now,
        }

    async def list_webhooks(self, user_id: str) -> list[dict[str, Any]]:
        """List all webhooks for a user."""
        cursor = await self._db.conn.execute(
            "SELECT id, url, events, active, created_at, updated_at FROM webhooks WHERE user_id = ?",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "url": row[1],
                "events": row[2].split(",") if row[2] else [],
                "active": bool(row[3]),
                "created_at": row[4],
                "updated_at": row[5],
            }
            for row in rows
        ]

    async def get_webhook(self, webhook_id: str, user_id: str) -> dict[str, Any] | None:
        """Get a specific webhook by ID (with user ownership check)."""
        cursor = await self._db.conn.execute(
            "SELECT id, url, events, secret, active, created_at, updated_at FROM webhooks WHERE id = ? AND user_id = ?",
            (webhook_id, user_id),
        )
        row = await cursor.fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "url": row[1],
            "events": row[2].split(",") if row[2] else [],
            "has_secret": bool(row[3]),
            "active": bool(row[4]),
            "created_at": row[5],
            "updated_at": row[6],
        }

    async def delete_webhook(self, webhook_id: str, user_id: str) -> bool:
        """Delete a webhook registration."""
        cursor = await self._db.conn.execute(
            "DELETE FROM webhooks WHERE id = ? AND user_id = ?",
            (webhook_id, user_id),
        )
        await self._db.conn.commit()
        deleted: bool = cursor.rowcount > 0
        if deleted:
            logger.info("Webhook deleted: id=%s user=%s", webhook_id, user_id)
        return deleted

    async def update_webhook(
        self,
        webhook_id: str,
        user_id: str,
        url: str | None = None,
        events: list[str] | None = None,
        active: bool | None = None,
    ) -> dict[str, Any] | None:
        """Update a webhook registration."""
        existing = await self.get_webhook(webhook_id, user_id)
        if existing is None:
            return None

        now = datetime.now(UTC).isoformat()
        updates: list[str] = ["updated_at = ?"]
        params: list[Any] = [now]

        if url is not None:
            # Same SSRF validation as registration (previously skipped on update).
            is_valid, error = await validate_webhook_url(url)
            if not is_valid:
                raise ValueError(f"Invalid webhook URL: {error}")
            updates.append("url = ?")
            params.append(url)

        if events is not None:
            for event in events:
                if event != "*" and event not in ALL_EVENT_TYPES:
                    raise ValueError(f"Unknown event type: {event}")
            updates.append("events = ?")
            params.append(",".join(events))

        if active is not None:
            updates.append("active = ?")
            params.append(int(active))

        params.extend([webhook_id, user_id])

        await self._db.conn.execute(
            f"UPDATE webhooks SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
            params,
        )
        await self._db.conn.commit()

        return await self.get_webhook(webhook_id, user_id)

    # -----------------------------------------------------------------------
    # Event dispatch
    # -----------------------------------------------------------------------

    async def dispatch(self, event: WebhookEvent) -> int:
        """Dispatch an event to all matching webhook registrations.

        Returns the number of deliveries queued.
        """
        # Find matching webhooks
        cursor = await self._db.conn.execute(
            "SELECT id, url, secret, events FROM webhooks WHERE user_id = ? AND active = 1",
            (event.user_id,),
        )
        rows = await cursor.fetchall()

        queued = 0
        for row in rows:
            webhook_id = row[0]
            url = row[1]
            secret = row[2]
            subscribed_events = row[3].split(",") if row[3] else []

            # Check if this webhook subscribes to this event type
            if "*" not in subscribed_events and event.type not in subscribed_events:
                continue

            # Create delivery record
            delivery_id = str(uuid4())
            now = datetime.now(UTC).isoformat()
            await self._db.conn.execute(
                """
                INSERT INTO webhook_deliveries
                    (id, webhook_id, event_id, event_type, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (delivery_id, webhook_id, event.id, event.type, now),
            )

            # Deliver immediately if delivery service is available
            if self._delivery is not None:
                try:
                    success = await self._delivery.deliver(
                        url=url,
                        payload=event.to_dict(),
                        secret=secret,
                        delivery_id=delivery_id,
                    )
                    status = "delivered" if success else "failed"
                except Exception as e:
                    logger.warning("Webhook delivery failed: %s", e)
                    status = "failed"

                await self._db.conn.execute(
                    """
                    UPDATE webhook_deliveries
                    SET status = ?, attempts = 1, last_attempt_at = ?
                    WHERE id = ?
                    """,
                    (status, now, delivery_id),
                )

            queued += 1

        if queued > 0:
            await self._db.conn.commit()

        return queued

    async def get_deliveries(
        self,
        webhook_id: str,
        user_id: str,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """Get recent deliveries for a webhook."""
        # Verify ownership
        existing = await self.get_webhook(webhook_id, user_id)
        if existing is None:
            return []

        cursor = await self._db.conn.execute(
            """
            SELECT id, event_id, event_type, status, attempts,
                   last_attempt_at, response_status, created_at
            FROM webhook_deliveries
            WHERE webhook_id = ?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (webhook_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            {
                "id": row[0],
                "event_id": row[1],
                "event_type": row[2],
                "status": row[3],
                "attempts": row[4],
                "last_attempt_at": row[5],
                "response_status": row[6],
                "created_at": row[7],
            }
            for row in rows
        ]
