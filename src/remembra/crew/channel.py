"""Crew channel: messages, threads, verified mention routing, edits, redaction and long-poll replies (spec §5.7).

Who writes
    A message is written by a **crew session** (authenticated by its session
    token, :func:`authenticate_session`) or by a **human** (dashboard JWT). The
    author, callsign, agent id and the key-verified flag always come from the
    credential, never from the request body. Sender names ``mani``, ``human``,
    ``system`` and ``remembra`` and the kind ``system`` are server-set only
    (§5.8 reserved senders): an agent gets 422.

Threads and references
    ``thread_root_id``, ``reply_to_id`` and every ``refs`` id must belong to the
    same crew (422 otherwise, no existence oracle). A reply given only
    ``reply_to_id`` joins that message's thread.

Mentions (§5.7 table)
    ``@codex-1`` → that live session's queue; ``@codex`` → the live sessions of
    agent ``codex`` (a self-declared session named ``codex`` receives it labelled
    "addressed to codex, you are self-declared"), or the agent inbox when none is
    live (through the outbox, D35); ``@mani`` / ``@human`` → Needs-you, coalesced
    by (session, kind) and capped per session per hour; ``@crew`` → every live
    session plus the crew inbox; ``@zone:pos`` → holders of claims on that zone;
    ``@task:T-14`` → the task's owner. Tokens that match nothing are returned as
    ``unresolved``; nothing is created for them.

Delivery
    Mentions become session-queue items; hooks and MCP calls deliver them at
    the next turn (never mid-turn for agent-authored text, D13), and
    ``POST /crews/{id}/messages`` with ``wait_s`` long-polls for the first reply
    in the thread (:meth:`CrewChannel.wait_for_reply`).

Edits and redaction
    The author may edit for 10 minutes; every previous body is kept in
    ``crew_message_edits`` and the edit event says whether the message had
    already been delivered to a recipient. A human may redact: the body is
    removed (edit history too) and ``redacted_body_hash`` keeps its sha256.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from remembra.crew import schemas
from remembra.crew.bus import CrewBus
from remembra.crew.decisions import CrewDecisions, CrewRef, decision_api, title_from_body
from remembra.crew.events import CrewEventLog, EventTx
from remembra.crew.inbox import (
    Author,
    CrewInbox,
    InboxError,
    NotAllowed,
    ValidationFailed,
    ref_kind,
    require_same_crew,
)
from remembra.crew.store import CrewStore, new_id, now_iso, parse_iso

SESSION_HEADER: Final = "X-Remembra-Crew-Session"  # carries the session token (same header on every crew route)
RESERVED_SENDER_NAMES: Final = frozenset({"mani", "human", "system", "remembra"})
RESERVED_KINDS: Final = frozenset({"override", "pause"})
SERVER_ONLY_MESSAGE_KINDS: Final = frozenset({"system"})
HUMAN_ALIASES: Final = frozenset({"mani", "human"})
EDIT_WINDOW: Final = timedelta(minutes=10)
MAX_MENTIONS: Final = 20
MAX_REFS: Final = 20
MAX_CLIENT_MSG_ID: Final = 64
REPLY_DATA_CLIP: Final = 1000
TRUST_COLLAPSE_BELOW: Final = 0.5
KIND_AGENT_INBOX_SEND: Final = "agent_inbox_send"
LIVE_SESSION_STATES: Final = schemas.LIVE_PRESENCE_STATES

MENTION_RE: Final = re.compile(r"(?<![A-Za-z0-9_@/.\\-])@([A-Za-z0-9][A-Za-z0-9._:-]{0,79})")
CLIENT_MSG_ID_RE: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}")
TASK_REF_RE: Final = re.compile(r"t-([1-9][0-9]{0,6})")


class SessionAuthError(InboxError):
    status = 401
    error = "session_auth"


class MessageNotFound(InboxError):
    status = 404
    error = "not_found"


class MessageConflict(InboxError):
    status = 409
    error = "message_conflict"


class ReservedSender(InboxError):
    status = 422
    error = "reserved_sender"


def is_reserved_sender(name: str | None) -> bool:
    """The agent-inbox rule (look-alikes, zero-width characters and punctuation included, §5.8)."""
    from remembra.inbox.manager import is_reserved_sender as _reserved

    return _reserved(name)


def token_hash(token: str) -> str:
    """How a session token is stored (``crew_sessions.token_hash``): sha256 hex, never the raw token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def authenticate_session(
    conn: Any, crew_id: str, user_id: str, token: str | None, *, agent_id: str | None = None
) -> Author:
    """The :class:`Author` for a crew session proven by its token; 401 on any mismatch.

    The token (``X-Remembra-Crew-Session``) is looked up by its sha256
    (``crew_sessions.token_hash``). The session must belong to ``crew_id`` and
    to the calling user and must not be ``ended``. An agent-scoped key
    (``agent_id``) may only act as sessions of its own agent.
    """
    if not token or len(token) > 512:
        raise SessionAuthError("a crew session token is required")
    async with conn.execute("SELECT * FROM crew_sessions WHERE token_hash = ?", (token_hash(token),)) as cur:
        found = await cur.fetchone()
    row = dict(found) if found is not None else None
    if (
        row is None
        or row["crew_id"] != crew_id
        or row["user_id"] != user_id
        or row["state"] == "ended"
        or not hmac.compare_digest(str(row["token_hash"]), token_hash(token))
        or (agent_id is not None and row["agent_id"] != agent_id)
    ):
        raise SessionAuthError("crew session not recognised")
    return Author.session(row)


