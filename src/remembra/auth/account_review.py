"""One-time review of the credentials set up before an account's email was verified.

Accounts made before email verification existed (and any account whose owner
never clicked the link) have ``email_verified = 0``. Anyone could have
registered such an account with somebody else's address and a password, then
added API keys, 2FA, app connections, webhooks and sign-in links to it
("email squatting"). So the first time somebody proves they own the mailbox:

* Sign in with Google or GitHub with a verified email (see
  :mod:`remembra.auth.social`), or
* an emailed password reset,

the account is NOT wiped. The email is marked verified, every dashboard
session issued before that ends (app connections and API keys keep working),
and a review opens. Everything keeps working (agents stay connected) until the
owner either keeps it all with one click or revokes single items. The review
lists every active API key, app connection and webhook, every sign-in link
except the one that proved the mailbox, 2FA turned on before it, and the
password when nobody has proven it belongs to the mailbox owner. A review with
nothing to list finishes by itself, without a screen or an email.

Only a session that proved the mailbox can act on the review. Such sessions
carry the review's id as the ``rvw`` JWT claim, which the server signs:

* a sign-in with the exact provider account that opened the review, or one
  the owner connected from such a session while the review was open
  (``trusted_identities``, stored as ``provider:subject``), or
* a password sign-in once the password was set through an emailed reset.

Any other session (the squatter's password, a GitHub link the squatter added)
still signs in, but sees no review, cannot connect or disconnect sign-in
methods, turn on 2FA, or create API keys and webhooks until the review is
done, and gets no superadmin rights (or owner plan) from an owner address. A
Settings connect started before the review opened never lands. 2FA set up
before verification does not stand between the mailbox owner and the
account: it is one of the items under review, and it is turned off when the
review finishes unless the owner keeps it by entering a current code.

"Keep all" keeps exactly the list the owner saw: the dashboard sends the
list's ``version`` and a changed list is refused (409) and shown again.

Every revoke, keep, deferral, completion, trust change and trusted session is
written to the audit log, and the finished review is emailed to the account
address (unless nothing was kept or removed).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from remembra.core.time import utcnow
from remembra.security.audit import AuditAction, AuditLogger

log = structlog.get_logger(__name__)

REVIEW_CLAIM = "rvw"
ORIGIN_GOOGLE = "google"
ORIGIN_GITHUB = "github"
ORIGIN_PASSWORD_RESET = "password_reset"
PROVIDER_ORIGINS = frozenset({ORIGIN_GOOGLE, ORIGIN_GITHUB})
ORIGINS = PROVIDER_ORIGINS | {ORIGIN_PASSWORD_RESET}
_ORIGIN_NAMES = {ORIGIN_GOOGLE: "Google", ORIGIN_GITHUB: "GitHub"}

# password_status: whether the current password is known to be the mailbox owner's.
PASSWORD_UNTRUSTED = "untrusted"  # set before verification, by whoever made the account
PASSWORD_TRUSTED = "trusted"  # set through an emailed reset
PASSWORD_REMOVED = "removed"  # removed in the review; only a reset sets a new one

# totp_status: whether 2FA on the account predates verification.
TOTP_NONE = "none"
TOTP_PREDATES = "predates"
TOTP_TRUSTED = "trusted"  # turned on (or kept with a current code) by a session that proved the mailbox

ITEM_KINDS = ("key", "connection", "webhook", "identity", "two_factor", "password")

# Who closed a review that had nothing to list (no screen, no email).
METHOD_NOTHING_TO_CHECK = "auto_nothing_to_check"


class ReviewError(Exception):
    """A review action that cannot be done; ``status`` is the HTTP status to answer with."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass(frozen=True)
class Review:
    user_id: str
    review_id: str
    origin: str
    verified_at: str
    verified_at_ms: int
    password_status: str
    totp_status: str
    created_at: str
    completed_at: str | None
    # "provider:subject" of each provider account that proved the mailbox.
    trusted_identities: tuple[str, ...] = ()

    @property
    def pending(self) -> bool:
        return self.completed_at is None


