"""A closing handoff the stdio MCP server could not deliver is kept and sent again, not dropped.

``close_session`` used to be one request: when the server was down or hung, the handoff was gone and
the agent could not tell. Now a close that failed for a reason that passes (no answer, a gateway
answer, a 5xx, a 429) is queued in the relay's outbox on this machine
(``~/.remembra/relay/outbox``, see :mod:`remembra.relay.outbox`: 0600 files, the body redacted like
the server redacts it, never a key or a crew token) and delivered by whoever gets there first:

* this MCP server, from a background thread, as soon as the server answers (:class:`Drainer`);
* the next Remembra MCP server this agent starts on this machine, on its first tool call;
* a ``remembra-relay`` hook (``brief`` / ``close``), when it is configured with the same key.

An entry records a one-way mark of the key it was queued with and is only ever sent with that key,
to the server it was queued for, as the agent that wrote it. The server keeps one handoff per
(agent, session) and ignores a close older than the one it has, so a second delivery changes nothing.

What a late delivery cannot do: end the crew session, unless it is this same MCP process that
delivers it (only it holds the session token). Otherwise the crew session ends by its lease.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from remembra.client.memory import Memory
from remembra.mcp.crew import CrewApiError, CrewHttp
from remembra.relay import outbox

# Seconds between delivery attempts of the background thread (about 20 minutes in all). A later tool
# call, MCP server or relay hook picks the entry up after that.
RETRY_DELAYS_S: tuple[float, ...] = (5.0, 10.0, 20.0, 30.0, *([60.0] * 19))

TokenFor = Callable[[str, str], str | None]
OnDelivered = Callable[[str, str, dict[str, Any]], None]


def is_retryable(status: int | None) -> bool:
    """A failed close worth sending again: no answer at all (``None`` / 0), a 5xx, or a 429."""
    return not status or status >= 500 or status == 429


def enqueue(home: Path, payload: dict[str, Any], client: Memory, *, error: str, http_status: int | None) -> Path | None:
    """Queue the close body for ``client``'s server and key. None when it could not be kept."""
    body = dict(payload)
    # when the session ended, so a handoff delivered late keeps its place in time (the server clamps it)
    body.setdefault("closed_at", datetime.now(UTC).isoformat())
    return outbox.enqueue(
        home,
        body,
        url=client.base_url,
        config_source=outbox.MCP_SOURCE,
        error=error,
        http_status=http_status,
        api_key=client.api_key,
        extra={"key_fp": outbox.key_mark(client.api_key)},
    )


def mine(entry: outbox.Entry, client: Memory) -> bool:
    """Whether ``client`` may deliver ``entry``: queued by an MCP server with this key, for this server and agent."""
    return bool(
        entry.data.get("config_source") == outbox.MCP_SOURCE
        and client.agent_id
        and entry.agent_id == client.agent_id
        and entry.data.get("key_fp")
        and entry.data.get("key_fp") == outbox.key_mark(client.api_key)
        and entry.url
        and entry.url == outbox.clean_url(client.base_url)
        and entry.data.get("last_status") != 403  # refused: kept for the owner, never sent again on its own
    )


def waiting(home: Path, client: Memory) -> list[outbox.Entry]:
    try:
        return [e for e in outbox.pending(home) if mine(e, client)]
    except Exception:
        return []


def drain(
    home: Path,
    client: Memory,
    *,
    token_for: TokenFor | None = None,
    on_delivered: OnDelivered | None = None,
    timeout: float | None = None,
) -> tuple[int, int]:
    """Send the queued closes ``client`` may deliver, oldest first. ``(delivered, still waiting)``. Never raises.

    ``timeout``: seconds to wait for each send instead of the client's own (a caller that has an agent
    waiting on it passes a short one).

    Stops at the first entry the server does not take for a reason that passes (it is still down). An
    entry the server rejects for good (400, 404, 413, 422) is dropped and logged: sending the same
    body again cannot succeed. ``token_for(agent, session)`` supplies the crew session token when this
    process still holds it, so the delivery also ends the crew session.
    """
    delivered = 0
    entries = waiting(home, client)
    for index, entry in enumerate(entries):
        claimed = outbox.claim(entry)
        if claimed is None:
            continue  # another process is sending it right now
        token = None
        if token_for is not None:
            try:
                token = token_for(entry.agent_id, entry.session_id)
            except Exception:
                token = None
        try:
            _, body, _ = CrewHttp(client).call("POST", "/session/close", token=token, json=entry.payload, timeout=timeout)
        except CrewApiError as e:
            status = e.status or None
            detail = f"HTTP {e.status}: {e.message}" if e.status else e.message
            if not is_retryable(status) and status not in outbox.RETRY_STATUS and e.error != "route_missing":
                outbox.finish(entry, claimed, sent=True)
                outbox.log(home, f"outbox: dropped {entry.agent_id} session {entry.session_id[:40]}: {detail}")
                continue
            outbox.finish(entry, claimed, sent=False, error=detail, http_status=status, api_key=client.api_key)
            if status in (401, 403):
                continue  # this entry is refused; the others may still go
            return delivered, len(entries) - index
        except Exception as e:  # never lose the entry to a bug in the send
            outbox.finish(entry, claimed, sent=False, error=e.__class__.__name__, api_key=client.api_key)
            return delivered, len(entries) - index
        outbox.finish(entry, claimed, sent=True)
        outbox.log(
            home,
            f"outbox: delivered {entry.agent_id} session {entry.session_id[:40]} "
            f"(queued {entry.data.get('queued_at')}, by the MCP server)",
        )
        delivered += 1
        if on_delivered is not None:
            try:
                on_delivered(entry.agent_id, entry.session_id, dict(body or {}))
            except Exception:
                pass
    return delivered, len(waiting(home, client))


class Drainer:
    """One background thread per MCP process that keeps trying until the queue is empty or it gives up."""

    def __init__(self, delays: Sequence[float] = RETRY_DELAYS_S) -> None:
        self.delays = tuple(delays)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def kick(
        self,
        home: Path,
        client: Memory,
        *,
        token_for: TokenFor | None = None,
        on_delivered: OnDelivered | None = None,
        now: bool = False,
    ) -> bool:
        """Start the thread unless it runs already. ``now``: try at once (entries found at startup),
        otherwise wait the first delay (the close just failed, the server is still down)."""
        with self._lock:
            if self.running():
                return False
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._run,
                args=(home, client, token_for, on_delivered, now),
                name="remembra-close-queue",
                daemon=True,
            )
            self._thread.start()
            return True

    def _run(self, home: Path, client: Memory, token_for: TokenFor | None, on_delivered: OnDelivered | None, now: bool) -> None:
        delays = ((0.0, *self.delays) if now else self.delays) or (0.0,)
        for delay in delays:
            if delay and self._stop.wait(delay):
                return
            _, left = drain(home, client, token_for=token_for, on_delivered=on_delivered)
            if not left:
                return

    def join(self, timeout: float | None = None) -> None:
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def stop(self) -> None:
        self._stop.set()


def has_entries(home: Path) -> bool:
    """A cheap look (one directory listing) before anything else is done on a tool call."""
    try:
        return any(outbox.outbox_dir(home).glob("*.json"))
    except OSError:
        return False