# ---------------------------------------------------------------------------
# Views and parsing
# ---------------------------------------------------------------------------


def parse_mentions(body: str) -> list[str]:
    """``@tokens`` in order, lowercased, deduplicated, trailing punctuation dropped, at most 20."""
    out: list[str] = []
    for match in MENTION_RE.finditer(body):
        token = match.group(1).rstrip(".:-").lower()
        if token and token not in out:
            out.append(token)
        if len(out) >= MAX_MENTIONS:
            break
    return out


def _json_list(value: Any) -> list[str]:
    if not value:
        return []
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in loaded] if isinstance(loaded, list) else []


def message_view(row: Mapping[str, Any]) -> dict[str, Any]:
    """``MessageView`` (schemas) of a ``crew_messages`` row; the body is clipped to the event limit."""
    body = str(row["body"] or "")
    clipped = body[: schemas.MAX_MESSAGE_BODY_IN_EVENT]
    return {
        "id": row["id"],
        "seq": int(row["seq"] or 1),
        "thread_root_id": row["thread_root_id"],
        "reply_to_id": row["reply_to_id"],
        "kind": row["kind"],
        "author_kind": row["author_kind"],
        "author_session_id": row["author_session_id"],
        "author_agent_id": row["author_agent_id"],
        "author_verified": bool(row["author_verified"]),
        "body": clipped,
        "body_truncated": len(body) > len(clipped),
        "mentions": [m[:80] for m in _json_list(row["mentions"])][:MAX_MENTIONS],
        "refs": [r[:80] for r in _json_list(row["refs"])][:MAX_REFS],
        "edited": row["edited_at"] is not None,
        "redacted": bool(row["redacted"]),
        "pinned": bool(row["pinned"]),
    }


def message_api(row: Mapping[str, Any], *, callsign: str | None = None) -> dict[str, Any]:
    """The REST shape: the full body plus provenance labels and the trust heuristic."""
    out = message_view(row)
    out["body"] = str(row["body"] or "")
    out["body_truncated"] = False
    score = row["trust_score"]
    if row["author_kind"] == "human":
        label = "human"
    elif row["author_kind"] == "system":
        label = "system"
    else:
        label = f"agent {row['author_agent_id']} ({'key-verified' if row['author_verified'] else 'self-declared'})"
    out.update(
        crew_id=row["crew_id"],
        author_user_id=row["author_user_id"],
        author_callsign=callsign,
        author_label=label,
        trust_score=score,
        # Display heuristic only (§11.2): collapses obviously suspicious agent text, not a defence.
        collapsed=bool(row["author_kind"] == "agent" and score is not None and float(score) < TRUST_COLLAPSE_BELOW),
        client_msg_id=row["client_msg_id"],
        created_at=row["created_at"],
        edited_at=row["edited_at"],
    )
    return out


def reply_as_data(message: Mapping[str, Any]) -> str:
    """A reply rendered for an agent: agent text only inside the ``<remembra-data>`` block (§11.2),
    under the brief's trust policy (:func:`remembra.relay.handoff.police_item`)."""
    from remembra.relay.handoff import police_item
    from remembra.security.untrusted import DATA_PREAMBLE

    who = message.get("author_callsign") or message.get("author_label") or "someone"
    body = police_item(
        str(message.get("body") or ""),
        stored_trust=message.get("trust_score"),
        clip_body=lambda text: schemas.clip_item(text, REPLY_DATA_CLIP),
    )
    where = message.get("thread_root_id") or message.get("id")
    line = f"{schemas.clip_item(str(who), 60)} replied in {where} ({message.get('kind')}): {body}"
    return "\n".join([schemas.DATA_OPEN, DATA_PREAMBLE, line, schemas.DATA_CLOSE])


def _trust_score(body: str) -> float:
    """The brief's trust policy score (R-14: the injection score, lowered for hidden characters)."""
    from remembra.relay.handoff import assess_text

    return assess_text(body).trust


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