_COLUMNS = (
    "user_id, review_id, origin, verified_at, verified_at_ms, password_status, totp_status, created_at, completed_at,"
    " trusted_identities"
)


def _missing_table(error: Exception) -> bool:
    return "no such table" in str(error).lower()


def identity_key(provider: str, subject: str | None) -> str:
    return f"{provider}:{subject or ''}"


def now_iso() -> str:
    """Naive UTC ISO time, the format the rest of the schema (and the audit log) stores."""
    return utcnow().isoformat()


def parse_ts(value: Any) -> float | None:
    """Epoch seconds from an ISO string, a SQLite ``CURRENT_TIMESTAMP`` string or an epoch number."""
    if value is None or value == "":
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            try:
                return float(text)
            except ValueError:
                return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _iso(value: Any) -> str | None:
    ts = parse_ts(value)
    return datetime.fromtimestamp(ts, tz=UTC).isoformat() if ts is not None else None


def _trusted_list(raw: Any) -> tuple[str, ...]:
    try:
        parsed = json.loads(raw or "[]")
    except ValueError:
        return ()
    return tuple(str(v) for v in parsed if isinstance(v, str)) if isinstance(parsed, list) else ()


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


async def audit(
    db: Any, user_id: str, action: AuditAction, *, resource_id: str | None, ip: str | None, details: dict[str, Any]
) -> None:
    """One audit row; ``details`` (JSON) goes in ``error_message``. Never raises."""
    try:
        await AuditLogger(db).log(
            user_id=user_id,
            action=action,
            resource_id=resource_id,
            ip_address=ip,
            success=True,
            error_message=json.dumps(details, sort_keys=True),
        )
    except Exception as e:  # the audit trail never blocks the owner
        log.error("account_review_audit_failed", user_id=user_id, action=action.value, error_type=type(e).__name__)


# ---------------------------------------------------------------------------
# Reading and opening
# ---------------------------------------------------------------------------


async def get_review(db: Any, user_id: str) -> Review | None:
    try:
        cursor = await db.conn.execute(f"SELECT {_COLUMNS} FROM account_reviews WHERE user_id = ?", (user_id,))
    except sqlite3.OperationalError as e:
        if _missing_table(e):
            return None  # a database that never ran migration 10
        raise
    row = await cursor.fetchone()
    if row is None:
        return None
    return Review(
        user_id=str(row[0]),
        review_id=str(row[1]),
        origin=str(row[2]),
        verified_at=str(row[3]),
        verified_at_ms=int(row[4]),
        password_status=str(row[5]),
        totp_status=str(row[6]),
        created_at=str(row[7]),
        completed_at=str(row[8]) if row[8] else None,
        trusted_identities=_trusted_list(row[9]),
    )


async def pending_review(db: Any, user_id: str) -> Review | None:
    review = await get_review(db, user_id)
    return review if review is not None and review.pending else None


async def is_pending(db: Any, user_id: str) -> bool:
    return await pending_review(db, user_id) is not None


