"""Crew notifications: in-app list, real-time email and signed webhooks (spec §9.11, G4).

What notifies (L0 defaults, §9.11)

    ========================  =====================================  ===========
    kind                      events                                 channel
    ========================  =====================================  ===========
    ``handoff``               ``session.quota_blocked``,             real-time
                              ``session.lost``
    ``collision``             ``collision.detected`` /               real-time
                              ``collision.escalated`` (high/crit.)
    ``tamper``                ``guard.tamper_blocked``               real-time
    ``bypass``                ``guard.bypass_used``                  real-time
    ``githook``               ``githook.missing`` (state missing)    real-time
    ``zone_change``           ``zone.change_pending``                real-time
    ``decision``              ``decision.proposed``                  in-app
    ``stuck``                 ``session.stuck``                      in-app
    ``checkpoint_missed``     ``checkpoint.missed``                  in-app
    ``task_done``             ``task.done``                          in-app
    ========================  =====================================  ===========

Real-time delivery
    Targets are set by a **human** (dashboard login): an email address, or an
    ``https`` webhook that must answer a **signed challenge** before it is
    stored. Each webhook has its own signing secret, derived from a server key
    (``REMEMBRA_NOTIFY_SIGNING_KEY``, else the JWT secret) and the target id, so
    only its hash is stored (``crew_notify_targets.secret_hash``); the secret is
    shown once, when the target is added. Every webhook body is signed:
    ``X-Remembra-Signature: sha256=<HMAC-SHA256(secret, body)>`` (the body
    carries ``sent_at`` and a delivery ``id`` for replay checks). Webhook targets
    go through the SSRF-safe resolver (public IPs only, pinned, no redirects).

    A channel fires for a crew only when it is listed in that crew's
    ``settings.notify.realtime`` (default ``["email"]``) and not turned off by a
    ``crew_notification_rules`` row. Recipients are the crew's human owners and
    admins.

Durability
    :class:`NotificationDispatcher` reads committed events per crew after a
    durable cursor (``crew_read_cursors``, principal ``system:notify``) and
    queues deliveries in ``crew_outbox`` (kind ``crew_notify``) in the **same
    transaction** that advances the cursor, so a crash never loses or doubles a
    notification; the outbox worker delivers with retry and backoff. The bus
    only wakes the dispatcher; a periodic poll covers anything it missed.

Timing rules
    * The first notification to a target goes out at once (E2E: webhook within
      5 s of a quota stop); later ones within the 2-minute batching window are
      merged into one delivery at the end of the window.
    * Quiet hours (``crew_notification_rules.quiet_hours`` ``"HH:MM-HH:MM"``,
      in US Eastern time by default) delay delivery to the end of the window.
    * Losses shorter than 20 minutes do not notify: a ``session.lost`` whose
      last signal is younger than 20 minutes (and whose process did not exit)
      is held until the 20 minutes are up, cancelled by ``session.recovered``,
      and re-checked against the session's state just before sending.
    * Events older than one hour when first seen (e.g. history of a crew the
      dispatcher has never processed) are skipped for real-time delivery.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import html
import json
import os
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, Final
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
import structlog

from remembra.crew import schemas, startup
from remembra.crew.events import fetch_events, parse_ts
from remembra.crew.inbox import InboxError, ValidationFailed
from remembra.crew.settings import load_settings
from remembra.crew.store import CrewStore, now_iso

if TYPE_CHECKING:
    from fastapi import FastAPI

    from remembra.cloud.email import EmailBackend
    from remembra.crew.outbox import OutboxItem
    from remembra.webhooks.manager import ResolvedTarget

log = structlog.get_logger(__name__)

KIND_CREW_NOTIFY: Final = "crew_notify"
DISPATCH_PRINCIPAL: Final = "system:notify"
DISPATCH_STREAM: Final = "notify"
INAPP_STREAM: Final = "notifications"
BATCH_WINDOW_S: Final = 120
LOSS_QUIET_S: Final = 20 * 60
MAX_REALTIME_EVENT_AGE_S: Final = 3600
MAX_ITEMS_PER_DELIVERY: Final = 20
MAX_TARGETS_PER_USER: Final = 10
WEBHOOK_TIMEOUT_S: Final = 5.0
DEFAULT_QUIET_TZ: Final = "America/New_York"
DASHBOARD_URL_ENV: Final = "REMEMBRA_DASHBOARD_URL"
DEFAULT_DASHBOARD_URL: Final = "https://app.remembra.dev"
SIGNING_KEY_ENV: Final = "REMEMBRA_NOTIFY_SIGNING_KEY"
NOTIFY_ORDER: Final = 45
POLL_INTERVAL_S: Final = 5.0
SIGNATURE_HEADER: Final = "X-Remembra-Signature"
EMAIL_RE: Final = re.compile(r"[^@\s<>\"']{1,64}@[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}[A-Za-z0-9])?(?:\.[A-Za-z0-9-]{1,63})+")
QUIET_RE: Final = re.compile(r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)")

# kind -> (real-time by default, human description of the copy)
DEFAULT_RULES: Final[Mapping[str, bool]] = {
    "handoff": True,
    "collision": True,
    "tamper": True,
    "bypass": True,
    "githook": True,
    "zone_change": True,
    "decision": False,
    "stuck": False,
    "checkpoint_missed": False,
    "task_done": False,
}
NOTIFY_EVENT_TYPES: Final = (
    "session.quota_blocked",
    "session.lost",
    "collision.detected",
    "collision.escalated",
    "guard.tamper_blocked",
    "guard.bypass_used",
    "githook.missing",
    "zone.change_pending",
    "decision.proposed",
    "session.stuck",
    "checkpoint.missed",
    "task.done",
)
RULE_CHANNELS: Final = ("email", "webhook", "all", "in_app", "none")
TAMPER_TEXT: Final[Mapping[str, str]] = {
    "env_crew_var": "switch crew mode off with an environment variable",
    "no_verify": "skip the git hooks (--no-verify)",
    "hooks_path": "redirect the git hooks path",
    "husky_off": "turn Husky hooks off",
    "lefthook_off": "turn lefthook hooks off",
    "crewd_kill": "stop the crew daemon",
    "crew_files_removed": "remove crew files",
    "settings_hook_edit": "remove the crew hooks from its settings",
    "crew_policy_write": "edit the crew policy",
}


class NotifyError(InboxError):
    status = 422
    error = "notify_target"


# ---------------------------------------------------------------------------
# Classification and copy
# ---------------------------------------------------------------------------


def classify(event: Mapping[str, Any]) -> str | None:
    """The notification kind of an event, or None (§9.11 table)."""
    etype = event.get("type")
    payload = event.get("payload") or {}
    if etype in ("session.quota_blocked", "session.lost"):
        return "handoff"
    if etype in ("collision.detected", "collision.escalated"):
        sev = (payload.get("collision") or {}).get("severity")
        return "collision" if sev in ("high", "critical") else None
    if etype == "guard.tamper_blocked":
        return "tamper"
    if etype == "guard.bypass_used":
        return "bypass"
    if etype == "githook.missing":
        return "githook" if payload.get("state") == "missing" else None
    if etype == "zone.change_pending":
        return "zone_change"
    if etype == "decision.proposed":
        return "decision"
    if etype == "session.stuck":
        return "stuck" if payload.get("stuck") else None
    if etype == "checkpoint.missed":
        return "checkpoint_missed"
    if etype == "task.done":
        return "task_done"
    return None


def dashboard_url() -> str:
    return (os.environ.get(DASHBOARD_URL_ENV) or DEFAULT_DASHBOARD_URL).rstrip("/")


# Notification kinds whose item lives on a crew screen other than the feed (the in-app bell uses the same).
DEEP_LINK_VIEWS: Final = {"decision": "channel", "zone_change": "policy"}


def deep_link(project_id: str, seq: int, kind: str | None = None, payload: Mapping[str, Any] | None = None) -> str:
    """Opens the exact item on the dashboard (§9.12).

    The dashboard route is ``#/crew?project=<project_id>&view=<screen>&seq=<seq>`` (``lib/nav.ts``
    ``parseHash`` + ``lib/crew/routes.ts`` ``parseCrewRoute``): the feed scrolls to and selects the
    event with that seq; a finished task opens its report receipt; a zone change opens Policy (where
    it is approved) and a decision the Channel (where it is confirmed).
    """
    params: dict[str, str] = {"project": project_id}
    report_id = (payload or {}).get("report_id") if kind == "task_done" else None
    if isinstance(report_id, str) and report_id:
        params.update(view="report", report=report_id)
    else:
        params["view"] = DEEP_LINK_VIEWS.get(kind or "", "feed")
    params["seq"] = str(int(seq))
    return f"{dashboard_url()}/#/crew?{urlencode(params)}"


class Lookup:
    """Read-only lookups for notification copy (callsigns, zone slugs, task numbers)."""

    def __init__(self, db: Any) -> None:
        self.db = db
        self._callsigns: dict[str, str] = {}

    async def callsign(self, session_id: str | None) -> str:
        if not session_id:
            return "an agent"
        if session_id not in self._callsigns:
            row = await self.db.fetchone("SELECT callsign FROM crew_sessions WHERE id = ?", (session_id,))
            self._callsigns[session_id] = str(row["callsign"]) if row else "an agent"
        return self._callsigns[session_id]

    async def zone_slug(self, zone_id: str | None) -> str | None:
        if not zone_id:
            return None
        row = await self.db.fetchone("SELECT slug FROM crew_zones WHERE id = ?", (zone_id,))
        return str(row["slug"]) if row else None

    async def reserved_zones(self, crew_id: str, session_id: str | None, claim_ids: Iterable[str]) -> list[str]:
        ids = [c for c in claim_ids if isinstance(c, str)]
        if ids:
            marks = ", ".join("?" for _ in ids)
            rows = await self.db.fetchall(
                f"SELECT DISTINCT z.slug FROM crew_claims c JOIN crew_zones z ON z.id = c.zone_id"  # noqa: S608
                f" WHERE c.crew_id = ? AND c.id IN ({marks}) ORDER BY z.slug",
                [crew_id, *ids],
            )
        elif session_id:
            rows = await self.db.fetchall(
                "SELECT DISTINCT z.slug FROM crew_claims c JOIN crew_zones z ON z.id = c.zone_id"
                " WHERE c.crew_id = ? AND c.holder_session_id = ? AND c.state = 'reserved' ORDER BY z.slug",
                (crew_id, session_id),
            )
        else:
            rows = []
        return [str(r["slug"]) for r in rows]


def _plural(items: list[str]) -> str:
    if not items:
        return "no zones"
    if len(items) == 1:
        return f"zone {items[0]}"
    return "zones " + ", ".join(items)


async def render(event: Mapping[str, Any], lookup: Lookup) -> str:
    """Notification copy (§9.11). Server templates: callsigns, slugs, ids and enums; clipped."""
    etype = event.get("type")
    payload = event.get("payload") or {}
    actor = event.get("actor") or {}
    refs = event.get("refs") or {}
    session_id = refs.get("session_id") or (actor.get("id") if actor.get("kind") == "session" else None)
    agent = (
        actor.get("callsign") if actor.get("kind") == "session" and actor.get("callsign") else await lookup.callsign(session_id)
    )
    crew_id = str(event.get("crew_id"))
    text: str
    if etype == "session.quota_blocked":
        zones = await lookup.reserved_zones(crew_id, session_id, payload.get("claims_reserved") or [])
        text = (
            f"{agent} stopped ({payload.get('error')}, {payload.get('source')}). "
            f"Work saved; {_plural(zones)} reserved for the next pickup."
        )
    elif etype == "session.lost":
        zones = await lookup.reserved_zones(crew_id, session_id, [])
        text = (
            f"{agent} stopped ({payload.get('reason')}, server-inferred). "
            f"Work saved; {_plural(zones)} reserved for the next pickup."
        )
    elif etype in ("collision.detected", "collision.escalated"):
        c = payload.get("collision") or {}
        a = await lookup.callsign(c.get("session_a"))
        b = await lookup.callsign(c.get("session_b")) if c.get("session_b") else "another session"
        zone = await lookup.zone_slug(c.get("zone_id"))
        where = f"zone {zone}" if zone else schemas.clip_item(str(c.get("subject") or "a file"), 80)
        text = f"{a} changed 1 file in {where}, held by {b} ({c.get('kind')}, {c.get('severity')})."
    elif etype == "guard.tamper_blocked":
        text = f"{agent} tried to {TAMPER_TEXT.get(str(payload.get('kind')), str(payload.get('kind')))}. Blocked."
    elif etype == "guard.bypass_used":
        text = f"Bypass code used by {agent} ({payload.get('scope')})."
    elif etype == "githook.missing":
        text = f"{agent}: git hook {payload.get('hook')} is {payload.get('state')}. The commit gate is not active there."
    elif etype == "zone.change_pending":
        text = f"Approve zone change from {agent}: {schemas.clip_item(str(payload.get('diff_summary') or ''), 200)}"
    elif etype == "decision.proposed":
        d = payload.get("decision") or {}
        text = f"Confirm decision: D-{d.get('number')} {schemas.clip_item(str(d.get('title') or ''), 120)}"
    elif etype == "session.stuck":
        text = f"{agent} looks stuck ({payload.get('signal')})."
    elif etype == "checkpoint.missed":
        text = f"{agent} missed its checkpoint ({int(payload.get('overdue_s') or 0) // 60} min overdue)."
    elif etype == "task.done":
        t = payload.get("task") or {}
        text = f"T-{t.get('number')} done ({agent})."
    else:
        text = str(event.get("summary") or etype)
    return text[:500]


# ---------------------------------------------------------------------------
# Rules, quiet hours, recipients
# ---------------------------------------------------------------------------


def quiet_until(now: datetime, spec: str | None, tz_name: str = DEFAULT_QUIET_TZ) -> datetime | None:
    """End of the quiet window containing ``now`` (UTC), or None when not quiet. ``spec`` is ``"HH:MM-HH:MM"``."""
    if not spec:
        return None
    match = QUIET_RE.fullmatch(spec.strip())
    if not match:
        return None
    tz = ZoneInfo(tz_name)
    local = now.astimezone(tz)
    start = time(int(match.group(1)), int(match.group(2)))
    end = time(int(match.group(3)), int(match.group(4)))
    t = local.time()
    if start == end:
        return None
    if start < end:
        inside = start <= t < end
        end_day = local.date()
    else:  # wraps midnight
        inside = t >= start or t < end
        end_day = local.date() + timedelta(days=1) if t >= start else local.date()
    if not inside:
        return None
    return datetime.combine(end_day, end, tzinfo=tz).astimezone(UTC)


def validate_quiet_hours(spec: str) -> bool:
    return bool(QUIET_RE.fullmatch(spec.strip()))


@dataclass(frozen=True)
class Rule:
    channel: str | None  # None = defaults
    quiet_hours: str | None
    batch_window_s: int


async def load_rule(db: Any, user_id: str, crew_id: str, kind: str) -> Rule:
    rows = await db.fetchall(
        "SELECT kind, channel, quiet_hours, batch_window_s FROM crew_notification_rules"
        " WHERE user_id = ? AND crew_id = ? AND kind IN (?, '*')",
        (user_id, crew_id, kind),
    )
    chosen = next((r for r in rows if r["kind"] == kind), None) or next((r for r in rows if r["kind"] == "*"), None)
    if chosen is None:
        return Rule(None, None, BATCH_WINDOW_S)
    window = chosen["batch_window_s"]
    return Rule(
        channel=chosen["channel"] if chosen["channel"] in RULE_CHANNELS else None,
        quiet_hours=chosen["quiet_hours"],
        batch_window_s=int(window) if window is not None and int(window) >= 0 else BATCH_WINDOW_S,
    )


def channel_enabled(kind: str, target_kind: str, crew_realtime: Iterable[str], rule: Rule) -> bool:
    if not DEFAULT_RULES.get(kind, False):
        return False
    if rule.channel in ("none", "in_app"):
        return False
    if rule.channel in ("email", "webhook"):
        return rule.channel == target_kind
    return target_kind in set(crew_realtime)


async def crew_humans(db: Any, crew: Mapping[str, Any]) -> list[str]:
    """Humans who get real-time alerts: the owner and every owner/admin member."""
    rows = await db.fetchall(
        "SELECT user_id FROM crew_members WHERE crew_id = ? AND role IN ('owner','admin') ORDER BY added_at, user_id",
        (crew["id"],),
    )
    out = [str(crew["owner_user_id"])]
    out.extend(str(r["user_id"]) for r in rows if str(r["user_id"]) not in out)
    return out


# ---------------------------------------------------------------------------
# Targets and signing
# ---------------------------------------------------------------------------


def _signing_key() -> bytes:
    key = os.environ.get(SIGNING_KEY_ENV)
    if not key:
        from remembra.config import get_settings

        key = get_settings().jwt_secret
    return key.encode("utf-8")


def target_secret(target_id: str) -> str:
    """The webhook signing secret of a target (derived; never stored)."""
    digest = hmac.new(_signing_key(), f"crew-notify-target:{target_id}".encode(), hashlib.sha256).digest()
    return "whsec_" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(secret: str, body: bytes, header: str) -> bool:
    """Receiver-side check of ``X-Remembra-Signature`` (constant time)."""
    return hmac.compare_digest(sign(secret, body), header or "")


def target_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "target": row["target"],
        "verified_at": row["verified_at"],
        "created_at": row["created_at"],
    }


Resolver = Callable[[str], Awaitable["ResolvedTarget"]]


class WebhookSender:
    """Signed HTTPS POSTs to SSRF-checked, IP-pinned webhook targets (no redirects, 5 s timeout)."""

    def __init__(self, *, resolver: Resolver | None = None, transport: httpx.AsyncBaseTransport | None = None) -> None:
        if resolver is None:
            from remembra.webhooks.manager import resolve_webhook_target

            resolver = resolve_webhook_target
        self._resolver = resolver
        self._transport = transport

    async def post(self, url: str, payload: Mapping[str, Any], secret: str, delivery_id: str) -> httpx.Response:
        if not url.startswith("https://"):
            raise NotifyError("webhook targets must use https")
        from remembra.webhooks.delivery import _pinned_request

        try:
            target = await self._resolver(url)
        except ValueError as e:
            raise NotifyError(f"webhook target refused: {e}") from e
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "Remembra-Crew-Notify/1.0",
            "X-Remembra-Event": str(payload.get("type")),
            "X-Remembra-Delivery": delivery_id,
            SIGNATURE_HEADER: sign(secret, body),
        }
        pinned_url, kwargs = _pinned_request(target, headers)
        async with httpx.AsyncClient(timeout=WEBHOOK_TIMEOUT_S, follow_redirects=False, transport=self._transport) as client:
            return await client.post(pinned_url, content=body, **kwargs)


def email_backend_from_env() -> EmailBackend | None:
    """Resend when ``RESEND_API_KEY`` is set, else SMTP when ``SMTP_USERNAME``/``SMTP_PASSWORD`` are set, else None."""
    from remembra.cloud.email import ResendBackend, SMTPBackend

    try:
        if os.environ.get("RESEND_API_KEY"):
            return ResendBackend()
        if os.environ.get("SMTP_USERNAME") and os.environ.get("SMTP_PASSWORD"):
            return SMTPBackend()
    except ValueError:
        return None
    return None


class NotifyTargets:
    """Human-configured targets (``crew_notify_targets``)."""

    def __init__(self, db: Any, *, webhooks: WebhookSender | None = None) -> None:
        self.db = db
        self.webhooks = webhooks or WebhookSender()

    async def list(self, user_id: str) -> list[dict[str, Any]]:
        rows = await self.db.fetchall("SELECT * FROM crew_notify_targets WHERE user_id = ? ORDER BY created_at, id", (user_id,))
        return [target_view(r) for r in rows]

    async def add(self, user_id: str, kind: str, target: str) -> dict[str, Any]:
        """Add (or re-verify) a target. Webhooks must answer a signed challenge first; the secret is returned once."""
        if kind not in ("email", "webhook"):
            raise NotifyError("kind must be email or webhook")
        target = (target or "").strip()
        if kind == "email":
            if len(target) > 254 or not EMAIL_RE.fullmatch(target):
                raise NotifyError("target must be an email address")
            target = target.lower()
        elif not target.startswith("https://") or len(target) > 2048:
            raise NotifyError("webhook targets must be https URLs")
        existing = await self.db.fetchone(
            "SELECT * FROM crew_notify_targets WHERE user_id = ? AND kind = ? AND target = ?", (user_id, kind, target)
        )
        if existing is None:
            count = await self.db.fetchone("SELECT COUNT(*) AS n FROM crew_notify_targets WHERE user_id = ?", (user_id,))
            if count is not None and int(count["n"]) >= MAX_TARGETS_PER_USER:
                raise NotifyError(f"at most {MAX_TARGETS_PER_USER} notification targets per user")
        target_id = str(existing["id"]) if existing else "ntt_" + secrets.token_hex(12)
        out_secret: str | None = None
        hashed: str | None = None
        if kind == "webhook":
            out_secret = target_secret(target_id)
            hashed = secret_hash(out_secret)
            await self._challenge(target, target_id, out_secret)
        now = now_iso()
        async with self.db.transaction():
            if existing is None:
                await self.db.conn.execute(
                    "INSERT INTO crew_notify_targets (id, user_id, kind, target, secret_hash, verified_at, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (target_id, user_id, kind, target, hashed, now, now),
                )
            else:
                await self.db.conn.execute(
                    "UPDATE crew_notify_targets SET secret_hash = ?, verified_at = ? WHERE id = ?", (hashed, now, target_id)
                )
            row = await self.db.fetchone("SELECT * FROM crew_notify_targets WHERE id = ?", (target_id,))
        assert row is not None
        out = target_view(row)
        if out_secret is not None:
            out["signing_secret"] = out_secret
            out["signature_header"] = SIGNATURE_HEADER
        return out

    async def _challenge(self, url: str, target_id: str, secret: str) -> None:
        nonce = secrets.token_urlsafe(24)
        payload = {"type": "crew.notification.challenge", "challenge": nonce, "target_id": target_id, "sent_at": now_iso()}
        try:
            resp = await self.webhooks.post(url, payload, secret, f"challenge-{target_id}")
        except NotifyError:
            raise
        except httpx.HTTPError as e:
            raise NotifyError(f"webhook did not answer the challenge ({type(e).__name__})") from e
        answered = None
        if 200 <= resp.status_code < 300:
            with contextlib.suppress(ValueError):
                body = resp.json()
                if isinstance(body, dict):
                    answered = body.get("challenge")
            if answered is None:
                answered = resp.text.strip()
        if answered != nonce:
            raise NotifyError(
                f"webhook did not echo the signed challenge (status {resp.status_code}); "
                'reply 2xx with {"challenge": "<value>"} after checking the signature'
            )


# ---------------------------------------------------------------------------
# Dispatcher: events -> outbox rows (durable cursor)
# ---------------------------------------------------------------------------


class NotificationDispatcher:
    """Turns committed crew events into queued real-time deliveries (see the module docstring)."""

    def __init__(
        self, db: Any, *, outbox_wake: Callable[[], None] | None = None, poll_interval_s: float = POLL_INTERVAL_S
    ) -> None:
        self.db = db
        self.store = CrewStore(db)
        self._outbox_wake = outbox_wake
        self.poll_interval_s = poll_interval_s
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()

    def wake(self) -> None:
        self._wake.set()

    def bus_listener(self, envelope: Mapping[str, Any]) -> None:
        """CrewBus listener (synchronous, non-blocking): wake the loop for notification-relevant events."""
        if envelope.get("type") in NOTIFY_EVENT_TYPES or envelope.get("type") == "session.recovered":
            self._wake.set()

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # a bad row must not stop alerts for every crew
                log.error("crew_notify_loop_error", error=str(e), error_type=type(e).__name__)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=self.poll_interval_s)
            self._wake.clear()

    async def run_once(self, *, now: datetime | None = None) -> int:
        """Process every crew with events past its cursor; returns deliveries queued."""
        queued = 0
        async with self._lock:
            crews = await self.db.fetchall(
                """
                SELECT c.id, c.owner_user_id, c.project_id, c.settings, c.last_seq, rc.last_seq AS cursor
                  FROM crews c
                  LEFT JOIN crew_read_cursors rc ON rc.crew_id = c.id AND rc.principal = ? AND rc.stream = ?
                 WHERE rc.last_seq IS NULL OR c.last_seq > rc.last_seq
                """,
                (DISPATCH_PRINCIPAL, DISPATCH_STREAM),
            )
            for crew in crews:
                queued += await self._process_crew(crew, now)
        if queued and self._outbox_wake is not None:
            self._outbox_wake()
        return queued

    async def _process_crew(self, crew: Mapping[str, Any], now: datetime | None) -> int:
        cursor = int(crew["cursor"] or 0)
        queued = 0
        lookup = Lookup(self.db)
        while True:
            events = await fetch_events(self.db.conn, crew["id"], after_seq=cursor, limit=200)
            if not events:
                if crew["cursor"] is None:
                    await self._set_cursor(crew["id"], cursor)
                return queued
            stamp = now or datetime.now(UTC)
            plans: list[tuple[dict[str, Any], str, str]] = []
            recovered: list[str] = []
            for env in events:
                if env["type"] == "session.recovered":
                    sid = (env.get("refs") or {}).get("session_id") or (env.get("actor") or {}).get("id")
                    if sid:
                        recovered.append(str(sid))
                    continue
                kind = classify(env)
                if kind is None or not DEFAULT_RULES.get(kind, False):
                    continue
                if (stamp - parse_ts(env["ts"])).total_seconds() > MAX_REALTIME_EVENT_AGE_S:
                    continue
                plans.append((env, kind, await render(env, lookup)))
            async with self.db.transaction():
                for sid in recovered:
                    await self._cancel_for_recovery(crew["id"], sid)
                for env, kind, text in plans:
                    queued += await self._queue(crew, env, kind, text, stamp)
                await self._set_cursor(crew["id"], int(events[-1]["seq"]))
            cursor = int(events[-1]["seq"])

    async def _set_cursor(self, crew_id: str, seq: int) -> None:
        await self.db.conn.execute(
            """
            INSERT INTO crew_read_cursors (crew_id, principal, stream, last_seq, updated_at) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(crew_id, principal, stream) DO UPDATE SET last_seq = excluded.last_seq, updated_at = excluded.updated_at
            """,
            (crew_id, DISPATCH_PRINCIPAL, DISPATCH_STREAM, seq, now_iso()),
        )
        if not self.db.in_transaction:
            await self.db.conn.commit()

    async def _cancel_for_recovery(self, crew_id: str, session_id: str) -> None:
        await self.db.conn.execute(
            "UPDATE crew_outbox SET state = 'done', result_id = 'suppressed:recovered', updated_at = ?"
            " WHERE crew_id = ? AND kind = ? AND state = 'pending' AND json_extract(payload, '$.cancel_if_recovered') = ?",
            (now_iso(), crew_id, KIND_CREW_NOTIFY, session_id),
        )

    async def _queue(self, crew: Mapping[str, Any], env: Mapping[str, Any], kind: str, text: str, now: datetime) -> int:
        settings = load_settings(crew["settings"])
        realtime = list((settings.get("notify") or {}).get("realtime") or [])
        item = {
            "seq": int(env["seq"]),
            "event_type": env["type"],
            "kind": kind,
            "text": text,
            "link": deep_link(str(crew["project_id"]), int(env["seq"]), kind, env.get("payload")),
            "ts": env["ts"],
            "severity": env.get("severity"),
        }
        not_before = now
        cancel_session: str | None = None
        if env["type"] == "session.lost":
            payload = env.get("payload") or {}
            age = int(payload.get("last_signal_age_s") or 0)
            if payload.get("reason") != "process_exited" and age < LOSS_QUIET_S:
                not_before = max(now, parse_ts(env["ts"]) + timedelta(seconds=LOSS_QUIET_S - age))
                cancel_session = (env.get("refs") or {}).get("session_id") or (env.get("actor") or {}).get("id")
        queued = 0
        for user_id in await crew_humans(self.db, crew):
            rule = await load_rule(self.db, user_id, crew["id"], kind)
            targets = await self.db.fetchall(
                "SELECT * FROM crew_notify_targets WHERE user_id = ? AND verified_at IS NOT NULL ORDER BY created_at",
                (user_id,),
            )
            for target in targets:
                if not channel_enabled(kind, target["kind"], realtime, rule):
                    continue
                at = not_before
                quiet_end = quiet_until(at, rule.quiet_hours)
                if quiet_end is not None:
                    at = quiet_end
                queued += await self._enqueue(crew, target, user_id, item, at, now, rule.batch_window_s, cancel_session)
        return queued

    async def _enqueue(
        self,
        crew: Mapping[str, Any],
        target: Mapping[str, Any],
        user_id: str,
        item: dict[str, Any],
        at: datetime,
        now: datetime,
        window_s: int,
        cancel_session: str | None,
    ) -> int:
        target_id = str(target["id"])
        if cancel_session is None and at <= now and window_s > 0:
            # Merge into a delivery still waiting for its window (never into one the worker may be sending now).
            open_batch: dict[str, Any] | None = await self.db.fetchone(
                "SELECT id, payload FROM crew_outbox WHERE crew_id = ? AND kind = ? AND state = 'pending' AND attempts = 0"
                " AND json_extract(payload, '$.target_id') = ? AND json_extract(payload, '$.batch') = 1"
                " AND next_attempt_at > ? ORDER BY next_attempt_at LIMIT 1",
                (crew["id"], KIND_CREW_NOTIFY, target_id, now_iso(now + timedelta(seconds=1))),
            )
            if open_batch is not None:
                batch = json.loads(open_batch["payload"])
                if len(batch["items"]) < MAX_ITEMS_PER_DELIVERY:
                    batch["items"].append(item)
                else:
                    batch["dropped"] = int(batch.get("dropped", 0)) + 1
                await self.db.conn.execute(
                    "UPDATE crew_outbox SET payload = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(batch, sort_keys=True), now_iso(now), open_batch["id"]),
                )
                return 1
            window_start = now_iso(now - timedelta(seconds=window_s))
            recent = await self.db.fetchone(
                "SELECT MAX(created_at) AS last FROM crew_outbox WHERE crew_id = ? AND kind = ?"
                " AND json_extract(payload, '$.target_id') = ? AND created_at >= ?"
                " AND (result_id IS NULL OR result_id NOT LIKE 'suppressed%')",
                (crew["id"], KIND_CREW_NOTIFY, target_id, window_start),
            )
            if recent is not None and recent["last"]:
                at = parse_ts(str(recent["last"])) + timedelta(seconds=window_s)
        payload: dict[str, Any] = {
            "type": "crew.notification",
            "target_id": target_id,
            "target_kind": target["kind"],
            "user_id": user_id,
            "crew_id": crew["id"],
            "project_id": crew["project_id"],
            "items": [item],
            "batch": 1 if at > now and cancel_session is None else 0,
        }
        if cancel_session:
            payload["cancel_if_recovered"] = cancel_session
        outbox_id = await self.store.enqueue_outbox(
            crew["id"], KIND_CREW_NOTIFY, payload, dedupe_key=f"{target_id}:{item['seq']}"
        )
        if at > now:
            await self.db.conn.execute(
                "UPDATE crew_outbox SET next_attempt_at = ? WHERE id = ? AND state = 'pending'", (now_iso(at), outbox_id)
            )
        return 1


# ---------------------------------------------------------------------------
# Delivery (outbox handler)
# ---------------------------------------------------------------------------


def delivery_body(item_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    items = list(payload.get("items") or [])
    body: dict[str, Any] = {
        "type": "crew.notification",
        "id": item_id,
        "sent_at": now_iso(),
        "crew_id": payload.get("crew_id"),
        "project_id": payload.get("project_id"),
        "kind": items[0]["kind"] if len(items) == 1 else "batch",
        "text": "\n".join(str(i.get("text")) for i in items),
        "items": items,
    }
    if payload.get("dropped"):
        body["more"] = int(payload["dropped"])
    return body


def email_parts(body: Mapping[str, Any]) -> tuple[str, str]:
    items = list(body.get("items") or [])
    first = str(items[0]["text"]) if items else "Crew notification"
    subject = f"[Remembra crew · {body.get('project_id')}] " + (first if len(items) == 1 else f"{len(items)} updates")
    rows = "".join(
        f'<li><p>{html.escape(str(i.get("text")))}</p><p><a href="{html.escape(str(i.get("link")))}">Open</a></p></li>'
        for i in items
    )
    page = f"<html><body><h2>Crew {html.escape(str(body.get('project_id')))}</h2><ul>{rows}</ul></body></html>"
    return subject[:200], page


def notify_handler(
    db: Any, *, webhooks: WebhookSender | None = None, email_backend: EmailBackend | None = None
) -> Callable[[OutboxItem], Awaitable[str | None]]:
    """Outbox handler for :data:`KIND_CREW_NOTIFY`: one signed webhook POST or one email per item."""
    from remembra.cloud.email import EmailMessage
    from remembra.crew.outbox import OutboxPermanentError

    sender = webhooks or WebhookSender()

    async def handle(item: OutboxItem) -> str | None:
        payload = item.payload
        cancel = payload.get("cancel_if_recovered")
        if cancel:
            row = await db.fetchone("SELECT state FROM crew_sessions WHERE id = ?", (cancel,))
            if row is not None and row["state"] != "lost":
                return "suppressed:recovered"
        target = await db.fetchone("SELECT * FROM crew_notify_targets WHERE id = ?", (payload.get("target_id"),))
        if target is None or target["verified_at"] is None or target["user_id"] != payload.get("user_id"):
            raise OutboxPermanentError("notification target removed or not verified")
        body = delivery_body(item.id, payload)
        if target["kind"] == "webhook":
            secret = target_secret(str(target["id"]))
            if target["secret_hash"] != secret_hash(secret):
                raise OutboxPermanentError("signing key changed since the target was verified; add the target again")
            try:
                resp = await sender.post(str(target["target"]), body, secret, item.id)
            except NotifyError as e:
                raise OutboxPermanentError(e.message) from e
            if not 200 <= resp.status_code < 300:
                raise RuntimeError(f"webhook answered {resp.status_code}")
            return f"webhook:{resp.status_code}"
        backend = email_backend or email_backend_from_env()
        if backend is None:
            raise OutboxPermanentError("email delivery is not configured (RESEND_API_KEY or SMTP_*)")
        subject, page = email_parts(body)
        result = await backend.send(EmailMessage(to=str(target["target"]), subject=subject, html=page, tags={"kind": "crew"}))
        if not result.success:
            raise RuntimeError(f"email failed: {result.error}")
        return f"email:{result.message_id or 'sent'}"

    return handle


# ---------------------------------------------------------------------------
# In-app notifications (GET/PATCH /notifications)
# ---------------------------------------------------------------------------


async def list_notifications(
    db: Any, crews: Iterable[Mapping[str, Any]], user_id: str, *, limit: int = 50, crew_id: str | None = None
) -> dict[str, Any]:
    """Recent notification events across ``crews`` (already filtered by the caller's access), newest first."""
    crew_map = {str(c["id"]): c for c in crews if crew_id is None or str(c["id"]) == crew_id}
    if not crew_map:
        return {"items": [], "unread": 0}
    ids = list(crew_map)
    cursors = {
        str(r["crew_id"]): int(r["last_seq"])
        for r in await db.fetchall(
            f"SELECT crew_id, last_seq FROM crew_read_cursors WHERE principal = ? AND stream = ?"  # noqa: S608
            f" AND crew_id IN ({', '.join('?' for _ in ids)})",
            [user_id, INAPP_STREAM, *ids],
        )
    }
    type_marks = ", ".join("?" for _ in NOTIFY_EVENT_TYPES)
    from remembra.crew.events import EVENT_SELECT, row_to_stored

    async with db.conn.execute(
        f"{EVENT_SELECT} WHERE crew_id IN ({', '.join('?' for _ in ids)}) AND type IN ({type_marks})"  # noqa: S608
        " ORDER BY ts DESC, seq DESC LIMIT ?",
        [*ids, *NOTIFY_EVENT_TYPES, max(1, min(int(limit), 200)) * 3],
    ) as cur:
        rows = await cur.fetchall()
    lookup = Lookup(db)
    items: list[dict[str, Any]] = []
    unread = 0
    for raw in rows:
        env = row_to_stored(raw).envelope
        kind = classify(env)
        if kind is None:
            continue
        read = int(env["seq"]) <= cursors.get(str(env["crew_id"]), 0)
        unread += 0 if read else 1
        if len(items) < limit:
            items.append(
                {
                    "crew_id": env["crew_id"],
                    "project_id": crew_map[str(env["crew_id"])]["project_id"],
                    "seq": env["seq"],
                    "kind": kind,
                    "event_type": env["type"],
                    "realtime": DEFAULT_RULES.get(kind, False),
                    "text": await render(env, lookup),
                    "link": deep_link(
                        str(crew_map[str(env["crew_id"])]["project_id"]), int(env["seq"]), kind, env.get("payload")
                    ),
                    "ts": env["ts"],
                    "read": read,
                }
            )
    return {"items": items, "unread": unread}


def default_rules_view() -> list[dict[str, Any]]:
    return [
        {"kind": kind, "realtime": realtime, "channels": ["in_app", "email", "webhook"] if realtime else ["in_app"]}
        for kind, realtime in DEFAULT_RULES.items()
    ]


def validate_rule_channel(channel: str) -> None:
    if channel not in RULE_CHANNELS:
        raise ValidationFailed(f"channel must be one of {', '.join(RULE_CHANNELS)}")


# ---------------------------------------------------------------------------
# Startup hook (remembra.crew.startup registry, §14 interface)
# ---------------------------------------------------------------------------


async def _start_notify(app: FastAPI, rt: startup.CrewRuntime) -> None:
    from remembra.crew.channel import KIND_AGENT_INBOX_SEND, agent_inbox_handler

    db = startup.crew_db(app)
    worker = getattr(app.state, "crew_outbox", None)
    if worker is None:
        raise RuntimeError("crew mode: notifications need the crew outbox worker (crew.outbox hook)")
    # An embedding host (or a test) may provide the delivery transports; production builds them here.
    worker.handlers[KIND_CREW_NOTIFY] = notify_handler(
        db,
        webhooks=getattr(app.state, "crew_webhook_sender", None),
        email_backend=getattr(app.state, "crew_email_backend", None),
    )
    inbox_manager = getattr(app.state, "inbox_manager", None)
    if inbox_manager is not None:
        worker.handlers[KIND_AGENT_INBOX_SEND] = agent_inbox_handler(inbox_manager)
    dispatcher = NotificationDispatcher(db, outbox_wake=worker.wake)
    app.state.crew_notifier = dispatcher
    if rt.bus is not None:
        rt.extras["notify_unsubscribe"] = rt.bus.subscribe(dispatcher.bus_listener)
    tasks = getattr(app.state, "tasks", None)
    if tasks is None:
        raise RuntimeError("crew mode: app.state.tasks (TaskRegistry) is required to run the notification dispatcher")
    rt.extras["task:crew-notify"] = tasks.spawn(dispatcher.run_forever(), name="crew-notify", loop_task=True)
    log.info("crew_notify_started")


async def _stop_notify(app: FastAPI, rt: startup.CrewRuntime) -> None:
    unsubscribe = rt.extras.pop("notify_unsubscribe", None)
    if unsubscribe is not None:
        unsubscribe()
    task = rt.extras.pop("task:crew-notify", None)
    if task is not None and not task.done():
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.wait({task}, timeout=5.0)
    app.state.crew_notifier = None


def register_hooks() -> None:
    """Add (or re-add) the ``crew.notify`` hook: after the bus (10) and the outbox worker (35)."""
    startup.add_hook("crew.notify", order=NOTIFY_ORDER, start=_start_notify, stop=_stop_notify)


register_hooks()
