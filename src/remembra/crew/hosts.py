"""Crew hosts: one registered ``remembra-crewd`` per machine (spec §2 "Host", §6 Hosts, §10.1).

A host is registered once with ``POST /crew/hosts/register`` and receives a
**host token**. Only its SHA-256 hash is stored (``crew_hosts.token_hash``);
the raw token is returned exactly once and is never cached (§4.3, §11).

The host token authenticates:

* the batched heartbeat (``POST /crew/heartbeat``), whose rate limit is keyed
  on it (§11.2);
* binding a session to the host at ``join`` (a session is only bound to a host
  whose token the caller presents, so nobody can attach a session to another
  machine's liveness);
* token rotation of a session on re-join (§4.3: "rotates the token when called
  by crewd with the host token of the host that owns the session").

Host liveness is the server receipt time of its last heartbeat
(``last_seen_at``). The reaper (``crew/reaper.py``) marks a host that has been
silent for more than 3 minutes ``unreachable`` and emits ``host.unreachable``
into every crew that has live sessions on it (host-wide silence rule, §10.1);
the next heartbeat brings it back (``host.recovered``).

Hosts are per user, not per crew, so ``host.registered`` is emitted into a
crew the first time a session from that host joins it (``crew/sessions.py``).
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import aiosqlite

from remembra.crew import schemas
from remembra.crew.store import new_id

HOST_TOKEN_HEADER: Final = "X-Remembra-Host-Token"
HOST_TOKEN_PREFIX: Final = "rch_"
HOST_LABEL_RE: Final = re.compile(r"[a-z0-9]{2,32}")
# A user with more live hosts than this is refused a new registration (409 host_cap): each
# crewd registers once, so this only stops a runaway client from flooding the table.
MAX_LIVE_HOSTS_PER_USER: Final = 50
# Host-wide silence (§10.1): a host whose last heartbeat is older than this is unreachable.
HOST_SILENT_AFTER_S: Final = 180


class HostError(Exception):
    """Base class for host errors (the router maps ``code`` to an HTTP status)."""

    status: int = 400
    code: str = "host_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class HostCapReached(HostError):
    status = 409
    code = "host_cap"


class HostAuthFailed(HostError):
    status = 401
    code = "host_token_invalid"


def hash_token(token: str) -> str:
    """SHA-256 hex of a host or session token (what the database stores)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens_match(token: str | None, token_hash: str | None) -> bool:
    """Constant-time check of a presented token against a stored hash."""
    if not token or not token_hash:
        return False
    return hmac.compare_digest(hash_token(token), token_hash)


def new_host_token() -> str:
    return HOST_TOKEN_PREFIX + secrets.token_urlsafe(32)


def host_view(row: dict[str, Any]) -> dict[str, Any]:
    """``schemas.HOST_VIEW`` for a ``crew_hosts`` row (never the token hash)."""
    return {
        "id": row["id"],
        "host_label": row["host_label"],
        "platform": row.get("platform"),
        "crewd_version": row.get("crewd_version"),
        "state": row["state"],
    }


@dataclass(frozen=True)
class RegisteredHost:
    row: dict[str, Any]
    token: str  # returned once, never stored


async def _fetchone(conn: aiosqlite.Connection, sql: str, params: tuple[Any, ...]) -> dict[str, Any] | None:
    if conn.row_factory is aiosqlite.Row:
        rows = list(await conn.execute_fetchall(sql, params))
        return dict(zip(rows[0].keys(), tuple(rows[0]), strict=True)) if rows else None
    async with conn.execute(sql, params) as cur:
        row = await cur.fetchone()
        if row is None:
            return None
        names = [d[0] for d in cur.description]
        return dict(zip(names, tuple(row), strict=True))


async def get_host(conn: aiosqlite.Connection, host_id: str) -> dict[str, Any] | None:
    if not schemas.is_id("host", host_id):
        return None
    return await _fetchone(conn, "SELECT * FROM crew_hosts WHERE id = ?", (host_id,))


def validate_registration(host_label: Any, platform: Any, crewd_version: Any) -> list[str]:
    body = {"host_label": host_label, "platform": platform, "crewd_version": crewd_version}
    return schemas.validate(body, schemas.REQUEST_SHAPES["HostRegister"])