async def open_review(
    db: Any,
    user_id: str,
    *,
    origin: str,
    verified_at: str,
    totp_enabled: bool,
    proof_identity: tuple[str, str] | None = None,
    ip: str | None = None,
) -> Review:
    """Open (or update) the review for ``user_id``. Safe inside ``db.transaction()``.

    ``proof_identity`` is ``(provider, subject)`` of the provider account that
    proved the mailbox (required for a provider origin); only that exact
    account is trusted, not any later link at the same provider. A reset on
    an account that already has a pending review only marks the password as
    the owner's (audited). A finished review is never reopened.

    Callers cut off the account's dashboard sessions right after (which also
    cancels Settings connects in flight), outside any transaction.
    """
    if origin not in ORIGINS:
        raise ValueError(f"unknown review origin {origin!r}")
    if origin in PROVIDER_ORIGINS and (proof_identity is None or proof_identity[0] != origin):
        raise ValueError("a provider review needs the provider account that proved the mailbox")
    existing = await get_review(db, user_id)
    if existing is not None:
        if existing.pending and origin == ORIGIN_PASSWORD_RESET and existing.password_status != PASSWORD_TRUSTED:
            await db.conn.execute("UPDATE account_reviews SET password_status = ? WHERE user_id = ?", (PASSWORD_TRUSTED, user_id))
            await db.conn.commit()
            await audit(
                db,
                user_id,
                AuditAction.ACCOUNT_REVIEW_UPDATED,
                resource_id="password",
                ip=ip,
                details={"change": "password_trusted", "by": "password_reset"},
            )
            refreshed = await get_review(db, user_id)
            assert refreshed is not None
            return refreshed
        return existing
    verified_ms = int((parse_ts(verified_at) or time.time()) * 1000)
    review_id = secrets.token_urlsafe(24)
    trusted = [identity_key(*proof_identity)] if proof_identity else []
    await db.conn.execute(
        f"INSERT INTO account_reviews ({_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
        (
            user_id,
            review_id,
            origin,
            verified_at,
            verified_ms,
            PASSWORD_TRUSTED if origin == ORIGIN_PASSWORD_RESET else PASSWORD_UNTRUSTED,
            TOTP_PREDATES if totp_enabled else TOTP_NONE,
            now_iso(),
            json.dumps(trusted),
        ),
    )
    await db.conn.commit()
    log.info("account_review_opened", user_id=user_id, origin=origin)
    review = await get_review(db, user_id)
    assert review is not None
    return review


# ---------------------------------------------------------------------------
# Sessions that proved the mailbox
# ---------------------------------------------------------------------------


def session_is_trusted(review: Review | None, payload: dict[str, Any] | None) -> bool:
    """True when the JWT payload carries this pending review's id."""
    if review is None or not review.pending or not payload:
        return False
    claim = payload.get(REVIEW_CLAIM)
    return isinstance(claim, str) and secrets.compare_digest(claim, review.review_id)


async def login_claims(db: Any, user_id: str, *, provider: str | None, subject: str | None = None) -> tuple[dict[str, Any], bool]:
    """Extra JWT claims for a new session, and whether a pre-verification 2FA is skipped.

    ``provider``/``subject`` identify the provider account used, or
    ``provider=None`` for a password sign-in. Returns ``({}, False)`` when no
    review is pending or the sign-in method does not prove the mailbox. Call
    :func:`audit_session` once the session is actually issued.
    """
    review = await pending_review(db, user_id)
    if review is None:
        return {}, False
    if provider is None:
        trusted = review.password_status == PASSWORD_TRUSTED
    else:
        trusted = bool(subject) and identity_key(provider, subject) in review.trusted_identities
    if not trusted:
        return {}, False
    return {REVIEW_CLAIM: review.review_id}, review.totp_status == TOTP_PREDATES


async def audit_session(
    db: Any, user_id: str, claims: dict[str, Any], *, provider: str | None, subject: str | None, ip: str | None
) -> None:
    """Record which sign-in method gained review authority (a session with the ``rvw`` claim)."""
    if not claims.get(REVIEW_CLAIM):
        return
    details: dict[str, Any] = {"method": "password_reset"} if provider is None else {"method": provider, "subject": subject}
    await audit(db, user_id, AuditAction.ACCOUNT_REVIEW_SESSION, resource_id=str(details["method"]), ip=ip, details=details)


def blocked_message(review: Review) -> str:
    name = _ORIGIN_NAMES.get(review.origin)
    if name:
        return f"Sign in with {name} and finish checking your account first."
    return "Sign in with your password and finish checking your account first."


async def untrusted_block(db: Any, user_id: str, payload: dict[str, Any] | None) -> str | None:
    """The refusal to show when a review is pending and this session did not prove the mailbox, else None."""
    review = await pending_review(db, user_id)
    if review is None or session_is_trusted(review, payload):
        return None
    return blocked_message(review)