@dataclass
class Routing:
    sessions: list[str] = field(default_factory=list)
    self_declared: list[str] = field(default_factory=list)
    needs_you: dict[str, Any] | None = None
    crew_inbox: str | None = None
    agent_inbox: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions": self.sessions,
            "self_declared": self.self_declared,
            "needs_you": self.needs_you,
            "crew_inbox": self.crew_inbox,
            "agent_inbox": self.agent_inbox,
            "unresolved": self.unresolved,
        }


@dataclass(frozen=True)
class PostResult:
    message: dict[str, Any]
    seq: int
    replayed: bool
    routing: dict[str, Any]
    decision: dict[str, Any] | None = None
    reply: dict[str, Any] | None = None
    waited_s: float | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "message": self.message,
            "seq": self.seq,
            "replayed": self.replayed,
            "routing": self.routing,
            "decision": self.decision,
        }
        if self.waited_s is not None:
            out["reply"] = self.reply
            out["reply_text"] = reply_as_data(self.reply) if self.reply else "no reply yet"
            out["waited_s"] = round(self.waited_s, 3)
        return out


class CrewChannel:
    """Channel service bound to the event log (and optionally the bus, for long-poll wake-ups)."""

    def __init__(
        self,
        log: CrewEventLog,
        *,
        inbox: CrewInbox | None = None,
        decisions: CrewDecisions | None = None,
        bus: CrewBus | None = None,
        outbox_wake: Any = None,
    ) -> None:
        self.log = log
        self.inbox = inbox or CrewInbox(log)
        self.decisions = decisions or CrewDecisions(log, self.inbox)
        self.bus = bus if bus is not None else getattr(log, "bus", None)
        self._outbox_wake = outbox_wake

    @property
    def db(self) -> Any:
        return self.log.db

    # -- post ----------------------------------------------------------------------------------

    async def post(
        self,
        crew: CrewRef,
        author: Author,
        *,
        kind: str,
        body: str,
        client_msg_id: str,
        thread_root_id: str | None = None,
        reply_to_id: str | None = None,
        refs: Sequence[str] | None = None,
        wait_s: int = 0,
    ) -> PostResult:
        """Post a message, route its mentions, and optionally wait up to ``wait_s`` (≤120) for the first reply."""
        self._validate(author, kind, body, client_msg_id, refs, wait_s)
        routing = Routing()
        decision_out: dict[str, Any] | None = None
        replayed = False
        async with self.log.transaction() as tx:
            existing = await self._by_client_id(tx, crew.id, author.user_id, client_msg_id)
            if existing is not None:
                if existing["author_session_id"] != author.session_id or existing["author_kind"] != (
                    "human" if author.is_human else "agent"
                ):
                    raise MessageConflict("client_msg_id was already used by another writer")
                row = existing
                replayed = True
            else:
                root, reply_to = await self._thread(tx, crew.id, thread_root_id, reply_to_id)
                clean_refs = await self._refs(tx, crew.id, refs or [])
                if kind == "decision":
                    decision_row, _ = await self.decisions.create_in_tx(
                        tx, crew, author, title=title_from_body(body), decision=body[:2000]
                    )
                    decision_out = decision_api(decision_row)
                    if decision_row["id"] not in clean_refs and len(clean_refs) < MAX_REFS:
                        clean_refs.append(decision_row["id"])
                mentions = parse_mentions(body)
                row = await self._insert(tx, crew, author, kind, body, client_msg_id, root, reply_to, clean_refs, mentions)
                await self._route(tx, crew, author, row, mentions, routing)
        if routing.agent_inbox and self._outbox_wake is not None:
            self._outbox_wake()
        message = message_api(row, callsign=author.callsign)
        result = PostResult(
            message=message,
            seq=int(row["seq"]),
            replayed=replayed,
            routing=routing.to_dict() if not replayed else {},
            decision=decision_out,
        )
        if wait_s > 0:
            started = time.monotonic()
            reply = await self.wait_for_reply(crew.id, row, author, wait_s=wait_s)
            return PostResult(
                message=result.message,
                seq=result.seq,
                replayed=result.replayed,
                routing=result.routing,
                decision=result.decision,
                reply=reply,
                waited_s=time.monotonic() - started,
            )
        return result

    def _validate(
        self, author: Author, kind: str, body: str, client_msg_id: str, refs: Sequence[str] | None, wait_s: int
    ) -> None:
        if not author.is_human:
            if not author.session_id:
                raise NotAllowed("agents post through their crew session")
            if is_reserved_sender(author.agent_id) or is_reserved_sender(author.callsign):
                raise ReservedSender("sender names mani, human, system and remembra are server-set only")
        if kind in RESERVED_KINDS or kind in SERVER_ONLY_MESSAGE_KINDS:
            raise ReservedSender(f"message kind {kind!r} is server-set only")
        if kind not in schemas.MESSAGE_KINDS:
            raise ValidationFailed(f"kind must be one of {', '.join(k for k in schemas.MESSAGE_KINDS if k != 'system')}")
        if not isinstance(body, str) or not body.strip():
            raise ValidationFailed("body is required")
        if len(body.encode("utf-8")) > schemas.MAX_MESSAGE_BYTES:
            raise ValidationFailed(f"body is larger than {schemas.MAX_MESSAGE_BYTES} bytes")
        if not isinstance(client_msg_id, str) or not CLIENT_MSG_ID_RE.fullmatch(client_msg_id):
            raise ValidationFailed("client_msg_id must be 1-64 characters of [A-Za-z0-9._:-]")
        if refs is not None and (not isinstance(refs, (list, tuple)) or len(refs) > MAX_REFS):
            raise ValidationFailed(f"refs must be a list of at most {MAX_REFS} ids")
        if isinstance(wait_s, bool) or not isinstance(wait_s, int) or not 0 <= wait_s <= schemas.SAY_WAIT_MAX_S:
            raise ValidationFailed(f"wait_s must be 0..{schemas.SAY_WAIT_MAX_S}")

    async def _by_client_id(self, tx: EventTx, crew_id: str, user_id: str, client_msg_id: str) -> dict[str, Any] | None:
        async with tx.conn.execute(
            "SELECT * FROM crew_messages WHERE crew_id = ? AND author_user_id = ? AND client_msg_id = ?",
            (crew_id, user_id, client_msg_id),
        ) as cur:
            row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def _thread(
        self, tx: EventTx, crew_id: str, thread_root_id: str | None, reply_to_id: str | None
    ) -> tuple[str | None, str | None]:
        root: str | None = None
        if reply_to_id is not None:
            parent = await require_same_crew(tx.conn, crew_id, "message", reply_to_id)
            root = parent["thread_root_id"] or parent["id"]
        if thread_root_id is not None:
            given = await require_same_crew(tx.conn, crew_id, "message", thread_root_id)
            given_root = given["thread_root_id"] or given["id"]
            if root is not None and root != given_root:
                raise ValidationFailed("reply_to_id is not in the thread named by thread_root_id")
            root = given_root
        return root, reply_to_id

    async def _refs(self, tx: EventTx, crew_id: str, refs: Sequence[Any]) -> list[str]:
        out: list[str] = []
        for ref in refs:
            kind = ref_kind(ref) if isinstance(ref, str) else None
            if kind is None:
                raise ValidationFailed("refs must be ids of this crew's entities (tsk_…, clm_…, msg_…, …)")
            await require_same_crew(tx.conn, crew_id, kind, ref)
            if ref not in out:
                out.append(ref)
        return out

    async def _insert(
        self,
        tx: EventTx,
        crew: CrewRef,
        author: Author,
        kind: str,
        body: str,
        client_msg_id: str,
        root: str | None,
        reply_to: str | None,
        refs: list[str],
        mentions: list[str],
    ) -> dict[str, Any]:
        async with tx.conn.execute("SELECT last_seq FROM crews WHERE id = ?", (crew.id,)) as cur:
            head = await cur.fetchone()
        if head is None:
            raise MessageNotFound("crew not found")
        expected_seq = int(head[0]) + 1
        message_id = new_id("message")
        await tx.conn.execute(
            """
            INSERT INTO crew_messages (id, crew_id, seq, thread_root_id, reply_to_id, kind, author_kind, author_user_id,
                author_session_id, author_agent_id, author_verified, body, mentions, refs, client_msg_id, trust_score,
                created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                message_id,
                crew.id,
                expected_seq,
                root,
                reply_to,
                kind,
                "human" if author.is_human else "agent",
                author.user_id,
                author.session_id,
                author.agent_id,
                1 if author.verified else 0,
                body,
                json.dumps(mentions),
                json.dumps(refs),
                client_msg_id,
                None if author.is_human else _trust_score(body),
                now_iso(),
            ),
        )
        row = await self._get(tx, message_id)
        assert row is not None
        where = f" in thread {root}" if root else ""
        result = await tx.emit(
            crew_id=crew.id,
            type="message.posted",
            actor=author.actor(),
            payload={"message": message_view(row)},
            summary=f"{author.label} posted a {kind} ({message_id}){where}",
            refs={"message_id": message_id, "session_id": author.session_id},
        )
        if result.seq != expected_seq:  # pragma: no cover - BEGIN IMMEDIATE makes last_seq stable
            raise MessageConflict("event sequence moved while posting")
        return row

    async def _route(
        self, tx: EventTx, crew: CrewRef, author: Author, row: Mapping[str, Any], mentions: list[str], routing: Routing
    ) -> None:
        if not mentions:
            return
        async with tx.conn.execute(
            f"SELECT id, callsign, agent_id, agent_verified, user_id FROM crew_sessions WHERE crew_id = ?"  # noqa: S608
            f" AND state IN ({', '.join('?' for _ in LIVE_SESSION_STATES)})",
            (crew.id, *LIVE_SESSION_STATES),
        ) as cur:
            live = [dict(r) for r in await cur.fetchall()]
        by_callsign = {str(s["callsign"]).lower(): s for s in live}
        message_id = str(row["id"])
        kind = str(row["kind"])
        origin = "human" if author.is_human else "agent"
        targets: dict[str, str | None] = {}  # session id -> "self-declared" label target, or None

        def queue(session: Mapping[str, Any], self_declared_for: str | None = None) -> None:
            sid = str(session["id"])
            if sid == author.session_id:
                return
            if sid not in targets or targets[sid] is not None:
                targets[sid] = self_declared_for

        paged_human = False
        for token in mentions:
            if token in HUMAN_ALIASES:
                if not author.is_human and not paged_human:  # "@mani @human" pages once
                    await self._needs_you(crew, author, message_id, kind, routing)
                    paged_human = True
                continue
            if token == "crew":
                for s in live:
                    queue(s)
                item = await self.inbox.raise_item(
                    crew_id=crew.id,
                    audience="crew",
                    kind="mention",
                    origin=origin,
                    title=f"{author.label} addressed the crew ({kind}, {message_id})",
                    dedupe_key=f"mention:{message_id}:crew",
                    ref_type="message",
                    ref_id=message_id,
                    actor=author.actor(),
                )
                routing.crew_inbox = item.item["id"] if item.item else None
                continue
            if token.startswith("zone:"):
                holders = await self._zone_holders(tx, crew.id, token[5:])
                if holders is None:
                    routing.unresolved.append(token)
                for sid in holders or []:
                    queue({"id": sid})
                continue
            if token.startswith("task:"):
                owner = await self._task_owner(tx, crew.id, token[5:])
                if owner is None:
                    routing.unresolved.append(token)
                else:
                    queue({"id": owner})
                continue
            session = by_callsign.get(token)
            if session is not None:
                queue(session)
                continue
            agent_sessions = [s for s in live if str(s["agent_id"]).lower() == token]
            if agent_sessions:
                for s in agent_sessions:
                    # @agent reaches that agent's key-verified sessions; a self-declared session using the
                    # name only gets it labelled as such (§5.7, §11.2).
                    queue(s, None if s["agent_verified"] else token)
                continue
            agent_id = await self._known_agent(tx, crew.id, token)
            if agent_id is not None:
                await self._to_agent_inbox(tx, crew, author, row, agent_id)
                routing.agent_inbox.append(agent_id)
                continue
            routing.unresolved.append(token)

        for sid, self_declared_for in targets.items():
            if self_declared_for is not None:
                title = f"{author.label} {kind} addressed to {self_declared_for}, you are self-declared ({message_id})"
                routing.self_declared.append(sid)
            else:
                title = f"{author.label} mentioned you ({kind}, {message_id})"
            await self.inbox.raise_item(
                crew_id=crew.id,
                audience="session",
                recipient=sid,
                kind="mention",
                origin=origin,
                title=title,
                dedupe_key=f"mention:{message_id}:{sid}",
                ref_type="message",
                ref_id=message_id,
                priority=1 if kind in ("question", "request_release") else 2,
                actor=author.actor(),
            )
            routing.sessions.append(sid)

    async def _needs_you(self, crew: CrewRef, author: Author, message_id: str, kind: str, routing: Routing) -> None:
        label = author.label
        noun = "question" if kind == "question" else "message"
        result = await self.inbox.raise_item(
            crew_id=crew.id,
            audience="project",
            kind="human_question",
            origin="agent",
            agent=author.agent_origin(),
            title=lambda n: (
                f"{label} sent you {n} {noun}{'s' if n != 1 else ''}"
                if noun == "message"
                else f"{label} asked {n} question{'s' if n != 1 else ''}"
            ),
            ref_type="message",
            ref_id=message_id,
            primary_action="answer",
            actor=author.actor(),
        )
        routing.needs_you = {
            "item_id": result.item["id"] if result.item else None,
            "coalesced": result.coalesced,
            "capped": result.capped,
        }

    async def _zone_holders(self, tx: EventTx, crew_id: str, slug: str) -> list[str] | None:
        async with tx.conn.execute(
            "SELECT id FROM crew_zones WHERE crew_id = ? AND lower(slug) = ? AND archived_at IS NULL", (crew_id, slug)
        ) as cur:
            zone = await cur.fetchone()
        if zone is None:
            return None
        async with tx.conn.execute(
            "SELECT DISTINCT holder_session_id FROM crew_claims WHERE crew_id = ? AND zone_id = ?"
            " AND state IN ('active','offered','reserved') AND holder_session_id IS NOT NULL",
            (crew_id, zone[0]),
        ) as cur:
            return [str(r[0]) for r in await cur.fetchall()]

    async def _task_owner(self, tx: EventTx, crew_id: str, ref: str) -> str | None:
        match = TASK_REF_RE.fullmatch(ref)
        if match:
            sql, param = "SELECT owner_session_id FROM crew_tasks WHERE crew_id = ? AND number = ?", int(match.group(1))
        elif schemas.is_id("task", ref):
            sql, param = "SELECT owner_session_id FROM crew_tasks WHERE crew_id = ? AND id = ?", ref  # type: ignore[assignment]
        else:
            return None
        async with tx.conn.execute(sql, (crew_id, param)) as cur:
            row = await cur.fetchone()
        return str(row[0]) if row is not None and row[0] else None

    async def _known_agent(self, tx: EventTx, crew_id: str, token: str) -> str | None:
        """An agent id that has joined this crew before (case-insensitive), as it was written."""
        async with tx.conn.execute(
            "SELECT agent_id FROM crew_sessions WHERE crew_id = ? AND lower(agent_id) = ? ORDER BY joined_at DESC LIMIT 1",
            (crew_id, token),
        ) as cur:
            row = await cur.fetchone()
        return str(row[0]) if row is not None else None

    async def _to_agent_inbox(self, tx: EventTx, crew: CrewRef, author: Author, row: Mapping[str, Any], agent_id: str) -> None:
        """No live session of ``agent_id``: leave the mention in its agent inbox (main DB, via the outbox, D35)."""
        await CrewStore(self.db).enqueue_outbox(
            crew.id,
            KIND_AGENT_INBOX_SEND,
            {
                "owner_user_id": crew.owner_user_id,
                "project_id": crew.project_id,
                "crew_id": crew.id,
                "to_agent": agent_id,
                "from_agent": author.agent_id if not author.is_human else "human",
                "sender_kind": "human" if author.is_human else "agent",
                "sender_verified": bool(author.verified),
                "kind": "mention",
                "subject": f"Crew mention from {author.label} ({row['kind']}) in {crew.project_id}",
                "body": str(row["body"]),
                "metadata": {"crew_id": crew.id, "crew_message_id": row["id"], "project_id": crew.project_id},
            },
            dedupe_key=f"mention:{row['id']}:{agent_id}",
        )

    # -- long-poll ----------------------------------------------------------------------------------

    async def wait_for_reply(
        self, crew_id: str, message: Mapping[str, Any], author: Author, *, wait_s: float
    ) -> dict[str, Any] | None:
        """The first message in ``message``'s thread after it from someone else, waiting up to ``wait_s``.

        Woken by ``message.posted`` on the bus when there is one; the database is
        re-checked at least every second either way, so a missed wake-up only
        delays the answer.
        """
        root = str(message["thread_root_id"] or message["id"])
        after = int(message["seq"])
        wake = asyncio.Event()

        def listener(env: Mapping[str, Any]) -> None:
            if env.get("crew_id") == crew_id and env.get("type") == "message.posted":
                posted = (env.get("payload") or {}).get("message") or {}
                if posted.get("thread_root_id") == root:
                    wake.set()

        unsubscribe = self.bus.subscribe(listener) if self.bus is not None else None
        deadline = time.monotonic() + max(0.0, float(wait_s))
        try:
            while True:
                reply = await self._first_reply(crew_id, root, after, author)
                if reply is not None:
                    return reply
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                wake.clear()
                try:
                    await asyncio.wait_for(wake.wait(), timeout=min(remaining, 1.0))
                except TimeoutError:
                    pass
        finally:
            if unsubscribe is not None:
                unsubscribe()

    async def _first_reply(self, crew_id: str, root: str, after: int, author: Author) -> dict[str, Any] | None:
        if author.is_human:
            not_me = "NOT (author_kind = 'human' AND author_user_id = ?)"
            me: str = author.user_id
        else:
            not_me = "COALESCE(author_session_id, '') != ?"
            me = str(author.session_id)
        row = await self.db.fetchone(
            f"SELECT * FROM crew_messages WHERE crew_id = ? AND thread_root_id = ? AND seq > ? AND {not_me}"  # noqa: S608
            " ORDER BY seq LIMIT 1",
            (crew_id, root, after, me),
        )
        if row is None:
            return None
        return message_api(row, callsign=await self._callsign(row["author_session_id"]))

    # -- edit, redact, pin ----------------------------------------------------------------------------

    async def edit(self, message_id: str, author: Author, *, body: str, now: datetime | None = None) -> dict[str, Any]:
        """Author-only edit within 10 minutes; the previous body goes to ``crew_message_edits``.

        Mentions are routed once, when the message is posted; an edit never
        re-notifies (the edit event says whether the old text was already delivered).
        """
        if not isinstance(body, str) or not body.strip():
            raise ValidationFailed("body is required")
        if len(body.encode("utf-8")) > schemas.MAX_MESSAGE_BYTES:
            raise ValidationFailed(f"body is larger than {schemas.MAX_MESSAGE_BYTES} bytes")
        stamp = now or datetime.now(UTC)
        async with self.log.transaction() as tx:
            row = await self._get(tx, message_id)
            if row is None:
                raise MessageNotFound("message not found")
            if not self._is_author(row, author):
                raise NotAllowed("only the author can edit a message")
            if row["redacted"]:
                raise MessageConflict("a redacted message cannot be edited")
            if stamp - parse_iso(str(row["created_at"])) > EDIT_WINDOW:
                raise MessageConflict("messages can be edited for 10 minutes after posting")
            if row["body"] == body:
                return {"message": message_api(row, callsign=author.callsign), "seq": None, "after_delivery": False}
            delivered = await self._delivered(tx, row)
            ts = now_iso(stamp)
            await tx.conn.execute(
                "INSERT INTO crew_message_edits (message_id, crew_id, prev_body, edited_at) VALUES (?, ?, ?, ?)",
                (message_id, row["crew_id"], row["body"], ts),
            )
            await tx.conn.execute(
                "UPDATE crew_messages SET body = ?, edited_at = ?, trust_score = ? WHERE id = ?",
                (body, ts, None if author.is_human else _trust_score(body), message_id),
            )
            row = await self._get(tx, message_id)
            assert row is not None
            result = await tx.emit(
                crew_id=row["crew_id"],
                type="message.edited",
                actor=author.actor(),
                payload={"message": message_view(row), "after_delivery": delivered},
                summary=f"{author.label} edited {message_id}" + (" after delivery" if delivered else ""),
                refs={"message_id": message_id, "session_id": author.session_id},
            )
        return {"message": message_api(row, callsign=author.callsign), "seq": result.seq, "after_delivery": delivered}

    async def history(self, message_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall(
            "SELECT prev_body, edited_at FROM crew_message_edits WHERE message_id = ? ORDER BY edited_at, rowid", (message_id,)
        )
        return [{"prev_body": r["prev_body"], "edited_at": r["edited_at"]} for r in rows]

    async def redact(self, message_id: str, human: Author) -> dict[str, Any]:
        """Human-only: remove the body (and its edit history), keep ``redacted_body_hash``."""
        if not human.is_human:
            raise NotAllowed("only a human can redact a message")
        async with self.log.transaction() as tx:
            row = await self._get(tx, message_id)
            if row is None:
                raise MessageNotFound("message not found")
            if row["redacted"]:
                return {"message": message_api(row), "seq": None}
            digest = hashlib.sha256(str(row["body"]).encode("utf-8")).hexdigest()
            await tx.conn.execute(
                "UPDATE crew_messages SET body = '', redacted = 1, redacted_body_hash = ?, trust_score = NULL WHERE id = ?",
                (digest, message_id),
            )
            await tx.conn.execute("UPDATE crew_message_edits SET prev_body = '' WHERE message_id = ?", (message_id,))
            row = await self._get(tx, message_id)
            assert row is not None
            result = await tx.emit(
                crew_id=row["crew_id"],
                type="message.redacted",
                actor=human.actor(),
                payload={"message_id": message_id},
                summary=f"human redacted {message_id}",
                refs={"message_id": message_id},
            )
        out = message_api(row)
        out["redacted_body_hash"] = row["redacted_body_hash"]
        return {"message": out, "seq": result.seq}

    async def pin(self, message_id: str, human: Author, *, pinned: bool = True) -> dict[str, Any]:
        """Human-only pin/unpin. The closed event set has no pin event (§4.2), so this writes state only."""
        if not human.is_human:
            raise NotAllowed("only a human can pin a message")
        async with self.db.transaction():
            await self.db.conn.execute("UPDATE crew_messages SET pinned = ? WHERE id = ?", (1 if pinned else 0, message_id))
            row = await self.db.fetchone("SELECT * FROM crew_messages WHERE id = ?", (message_id,))
        if row is None:
            raise MessageNotFound("message not found")
        return {"message": message_api(row), "seq": None}

    # -- reads -------------------------------------------------------------------------------------------

    async def list_messages(
        self,
        crew_id: str,
        *,
        thread: str | None = None,
        since_seq: int | None = None,
        before: int | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Messages in seq order; ``thread`` = a root id (the root plus its replies)."""
        sql = "SELECT * FROM crew_messages WHERE crew_id = ?"
        params: list[Any] = [crew_id]
        if thread is not None:
            sql += " AND (id = ? OR thread_root_id = ?)"
            params.extend([thread, thread])
        if since_seq is not None:
            sql += " AND seq > ?"
            params.append(int(since_seq))
        if before is not None:
            sql += " AND seq < ?"
            params.append(int(before))
        limit = max(1, min(int(limit), 200))
        if before is not None and since_seq is None:
            sql += " ORDER BY seq DESC LIMIT ?"
            params.append(limit)
            rows = list(reversed(await self.db.fetchall(sql, params)))
        else:
            sql += " ORDER BY seq LIMIT ?"
            params.append(limit)
            rows = await self.db.fetchall(sql, params)
        callsigns = await self._callsigns(crew_id, {r["author_session_id"] for r in rows if r["author_session_id"]})
        return [message_api(r, callsign=callsigns.get(r["author_session_id"] or "")) for r in rows]

    async def get(self, message_id: str) -> dict[str, Any] | None:
        row = await self.db.fetchone("SELECT * FROM crew_messages WHERE id = ?", (message_id,))
        if row is None:
            return None
        return message_api(row, callsign=await self._callsign(row["author_session_id"]))

    # -- helpers ------------------------------------------------------------------------------------------

    @staticmethod
    def _is_author(row: Mapping[str, Any], author: Author) -> bool:
        if author.is_human:
            return bool(row["author_kind"] == "human" and row["author_user_id"] == author.user_id)
        return bool(row["author_kind"] == "agent" and row["author_session_id"] == author.session_id)

    async def _delivered(self, tx: EventTx, row: Mapping[str, Any]) -> bool:
        """True once any recipient session saw (was injected with) a queue item for this message."""
        async with tx.conn.execute(
            """
            SELECT 1 FROM crew_inbox_items i
              LEFT JOIN crew_sessions s ON s.id = i.recipient AND s.crew_id = i.crew_id
             WHERE i.crew_id = ? AND i.ref_type = 'message' AND i.ref_id = ?
               AND (i.state != 'open' OR (i.audience = 'session' AND s.delivered_seq >= COALESCE(i.created_seq, 0)))
             LIMIT 1
            """,
            (row["crew_id"], row["id"]),
        ) as cur:
            return await cur.fetchone() is not None

    async def _get(self, tx: EventTx, message_id: str) -> dict[str, Any] | None:
        async with tx.conn.execute("SELECT * FROM crew_messages WHERE id = ?", (message_id,)) as cur:
            row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def _callsign(self, session_id: str | None) -> str | None:
        if not session_id:
            return None
        row = await self.db.fetchone("SELECT callsign FROM crew_sessions WHERE id = ?", (session_id,))
        return str(row["callsign"]) if row else None

    async def _callsigns(self, crew_id: str, session_ids: set[str]) -> dict[str, str]:
        if not session_ids:
            return {}
        marks = ", ".join("?" for _ in session_ids)
        rows = await self.db.fetchall(
            f"SELECT id, callsign FROM crew_sessions WHERE crew_id = ? AND id IN ({marks})",  # noqa: S608
            [crew_id, *session_ids],
        )
        return {str(r["id"]): str(r["callsign"]) for r in rows}


# ---------------------------------------------------------------------------
# Outbox handler: agent-inbox delivery of a mention (main DB)
# ---------------------------------------------------------------------------


def agent_inbox_handler(inbox_manager: Any) -> Any:
    """Outbox handler for :data:`KIND_AGENT_INBOX_SEND`: writes one ``agent_inbox`` row, idempotently.

    The row id is derived from the outbox id, so a retry after a crash never
    delivers twice. Sender provenance (``sender_kind``, ``sender_verified``) is
    server-set from the crew session that wrote the mention.
    """
    from remembra.crew.outbox import OutboxItem, OutboxPermanentError

    async def handle(item: OutboxItem) -> str | None:
        p = item.payload
        for key in ("owner_user_id", "to_agent", "from_agent", "subject", "body"):
            if not isinstance(p.get(key), str) or not p[key].strip():
                raise OutboxPermanentError(f"payload.{key} must be a non-empty string")
        inbox_id = "inbox_" + hashlib.sha256(item.id.encode()).hexdigest()[:16]
        row = await inbox_manager.send(
            owner_user_id=p["owner_user_id"],
            from_agent=p["from_agent"],
            to_agent=p["to_agent"],
            subject=p["subject"][:256],
            body=p["body"],
            metadata=p.get("metadata") or {},
            project_id=p.get("project_id"),
            crew_id=p.get("crew_id") or item.crew_id,
            kind=p.get("kind") or "mention",
            sender_kind=p.get("sender_kind") or "agent",
            sender_verified=bool(p.get("sender_verified")),
            inbox_id=inbox_id,
        )
        return str(row["inbox_id"])

    return handle