async def register_host(
    db: Any,
    *,
    user_id: str,
    host_label: str,
    platform: str,
    crewd_version: str,
    now: str,
) -> RegisteredHost:
    """Create a host for ``user_id`` and return it with its one-time token.

    ``host_label`` must be crewd's salted label (``[a-z0-9]{2,32}``), never the
    raw hostname. Raises :class:`HostCapReached` past
    :data:`MAX_LIVE_HOSTS_PER_USER` live (non-retired) hosts.
    """
    errors = validate_registration(host_label, platform, crewd_version)
    if errors:
        raise ValueError("; ".join(errors))
    token = new_host_token()
    host_id = new_id("host")
    async with db.transaction():
        count = await _fetchone(
            db.conn, "SELECT COUNT(*) AS n FROM crew_hosts WHERE user_id = ? AND state != 'retired'", (user_id,)
        )
        if count is not None and int(count["n"]) >= MAX_LIVE_HOSTS_PER_USER:
            raise HostCapReached(f"at most {MAX_LIVE_HOSTS_PER_USER} registered hosts per account; retire unused ones")
        await db.conn.execute(
            """INSERT INTO crew_hosts (id, user_id, host_label, token_hash, platform, crewd_version, state,
                                       last_seen_at, registered_at)
               VALUES (?, ?, ?, ?, ?, ?, 'online', ?, ?)""",
            (host_id, user_id, host_label, hash_token(token), platform, crewd_version, now, now),
        )
        row = await get_host(db.conn, host_id)
    assert row is not None
    return RegisteredHost(row=row, token=token)


async def authenticate_host(
    conn: aiosqlite.Connection, *, user_id: str, host_id: str | None, token: str | None
) -> dict[str, Any]:
    """The host row when ``token`` is the current token of ``host_id`` owned by ``user_id``.

    Raises :class:`HostAuthFailed` for an unknown host, another user's host, a
    retired host or a wrong token (one answer for all, so it is not an oracle).
    """
    if not token or not host_id:
        raise HostAuthFailed("a valid host token is required")
    row = await get_host(conn, host_id)
    if row is None or row["user_id"] != user_id or row["state"] == "retired" or not tokens_match(token, row["token_hash"]):
        raise HostAuthFailed("the host token is not valid for this host")
    return row


async def authenticate_host_token(conn: aiosqlite.Connection, *, user_id: str, token: str | None) -> dict[str, Any]:
    """The host row for a bare host token (the heartbeat carries no host id)."""
    if not token:
        raise HostAuthFailed("a valid host token is required")
    row = await _fetchone(
        conn, "SELECT * FROM crew_hosts WHERE token_hash = ? AND user_id = ? AND state != 'retired'", (hash_token(token), user_id)
    )
    if row is None:
        raise HostAuthFailed("the host token is not valid")
    return row


async def rotate_host_token(db: Any, *, user_id: str, host_id: str, token: str | None) -> RegisteredHost:
    """Replace the host token (the current token must be presented). The old token stops working at once."""
    new_token = new_host_token()
    async with db.transaction():
        await authenticate_host(db.conn, user_id=user_id, host_id=host_id, token=token)
        await db.conn.execute("UPDATE crew_hosts SET token_hash = ? WHERE id = ?", (hash_token(new_token), host_id))
        row = await get_host(db.conn, host_id)
    assert row is not None
    return RegisteredHost(row=row, token=new_token)


async def mark_seen(conn: aiosqlite.Connection, host_id: str, now: str) -> str:
    """Stamp a heartbeat's server receipt time. Returns the host's state *before* this heartbeat."""
    row = await _fetchone(conn, "SELECT state FROM crew_hosts WHERE id = ?", (host_id,))
    previous = str(row["state"]) if row else "online"
    await conn.execute(
        "UPDATE crew_hosts SET last_seen_at = ?, state = CASE WHEN state = 'retired' THEN state ELSE 'online' END WHERE id = ?",
        (now, host_id),
    )
    return previous


def seconds_between(later: datetime, earlier: datetime) -> int:
    return max(0, int((later - earlier).total_seconds()))