async def password_sign_in_block(db: Any, user_id: str) -> str | None:
    """For password sign-ins that add access (connecting an app): refused while a review is open
    and the password has not been proven to be the mailbox owner's."""
    review = await pending_review(db, user_id)
    if review is None or review.password_status == PASSWORD_TRUSTED:
        return None
    return blocked_message(review)


async def note_totp_enabled(db: Any, user_id: str, *, ip: str | None = None) -> None:
    """2FA turned on by a trusted session during the review belongs to the owner (audited)."""
    review = await pending_review(db, user_id)
    if review is not None:
        await db.conn.execute("UPDATE account_reviews SET totp_status = ? WHERE user_id = ?", (TOTP_TRUSTED, user_id))
        await db.conn.commit()
        await audit(
            db,
            user_id,
            AuditAction.ACCOUNT_REVIEW_UPDATED,
            resource_id="two_factor",
            ip=ip,
            details={"change": "two_factor_trusted", "by": "two_factor_setup"},
        )


async def trust_identity(db: Any, review: Review, provider: str, subject: str, *, ip: str | None = None) -> None:
    """A provider account connected by the proven owner during the review also proves the mailbox (audited)."""
    key = identity_key(provider, subject)
    current = await get_review(db, review.user_id)
    if current is None or not current.pending or key in current.trusted_identities:
        return
    await db.conn.execute(
        "UPDATE account_reviews SET trusted_identities = ? WHERE user_id = ?",
        (json.dumps([*current.trusted_identities, key]), review.user_id),
    )
    await db.conn.commit()
    await audit(
        db,
        review.user_id,
        AuditAction.ACCOUNT_REVIEW_UPDATED,
        resource_id=f"identity:{provider}",
        ip=ip,
        details={"change": "identity_trusted", "provider": provider, "subject": subject},
    )


async def forget_trusted_identity(db: Any, user_id: str, provider: str, subject: str) -> None:
    """A disconnected provider account no longer proves anything."""
    review = await pending_review(db, user_id)
    key = identity_key(provider, subject)
    if review is None or key not in review.trusted_identities:
        return
    remaining = [k for k in review.trusted_identities if k != key]
    await db.conn.execute("UPDATE account_reviews SET trusted_identities = ? WHERE user_id = ?", (json.dumps(remaining), user_id))
    await db.conn.commit()


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------


def _split(value: Any) -> list[str]:
    if not value:
        return []
    text = str(value)
    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, list):
            return [str(p) for p in parsed if p]
    return [p for p in text.replace(" ", ",").split(",") if p]


async def _rows(db: Any, sql: str, args: tuple[Any, ...]) -> list[Any]:
    try:
        cursor = await db.conn.execute(sql, args)
    except sqlite3.OperationalError as e:
        if _missing_table(e):
            return []
        raise
    return list(await cursor.fetchall())


def _before(review: Review, created: Any) -> bool:
    ts = parse_ts(created)
    return ts is None or ts * 1000 < review.verified_at_ms


async def list_keys(db: Any, review: Review) -> list[dict[str, Any]]:
    rows = await _rows(
        db,
        "SELECT k.id, k.name, k.created_at, k.last_used_at, k.agent_id, r.role, r.scopes, r.project_ids"
        " FROM api_keys k LEFT JOIN api_key_roles r ON r.api_key_id = k.id"
        " WHERE k.user_id = ? AND k.active ORDER BY k.created_at",
        (review.user_id,),
    )
    if not rows:  # deployments without the RBAC table
        rows = [
            (*r, None, None, None)
            for r in await _rows(
                db,
                "SELECT id, name, created_at, last_used_at, agent_id FROM api_keys WHERE user_id = ? AND active"
                " ORDER BY created_at",
                (review.user_id,),
            )
        ]
    return [
        {
            "id": str(r[0]),
            "name": r[1] or None,
            "created_at": _iso(r[2]),
            "last_used_at": _iso(r[3]),
            "agent_id": r[4] or None,
            "role": r[5] or "editor",
            "scopes": _split(r[6]),
            "project_ids": _split(r[7]),
            "before_verification": _before(review, r[2]),
        }
        for r in rows
    ]


