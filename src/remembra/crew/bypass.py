"""Human-issued bypass codes (WP-5, spec D34, §8.4, §11.2).

There is no agent-usable bypass. A human, from a fresh dashboard login (step-up), issues a
**single-use** code of at most 15 minutes for **one named session** and a scope
(``push``, ``commit``, ``write:<zone>`` …). The gate or git hook passes it to
``POST /bypass-codes/redeem`` with that session's token; the server validates it, marks it used
in the same transaction, and records ``guard.bypass_used`` (a moment), a Needs-you item and an
audit row. Only the code's sha256 is stored; the plain code is returned once, at issue, and is
never written to the idempotency cache.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Final

from remembra.crew import schemas as S
from remembra.crew.store import new_id, now_iso, parse_iso
from remembra.crew.zones import CrewOpError, CrewOps, Principal, fetchone, raise_inbox_item, utcnow

CODE_ALPHABET: Final = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford base32 (no I, L, O, U)
_CODE_RE: Final = re.compile(S.BYPASS_CODE_PATTERN)
_SCOPE_RE: Final = re.compile(r"[a-z][a-z0-9:_.-]{0,63}")
INVALID: Final = "This bypass code is not valid for this session (unknown, used, expired or issued for another session)."


def new_code() -> str:
    chars = "".join(secrets.choice(CODE_ALPHABET) for _ in range(10))
    return f"RCB-{chars[:5]}-{chars[5:]}"


def hash_code(code: str) -> str:
    return hashlib.sha256(code.strip().upper().encode("ascii", "replace")).hexdigest()


def bypass_view(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "session_id": row.get("session_id"),
        "scope": row["scope"],
        "issued_by": row["issued_by"],
        "expires_at": row["expires_at"],
        "used_at": row.get("used_at"),
        "created_at": row["created_at"],
    }


async def issue_code(
    ops: CrewOps, crew_id: str, human: Principal, *, session_id: str, scope: str, minutes: int
) -> dict[str, Any]:
    """Issue a code (human + step-up, route-enforced). Returns the plain code exactly once."""
    if not human.is_privileged:
        raise CrewOpError(403, "human_only", "Bypass codes are issued from a dashboard login only.")
    if not isinstance(minutes, int) or isinstance(minutes, bool) or not 1 <= minutes <= S.BYPASS_CODE_MAX_MINUTES:
        raise CrewOpError(422, "invalid_bypass", f"minutes must be between 1 and {S.BYPASS_CODE_MAX_MINUTES}.")
    if not isinstance(scope, str) or not _SCOPE_RE.fullmatch(scope):
        raise CrewOpError(422, "invalid_bypass", "scope must be a short lower-case word such as push, commit or write:pos.")
    code = new_code()
    async with ops.log.transaction() as tx:
        session = (
            await fetchone(tx.conn, "SELECT * FROM crew_sessions WHERE id = ?", (session_id,))
            if S.is_id("session", session_id)
            else None
        )
        if session is None or session["crew_id"] != crew_id:
            raise CrewOpError(422, "cross_crew_reference", "The referenced session is not part of this crew.")
        if session["state"] == "ended":
            raise CrewOpError(422, "invalid_bypass", "That session has ended.")
        code_id = new_id("bypass_code")
        now = utcnow()
        expires = now_iso(now + timedelta(minutes=minutes))
        await tx.conn.execute(
            """INSERT INTO crew_bypass_codes (id, crew_id, code_hash, session_id, scope, issued_by, expires_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (code_id, crew_id, hash_code(code), session_id, scope, human.user_id, expires, now_iso(now)),
        )
        ops.audit(
            human.user_id,
            "crew.bypass_issued",
            code_id,
            {"session": session_id, "scope": scope, "minutes": minutes, "api_key_id": human.api_key_id},
        )
        return {"code_id": code_id, "code": code, "session_id": session_id, "scope": scope, "expires_at": expires}


async def redeem_code(
    ops: CrewOps, session: Mapping[str, Any], code: str, *, surface: str, zone: str | None = None
) -> dict[str, Any]:
    """Validate and consume a code for ``session`` (its token was already checked). 403 ``invalid_code`` otherwise.

    The code's scope must cover ``surface`` (and ``zone`` for ``write:<zone>``): a code issued for
    ``push`` never opens a commit or a pre-write deny. A mismatch is refused without consuming the
    code. ``surface = tty`` (a human typing it into ``remembra-crew bypass``) redeems it into crewd,
    which then applies the returned scope and expiry at use.
    """
    if not isinstance(code, str) or not _CODE_RE.fullmatch(code.strip().upper()):
        raise CrewOpError(403, "invalid_code", INVALID)
    if surface not in S.BYPASS_SURFACES:
        raise CrewOpError(422, "invalid_bypass", f"surface must be one of {', '.join(S.BYPASS_SURFACES)}.")
    crew_id = str(session["crew_id"])
    principal = Principal.for_session(session)
    async with ops.log.transaction() as tx:
        row = await fetchone(
            tx.conn, "SELECT * FROM crew_bypass_codes WHERE crew_id = ? AND code_hash = ?", (crew_id, hash_code(code))
        )
        now = utcnow()
        if (
            row is None
            or row["session_id"] != session["id"]
            or row["used_at"] is not None
            or parse_iso(str(row["expires_at"])) <= now
        ):
            raise CrewOpError(403, "invalid_code", INVALID)
        if not S.bypass_scope_matches(str(row["scope"]), surface, zone):
            raise CrewOpError(
                403, "bypass_scope_mismatch", f"This bypass code was issued for {row['scope']}, not for this {surface}."
            )
        cur = await tx.conn.execute(
            "UPDATE crew_bypass_codes SET used_at = ? WHERE id = ? AND used_at IS NULL", (now_iso(now), row["id"])
        )
        if (cur.rowcount or 0) != 1:
            raise CrewOpError(403, "invalid_code", INVALID)
        res = await tx.emit(
            crew_id=crew_id,
            type="guard.bypass_used",
            actor=principal.actor(),
            payload={"code_id": row["id"], "scope": row["scope"]},  # surface: in the audit row
            summary=f"{principal.name} used a human bypass code ({row['scope']})",
            severity="high",
            refs={"session_id": session["id"]},
        )
        await raise_inbox_item(
            tx,
            crew_id,
            audience="project",
            kind="bypass_used",
            title=f"{principal.name} used a bypass code ({row['scope']})",
            dedupe_key=f"bypass:{row['id']}",
            ref_type="bypass_code",
            ref_id=str(row["id"]),
            actor=principal.actor(),
        )
        ops.audit(
            str(session["user_id"]),
            "crew.bypass_used",
            str(row["id"]),
            {"session": session["id"], "scope": row["scope"], "surface": surface, "zone": zone},
        )
        return {"ok": True, "code_id": row["id"], "scope": row["scope"], "expires_at": row["expires_at"], "seq": res.seq}
