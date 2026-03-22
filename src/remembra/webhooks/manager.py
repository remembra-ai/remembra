"""
Webhook registration and dispatch manager.

Stores webhook registrations in SQLite, dispatches events
via HTTP POST with retry logic.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse
from uuid import uuid4

from remembra.webhooks.events import ALL_EVENT_TYPES, WebhookEvent

logger = logging.getLogger(__name__)


# Blocked IP ranges for SSRF protection
_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),      # Loopback
    ipaddress.ip_network("10.0.0.0/8"),       # Private Class A
    ipaddress.ip_network("172.16.0.0/12"),    # Private Class B
    ipaddress.ip_network("192.168.0.0/16"),   # Private Class C
    ipaddress.ip_network("169.254.0.0/16"),   # Link-local
    ipaddress.ip_network("::1/128"),          # IPv6 loopback
    ipaddress.ip_network("fc00::/7"),         # IPv6 private
    ipaddress.ip_network("fe80::/10"),        # IPv6 link-local
]


def validate_webhook_url(url: str) -> tuple[bool, str | None]:
    """
    Validate a webhook URL for SSRF protection.
    
    Returns (is_valid, error_message).
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL format"
    
    # Only allow HTTP/HTTPS
    if parsed.scheme not in ("http", "https"):
        return False, "Only HTTP and HTTPS URLs are allowed"
    
    # Must have a hostname
    if not parsed.hostname:
        return False, "URL must have a hostname"
    
    hostname = parsed.hostname.lower()
    
    # Block localhost variants
    if hostname in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return False, "Localhost URLs are not allowed"
    
    # Block cloud metadata endpoints
    if hostname in ("169.254.169.254", "metadata.google.internal"):
        return False, "Cloud metadata endpoints are not allowed"
    
    # Resolve hostname and check IP
    try:
        # Get all IP addresses for the hostname
        ips = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        
        for _family, _, _, _, sockaddr in ips:
            ip_str = sockaddr[0]
            try:
                ip = ipaddress.ip_address(ip_str)
                for blocked in _BLOCKED_NETWORKS:
                    if ip in blocked:
                        return False, f"URL resolves to blocked IP range ({blocked})"
            except ValueError:
                continue
                
    except socket.gaierror:
        # Can't resolve - might be a temporary DNS issue, allow it
        # The actual delivery will fail if the host doesn't exist
        logger.warning("Could not resolve hostname for validation: %s", hostname)
    
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
        is_valid, error = validate_webhook_url(url)
        if not is_valid:
            raise ValueError(f"Invalid webhook URL: {error}")
        
        # Validate event types
        for event in events:
            if event != "*" and event not in ALL_EVENT_TYPES:
                raise ValueError(
                    f"Unknown event type: {event}. "
                    f"Valid types: {', '.join(ALL_EVENT_TYPES)} or '*'"
                )

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

        logger.info("Webhook registered: id=%s user=%s url=%s events=%s", webhook_id, user_id, url, events_str)

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
        deleted = cursor.rowcount > 0
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