async def list_connections(db: Any, review: Review) -> list[dict[str, Any]]:
    rows = await _rows(
        db,
        "SELECT g.grant_id, c.client_name, g.client_id, g.scope, g.project_ids, g.agent_id, g.created_at, g.last_used_at"
        " FROM oauth_grants g LEFT JOIN oauth_clients c ON c.client_id = g.client_id"
        " WHERE g.user_id = ? AND g.revoked_at IS NULL ORDER BY g.created_at",
        (review.user_id,),
    )
    if not rows:
        rows = [
            (r[0], None, *r[1:])
            for r in await _rows(
                db,
                "SELECT grant_id, client_id, scope, project_ids, agent_id, created_at, last_used_at FROM oauth_grants"
                " WHERE user_id = ? AND revoked_at IS NULL ORDER BY created_at",
                (review.user_id,),
            )
        ]
    return [
        {
            "id": str(r[0]),
            "name": r[1] or r[2] or "App",
            "scopes": _split(r[3]),
            "project_ids": _split(r[4]),
            "agent_id": r[5] or None,
            "created_at": _iso(r[6]),
            "last_used_at": _iso(r[7]),
            "before_verification": _before(review, r[6]),
        }
        for r in rows
    ]


async def list_webhooks(db: Any, review: Review) -> list[dict[str, Any]]:
    rows = await _rows(
        db,
        "SELECT id, url, events, created_at FROM webhooks WHERE user_id = ? AND active = 1 ORDER BY created_at",
        (review.user_id,),
    )
    return [
        {
            "id": str(r[0]),
            "url": str(r[1]),
            "events": _split(r[2]),
            "created_at": _iso(r[3]),
            "before_verification": _before(review, r[3]),
        }
        for r in rows
    ]


async def list_identities(db: Any, review: Review) -> list[dict[str, Any]]:
    """Every sign-in link except the provider accounts that proved the mailbox."""
    from remembra.auth.social import PROVIDERS

    rows = await _rows(
        db,
        "SELECT provider, email, created_at, last_login_at, provider_user_id FROM user_identities WHERE user_id = ?"
        " ORDER BY provider",
        (review.user_id,),
    )
    return [
        {
            "id": str(r[0]),
            "provider": str(r[0]),
            "name": PROVIDERS[str(r[0])].name if str(r[0]) in PROVIDERS else str(r[0]),
            "email": str(r[1]),
            "created_at": _iso(r[2]),
            "last_login_at": _iso(r[3]),
            "subject": str(r[4]),
        }
        for r in rows
        if identity_key(str(r[0]), str(r[4])) not in review.trusted_identities
    ]


async def _totp_enabled(db: Any, user_id: str) -> bool:
    user = await db.get_user_by_id(user_id)
    return bool(user and user.get("totp_enabled"))


async def review_items(db: Any, review: Review) -> dict[str, Any]:
    return {
        "keys": await list_keys(db, review),
        "connections": await list_connections(db, review),
        "webhooks": await list_webhooks(db, review),
        "identities": await list_identities(db, review),
        "two_factor": review.totp_status == TOTP_PREDATES and await _totp_enabled(db, review.user_id),
        "password": review.password_status == PASSWORD_UNTRUSTED,
    }


def _digest(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def item_ids(items: dict[str, Any]) -> list[str]:
    """What is listed and what each item can reach (not how it is used: last-used times change all the time).

    A key whose role or projects changed, or a webhook pointed elsewhere, is
    a different item: "Keep all" must not keep what the owner did not see.
    """
    ids = [f"key:{k['id']}:{_digest(k['role'], k['scopes'], k['project_ids'], k['agent_id'])}" for k in items["keys"]]
    ids += [f"connection:{c['id']}:{_digest(c['scopes'], c['project_ids'], c['agent_id'])}" for c in items["connections"]]
    ids += [f"webhook:{w['id']}:{_digest(w['url'], w['events'])}" for w in items["webhooks"]]
    ids += [f"identity:{i['id']}:{_digest(i['email'], i['subject'])}" for i in items["identities"]]
    if items["two_factor"]:
        ids.append("two_factor")
    if items["password"]:
        ids.append("password")
    return sorted(ids)


def items_version(items: dict[str, Any]) -> str:
    """Fingerprint of the listed set; "Keep all" must send the one it was shown."""
    return hashlib.sha256("\n".join(item_ids(items)).encode()).hexdigest()[:32]


def _key_label(item: dict[str, Any]) -> str:
    projects = ", ".join(item["project_ids"]) if item["project_ids"] else "all projects"
    parts = [item["role"], projects]
    if item.get("agent_id"):
        parts.append(f"agent {item['agent_id']}")
    return f"API key '{item['name'] or 'unnamed'}' ({', '.join(parts)})"


def _host(url: str) -> str:
    """Only the host of a webhook URL goes into logs and email (its path may carry a secret)."""
    from urllib.parse import urlsplit

    try:
        return urlsplit(url).netloc or url
    except ValueError:
        return url


TWO_FACTOR_LABEL = "Two-factor sign-in"


def item_labels(items: dict[str, Any]) -> list[str]:
    """One plain line per item, for the audit log and the notice email."""
    labels = [_key_label(k) for k in items["keys"]]
    labels += [f"App connection '{c['name']}'" for c in items["connections"]]
    labels += [f"Webhook to {_host(w['url'])}" for w in items["webhooks"]]
    labels += [f"{i['name']} sign-in ({i['email']})" for i in items["identities"]]
    if items["two_factor"]:
        labels.append(TWO_FACTOR_LABEL)
    if items["password"]:
        labels.append("Password")
    return labels


# ---------------------------------------------------------------------------
# Acting on the review
# ---------------------------------------------------------------------------


async def audit_opened(db: Any, review: Review, *, ip: str | None) -> None:
    items = await review_items(db, review)
    await audit(
        db,
        review.user_id,
        AuditAction.ACCOUNT_REVIEW_OPENED,
        resource_id=review.origin,
        ip=ip,
        details={"origin": review.origin, "trusted": list(review.trusted_identities), "items": item_labels(items)},
    )


async def _turn_off_two_factor(db: Any, user_id: str) -> None:
    await db.disable_totp(user_id)
    await db.conn.execute("UPDATE account_reviews SET totp_status = ? WHERE user_id = ?", (TOTP_NONE, user_id))
    await db.conn.commit()


async def revoke_item(db: Any, review: Review, kind: str, item_id: str | None, *, ip: str | None, method: str) -> dict[str, Any]:
    """Revoke one listed item. Returns ``{"label", "sessions_reset", "finished"}``.

    Removing the password or a sign-in link signs out every dashboard session
    (the caller gets a fresh one), so a squatter's open session ends with it.
    App connections and API keys are not touched by that: each is its own
    item. When nothing is left to check the review finishes (``finished``).
    """
    if not review.pending:
        raise ReviewError("This account check is already done.", 409)
    if kind not in ITEM_KINDS:
        raise ReviewError("Unknown item.", 400)
    items = await review_items(db, review)
    user_id = review.user_id
    sessions_reset = False
    label: str

    if kind in ("key", "connection", "webhook", "identity"):
        collection = {"key": "keys", "connection": "connections", "webhook": "webhooks", "identity": "identities"}[kind]
        match = next((i for i in items[collection] if i["id"] == item_id), None)
        if match is None:
            raise ReviewError("That item is not in this account check.", 404)
        if kind == "key":
            from remembra.auth.keys import APIKeyManager

            await APIKeyManager(db).revoke_key(match["id"], user_id)
            label = _key_label(match)
        elif kind == "connection":
            now = time.time()
            async with db.transaction():
                await db.conn.execute(
                    "UPDATE oauth_grants SET revoked_at = ?, revoke_reason = 'account_review'"
                    " WHERE grant_id = ? AND user_id = ? AND revoked_at IS NULL",
                    (now, match["id"], user_id),
                )
                await db.conn.execute(
                    "UPDATE oauth_tokens SET revoked_at = ? WHERE grant_id = ? AND revoked_at IS NULL", (now, match["id"])
                )
                await db.conn.execute(
                    "UPDATE oauth_codes SET used_at = COALESCE(used_at, ?) WHERE grant_id = ?", (now, match["id"])
                )
            label = f"App connection '{match['name']}'"
        elif kind == "webhook":
            await db.conn.execute(
                "UPDATE webhooks SET active = 0, updated_at = ? WHERE id = ? AND user_id = ?", (now_iso(), match["id"], user_id)
            )
            await db.conn.commit()
            label = f"Webhook to {_host(match['url'])}"
        else:
            from remembra.auth import social

            await social.unlink_identity(db, user_id, match["id"], ip=ip, by="account_review")
            sessions_reset = True
            label = f"{match['name']} sign-in ({match['email']})"
    elif kind == "two_factor":
        if not items["two_factor"]:
            raise ReviewError("That item is not in this account check.", 404)
        await _turn_off_two_factor(db, user_id)
        label = TWO_FACTOR_LABEL
    else:
        if not items["password"]:
            raise ReviewError("That item is not in this account check.", 404)
        from remembra.auth.users import UserManager

        # A random hash nobody knows: the password no longer signs in.
        # "Forgot password" sets a new one (proven through the mailbox).
        await db.update_user_password(user_id, UserManager.hash_password(secrets.token_urlsafe(48)))
        await db.conn.execute("UPDATE account_reviews SET password_status = ? WHERE user_id = ?", (PASSWORD_REMOVED, user_id))
        await db.conn.commit()
        sessions_reset = True
        label = "Password"

    if sessions_reset:
        from remembra.security import state as security_state

        # Dashboard sessions only: the owner's agents (app connections, keys)
        # keep working; they are items of their own in this review.
        await security_state.invalidate_user_sessions(db, user_id, keep_app_connections=True)
    await audit(
        db,
        user_id,
        AuditAction.ACCOUNT_REVIEW_REVOKED,
        resource_id=f"{kind}:{item_id}" if item_id else kind,
        ip=ip,
        details={"kind": kind, "label": label, "by": method},
    )
    log.info("account_review_item_revoked", user_id=user_id, kind=kind)
    finished = await finish_if_empty(db, review, ip=ip, method=method)
    return {"label": label, "sessions_reset": sessions_reset, "finished": finished}


async def keep_two_factor(db: Any, review: Review, *, ip: str | None, method: str) -> None:
    """The owner showed a current code from the authenticator: 2FA is theirs and stays on.

    The caller verifies the code (``UserManager.verify_totp``) first.
    """
    if not review.pending:
        raise ReviewError("This account check is already done.", 409)
    if not (await review_items(db, review))["two_factor"]:
        raise ReviewError("That item is not in this account check.", 404)
    await db.conn.execute("UPDATE account_reviews SET totp_status = ? WHERE user_id = ?", (TOTP_TRUSTED, review.user_id))
    await db.conn.commit()
    await audit(
        db,
        review.user_id,
        AuditAction.ACCOUNT_REVIEW_KEPT,
        resource_id="two_factor",
        ip=ip,
        details={"kind": "two_factor", "label": TWO_FACTOR_LABEL, "by": method, "proof": "current_code"},
    )


async def defer(db: Any, review: Review, *, ip: str | None, method: str) -> None:
    await audit(db, review.user_id, AuditAction.ACCOUNT_REVIEW_DEFERRED, resource_id=review.origin, ip=ip, details={"by": method})


async def _audit_labels(db: Any, review: Review, action: AuditAction) -> list[str]:
    rows = await _rows(
        db,
        "SELECT error_message FROM audit_log WHERE user_id = ? AND action = ? AND timestamp >= ? ORDER BY timestamp",
        (review.user_id, action.value, review.created_at),
    )
    labels: list[str] = []
    for (raw,) in rows:
        try:
            label = json.loads(raw or "{}").get("label")
        except ValueError:
            label = None
        if isinstance(label, str):
            labels.append(label)
    return labels


async def _send_review_notice(to: str, kept: list[str], removed: list[str]) -> None:
    from remembra.cloud.email import email_service_or_none

    service = email_service_or_none()
    if service is None:
        return
    await service.send_account_review_email(to, kept=kept, removed=removed)


# Swapped in tests; called with (account_email, kept_labels, removed_labels).
review_notifier: Callable[[str, list[str], list[str]], Awaitable[None]] = _send_review_notice


async def complete(db: Any, review: Review, *, version: str | None, ip: str | None, method: str) -> dict[str, list[str]]:
    """Keep what the owner was shown and close the review (single use). Emails the account address.

    ``version`` is :func:`items_version` of the list the owner saw; when the
    list changed since (say a key was made meanwhile) nothing is kept and 409
    is raised, so the owner sees the new list. ``None`` only for the automatic
    finish of an empty review.

    2FA set up before the email was confirmed is never kept by "Keep all": it
    is turned off here unless the owner kept it with a current code
    (:func:`keep_two_factor`), so an authenticator the owner may not hold
    never locks them out after the review.
    """
    if not review.pending:
        raise ReviewError("This account check is already done.", 409)
    items = await review_items(db, review)
    if version is not None and not secrets.compare_digest(version, items_version(items)):
        raise ReviewError("The list changed. Check it again.", 409)
    user_id = review.user_id
    if items["two_factor"]:
        await _turn_off_two_factor(db, user_id)
        await audit(
            db,
            user_id,
            AuditAction.ACCOUNT_REVIEW_REVOKED,
            resource_id="two_factor",
            ip=ip,
            details={"kind": "two_factor", "label": TWO_FACTOR_LABEL, "by": method, "reason": "not_kept_with_code"},
        )
        items = {**items, "two_factor": False}
    cursor = await db.conn.execute(
        "UPDATE account_reviews SET completed_at = ?, completed_by = ? WHERE user_id = ? AND completed_at IS NULL",
        (now_iso(), method, user_id),
    )
    await db.conn.commit()
    if (cursor.rowcount or 0) != 1:
        raise ReviewError("This account check is already done.", 409)
    kept = item_labels(items) + await _audit_labels(db, review, AuditAction.ACCOUNT_REVIEW_KEPT)
    removed = await _audit_labels(db, review, AuditAction.ACCOUNT_REVIEW_REVOKED)
    await audit(
        db,
        user_id,
        AuditAction.ACCOUNT_REVIEW_COMPLETED,
        resource_id=review.origin,
        ip=ip,
        details={"kept": kept, "removed": removed, "by": method},
    )
    log.info("account_review_completed", user_id=user_id, kept=len(kept), removed=len(removed))
    user = await db.get_user_by_id(user_id)
    if (kept or removed) and user and user.get("email"):
        try:
            await review_notifier(str(user["email"]), kept, removed)
        except Exception as e:  # the notice never fails the review
            log.warning("account_review_notice_failed", user_id=user_id, error_type=type(e).__name__)
    return {"kept": kept, "removed": removed}


async def finish_if_empty(db: Any, review: Review, *, ip: str | None, method: str = METHOD_NOTHING_TO_CHECK) -> bool:
    """Close a pending review that lists nothing (no screen). True when it is (now) finished.

    Emails only when something was removed along the way.
    """
    current = await get_review(db, review.user_id)
    if current is None or not current.pending:
        return current is not None
    if item_ids(await review_items(db, current)):
        return False
    try:
        await complete(db, current, version=None, ip=ip, method=method)
    except ReviewError:  # finished concurrently
        pass
    return True
