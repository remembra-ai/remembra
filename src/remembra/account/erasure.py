"""Account erasure (R-23): remove every row and vector a deleted account owns.

A self-serve deletion (``DELETE /api/v1/auth/me``) stamps ``users.deleted_at``.
Once the grace period (``account_erasure_grace_days``) has passed, the erasure
job deletes, for that account:

* its Qdrant points (delete-by-filter on the ``user_id`` payload), first, so a
  failure leaves the SQL rows in place and the next run retries. Every
  collection of the app is swept, not only the active one: a rebuild reindex
  keeps the collection it replaced for rollback, and that copy holds the same
  content (``QdrantStore.delete_by_user_everywhere``);
* every SQLite row it owns, in one transaction, following :data:`ERASURE_RULES`
  (rows keyed by the user id, plus rows that hang off its memories, entities,
  teams, spaces, webhooks, API keys and OAuth grants);
* any other table found at runtime with a user-keyed column (``user_id``,
  ``*_user_id``, ``owner_id``): the schema is read from ``sqlite_master`` each
  run, so a table added later is erased even before it is registered (and the
  schema test fails until it is). Actor columns (``invited_by``, ``added_by``,
  ...) of such a table are set to NULL, never used to delete: a row the account
  merely acted on belongs to someone else.

Another SQLite database (for example Crew mode's ``crew.db``) is erased only
through an :class:`ExtraDatabase` that carries its own explicit rules; the
generic scan alone cannot tell whose row a crew table holds.

What remains is one audit row, ``account_erased``, holding only a SHA-256 of
the account id and row counts: no email, no content. Support can confirm an
erasure by hashing the id a customer gives them (:func:`erasure_digest`).

Backups are not rewritten: copies age out on their own schedule (pre-deploy
database copies after the newest ``pre_migration_backup_keep`` deploys, the
litestream replica after its retention window). The privacy page says so.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from remembra.security import state as security_state
from remembra.security.audit import AuditAction

log = structlog.get_logger(__name__)

# Columns whose value is the id of the user that owns the row.
_USER_KEY_COLUMN = re.compile(r"^(user_id|owner_id|[a-z0-9_]*_user_id)$")
# Columns recording which user did something to a row (inviter, granter, ...).
ACTOR_COLUMNS = frozenset({"invited_by", "granted_by", "added_by", "created_by"})


# Named like a user column but holding someone else's id (the Google/GitHub account id).
_NOT_USER_COLUMNS = frozenset({"provider_user_id"})


def is_user_key_column(column: str) -> bool:
    return column not in _NOT_USER_COLUMNS and bool(_USER_KEY_COLUMN.match(column))


def erasure_digest(user_id: str) -> str:
    """Content-free, stable reference to an erased account (kept in the audit log)."""
    return hashlib.sha256(f"remembra-erasure:v1:{user_id}".encode()).hexdigest()


@dataclass(frozen=True)
class TableRule:
    """How one table's rows for an account are removed.

    ``deletes``: WHERE clauses, each run as ``DELETE FROM table WHERE ...``.
    ``nulls``: (column, WHERE) pairs set to NULL instead (a user who only
    acted on someone else's row, e.g. invited a member to another team).
    Parameters: ``:uid`` (user id), ``:email`` (lower-case address),
    ``:login_key`` (the login-lockout key of that address).
    """

    table: str
    deletes: tuple[str, ...] = ()
    nulls: tuple[tuple[str, str], ...] = ()

    def columns(self) -> set[str]:
        """Columns of ``table`` this rule matches on (for the coverage test)."""
        out: set[str] = set()
        for clause in (*self.deletes, *(where for _col, where in self.nulls)):
            # Only the leading "<column> = / IN" of each clause names this table's column.
            match = re.match(r"\s*(?:LOWER\()?(\w+)\)?\s*(=|IN)", clause)
            if match:
                out.add(match.group(1))
        return out


_MEMORIES = "SELECT id FROM memories WHERE user_id = :uid"
_ENTITIES = "SELECT id FROM entities WHERE user_id = :uid"
_TEAMS = "SELECT id FROM teams WHERE owner_id = :uid"
_SPACES = "SELECT id FROM memory_spaces WHERE owner_id = :uid"
_WEBHOOKS = "SELECT id FROM webhooks WHERE user_id = :uid"
_KEYS = "SELECT id FROM api_keys WHERE user_id = :uid"
_GRANTS = "SELECT grant_id FROM oauth_grants WHERE user_id = :uid"


def _by_user(table: str, column: str = "user_id") -> TableRule:
    return TableRule(table, deletes=(f"{column} = :uid",))


# Children before parents: a clause that selects parent ids must run while the
# parent rows still exist. Every table in a fully initialised database is either
# here or in EXEMPT_TABLES (tests/test_account_erasure.py enforces it).
ERASURE_RULES: tuple[TableRule, ...] = (
    TableRule("memory_entities", deletes=(f"memory_id IN ({_MEMORIES})", f"entity_id IN ({_ENTITIES})")),
    TableRule(
        "relationships",
        deletes=(
            f"from_entity_id IN ({_ENTITIES})",
            f"to_entity_id IN ({_ENTITIES})",
            f"source_memory_id IN ({_MEMORIES})",
        ),
    ),
    TableRule("memory_feedback", deletes=("user_id = :uid", f"memory_id IN ({_MEMORIES})")),
    TableRule(
        "memory_space_membership",
        deletes=(f"memory_id IN ({_MEMORIES})", f"space_id IN ({_SPACES})", "added_by = :uid"),
    ),
    TableRule("space_access", deletes=(f"space_id IN ({_SPACES})", "agent_id = :uid", "granted_by = :uid")),
    TableRule("space_invites", deletes=(f"space_id IN ({_SPACES})", "agent_id = :uid", "invited_by = :uid")),
    TableRule("team_spaces", deletes=(f"team_id IN ({_TEAMS})", f"space_id IN ({_SPACES})", "created_by = :uid")),
    TableRule("team_invites", deletes=(f"team_id IN ({_TEAMS})", "invited_by = :uid", "LOWER(email) = :email")),
    TableRule(
        "team_members",
        deletes=(f"team_id IN ({_TEAMS})", "user_id = :uid"),
        nulls=(("invited_by", "invited_by = :uid"),),
    ),
    _by_user("teams", "owner_id"),
    _by_user("memory_spaces", "owner_id"),
    _by_user("memories_fts"),
    _by_user("memories"),
    _by_user("archived_memories"),
    _by_user("pending_embeddings"),
    _by_user("entities"),
    _by_user("communities"),
    _by_user("adaptive_thresholds"),
    _by_user("decision_log"),
    _by_user("memory_conflicts"),
    _by_user("idempotency_keys"),
    _by_user("project_fingerprints"),
    _by_user("project_links"),
    _by_user("relay_pickups"),
    _by_user("agent_inbox", "owner_user_id"),
    TableRule("webhook_deliveries", deletes=(f"webhook_id IN ({_WEBHOOKS})",)),
    _by_user("webhooks"),
    TableRule("api_key_roles", deletes=(f"api_key_id IN ({_KEYS})",)),
    _by_user("api_keys"),
    TableRule("oauth_codes", deletes=(f"grant_id IN ({_GRANTS})",)),
    TableRule("oauth_tokens", deletes=(f"grant_id IN ({_GRANTS})",)),
    _by_user("oauth_grants"),
    _by_user("oauth_auth_requests"),
    _by_user("oauth_login_codes"),
    _by_user("oauth_link_tickets"),
    _by_user("oauth_login_states", "link_user_id"),
    _by_user("user_identities"),
    _by_user("password_reset_tokens"),
    _by_user("token_blacklist"),
    _by_user("security_user_state"),
    _by_user("security_totp_used"),
    _by_user("security_email_verifications"),
    TableRule("security_login_attempts", deletes=("account_key = :login_key",)),
    _by_user("account_deletion_codes"),
    _by_user("promo_redemptions"),
    _by_user("reindex_jobs"),
    _by_user("audit_log"),
    _by_user("cloud_credit_reservations"),
    _by_user("cloud_credit_periods"),
    _by_user("cloud_usage_daily"),
    _by_user("founding_holds"),
    _by_user("cloud_tenants"),
    TableRule("users", deletes=("id = :uid",)),
)

# Tables that hold no row of any one account (with the reason).
EXEMPT_TABLES: dict[str, str] = {
    "cloud_ai_spend_monthly": "platform AI spend per month and group, no account column",
    "cloud_revenue_events": "net revenue per Paddle transaction id, the accounting record (no account column)",
    "cloud_migrations": "one-time data migration markers",
    "schema_version": "applied schema migrations",
    "vector_store_state": "which Qdrant collection is active",
    "oauth_clients": "public OAuth client registrations (RFC 7591), not tied to an account",
    "sqlite_sequence": "SQLite internal",
    "sqlite_stat1": "SQLite internal",
}
# FTS5 keeps the index of memories_fts in shadow tables; deleting from
# memories_fts removes the matching shadow rows.
EXEMPT_PREFIXES: tuple[str, ...] = ("memories_fts_", "sqlite_")


def is_exempt(table: str) -> bool:
    return table in EXEMPT_TABLES or table.startswith(EXEMPT_PREFIXES)


def registry_problems(
    schema: Mapping[str, Iterable[str]],
    rules: tuple[TableRule, ...],
    exempt: Mapping[str, str],
    exempt_prefixes: tuple[str, ...] = (),
) -> list[str]:
    """Why ``rules`` do not cover ``schema`` ({table: column names}); empty means fully covered.

    Every table must have a rule or an exemption, and every user-keyed or
    actor column of a ruled table must be matched by its rule. The coverage
    test of any database the eraser touches (the main one here, ``crew.db`` on
    its own branch) runs this against the real migrated schema.
    """
    by_table = {rule.table: rule for rule in rules}
    problems: list[str] = []
    for table, columns in schema.items():
        keyed = {c for c in columns if is_user_key_column(c) or c in ACTOR_COLUMNS}
        if table in exempt or table.startswith(exempt_prefixes):
            if keyed and table in exempt:
                problems.append(f"{table}: exempt but has user columns {sorted(keyed)}")
            continue
        rule = by_table.get(table)
        if rule is None:
            problems.append(f"{table}: no erasure rule and no exemption")
            continue
        missing = keyed - rule.columns()
        if missing:
            problems.append(f"{table}: rule does not match user columns {sorted(missing)}")
    return problems


def _tables_in(clause: str) -> set[str]:
    return set(re.findall(r"FROM (\w+)", clause))


async def _schema(conn: Any) -> dict[str, dict[str, bool]]:
    """{table: {column: NOT NULL}} read from the live database."""
    cursor = await conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    tables = [str(row[0]) for row in await cursor.fetchall()]
    out: dict[str, dict[str, bool]] = {}
    for table in tables:
        info = await conn.execute(f'PRAGMA table_info("{table}")')
        out[table] = {str(row[1]): bool(row[3]) for row in await info.fetchall()}
    return out


@dataclass
class ErasureReceipt:
    user_id: str
    digest: str
    rows: dict[str, int] = field(default_factory=dict)
    vectors: int = 0
    unregistered_tables: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return sum(self.rows.values())


async def erase_rows(
    conn: Any,
    user_id: str,
    email: str | None,
    *,
    rules: tuple[TableRule, ...] = ERASURE_RULES,
    exempt: Mapping[str, str] = EXEMPT_TABLES,
    exempt_prefixes: tuple[str, ...] = EXEMPT_PREFIXES,
) -> tuple[dict[str, int], list[str]]:
    """Delete every row ``user_id`` owns on ``conn`` (caller holds the transaction).

    Tables no rule names go through the safety net: rows whose user-keyed
    column is the account are deleted, and actor columns pointing at it are
    set to NULL (a NOT NULL actor column is left and logged; the coverage test
    fails until the table has a rule). Returns (rows deleted per table, tables
    the safety net touched because no rule names them).
    """
    params = {
        "uid": user_id,
        "email": email.strip().lower() if email else None,  # NULL never matches
        "login_key": security_state.account_key("login", email) if email else "",
    }
    schema = await _schema(conn)
    counts: dict[str, int] = {}
    ruled: set[str] = set()
    for rule in rules:
        ruled.add(rule.table)
        if rule.table not in schema:
            continue
        for column, where in rule.nulls:
            if column in schema[rule.table] and _tables_in(where) <= schema.keys():
                await conn.execute(f'UPDATE "{rule.table}" SET "{column}" = NULL WHERE {where}', params)
        for where in rule.deletes:
            if not _tables_in(where) <= schema.keys():
                continue  # the parent table does not exist, so neither do its rows
            cursor = await conn.execute(f'DELETE FROM "{rule.table}" WHERE {where}', params)
            if cursor.rowcount and cursor.rowcount > 0:
                counts[rule.table] = counts.get(rule.table, 0) + cursor.rowcount
    unregistered: list[str] = []
    for table, columns in schema.items():
        if table in ruled or table in exempt or table.startswith(exempt_prefixes):
            continue
        keys = [c for c in columns if is_user_key_column(c)]
        actors = [c for c in columns if c in ACTOR_COLUMNS and c not in keys]
        if not keys and not actors:
            continue
        unregistered.append(table)
        if keys:
            where = " OR ".join(f'"{c}" = :uid' for c in keys)
            cursor = await conn.execute(f'DELETE FROM "{table}" WHERE {where}', params)
            if cursor.rowcount and cursor.rowcount > 0:
                counts[table] = counts.get(table, 0) + cursor.rowcount
        for column in actors:
            if columns[column]:
                log.warning("erasure_actor_column_not_nullable", table=table, column=column)
                continue
            await conn.execute(f'UPDATE "{table}" SET "{column}" = NULL WHERE "{column}" = :uid', params)
    if unregistered:
        log.warning("erasure_unregistered_tables", tables=sorted(unregistered))
    return counts, unregistered


@dataclass(frozen=True)
class ExtraDatabase:
    """Another SQLite database the eraser covers, with its own explicit rules.

    ``db`` has ``.conn`` and ``.transaction()``. ``rules`` must name every
    table that can hold an account's rows (children before parents), and
    ``exempt`` every other table with the reason; the database's own coverage
    test runs :func:`registry_problems` over its real migrated schema. There is
    no rule-less mode: the generic scan cannot tell a row the account owns from
    a row it only acted on (``added_by``) in someone else's crew, nor find
    content keyed by a session or message id.
    """

    name: str
    db: Any
    rules: tuple[TableRule, ...]
    exempt: Mapping[str, str] = field(default_factory=dict)
    exempt_prefixes: tuple[str, ...] = ("sqlite_",)

    def __post_init__(self) -> None:
        if not self.rules:
            raise ValueError(f"extra database {self.name!r} needs explicit erasure rules")


class AccountEraser:
    """Erases accounts: Qdrant points, then every SQLite row, then a content-free receipt.

    ``extra_databases`` are further SQLite databases, each an :class:`ExtraDatabase`
    carrying its own erasure rules (e.g. Crew mode's ``crew.db``).
    """

    def __init__(self, db: Any, qdrant: Any | None, *, extra_databases: list[ExtraDatabase] | None = None) -> None:
        self._db = db
        self._qdrant = qdrant
        self._extra = [d for d in (extra_databases or []) if d is not None]
        for extra in self._extra:
            if not isinstance(extra, ExtraDatabase):
                raise TypeError("extra_databases takes ExtraDatabase entries (a database plus its erasure rules)")

    async def _reindex_collections(self) -> set[str]:
        """Collections recorded by reindex jobs (rollback copies may have been renamed by config since)."""
        try:
            cursor = await self._db.conn.execute("SELECT source_collection, target_collection FROM reindex_jobs")
            rows = await cursor.fetchall()
        except Exception:  # no reindex has ever run on this database
            return set()
        return {str(name) for row in rows for name in row if name}

    async def _email_of(self, user_id: str) -> str | None:
        for query in ("SELECT email FROM users WHERE id = ?", "SELECT email FROM cloud_tenants WHERE user_id = ?"):
            try:
                cursor = await self._db.conn.execute(query, (user_id,))
                row = await cursor.fetchone()
            except Exception:  # table missing in a minimal deployment
                continue
            if row and row[0]:
                return str(row[0])
        return None

    async def erase(self, user_id: str) -> ErasureReceipt:
        """Erase ``user_id`` now.

        Order: vectors, then extra databases, then the main database (last, in
        one transaction with the receipt). A failure anywhere raises before the
        ``users`` row is gone, so the next run of the job finds and retries it.
        """
        email = await self._email_of(user_id)
        receipt = ErasureReceipt(user_id=user_id, digest=erasure_digest(user_id))
        if self._qdrant is not None:
            also = await self._reindex_collections()
            receipt.vectors = int(await self._qdrant.delete_by_user_everywhere(user_id, also=also))
        for extra in self._extra:
            async with extra.db.transaction():
                rows, unregistered = await erase_rows(
                    extra.db.conn,
                    user_id,
                    email,
                    rules=extra.rules,
                    exempt=extra.exempt,
                    exempt_prefixes=extra.exempt_prefixes,
                )
            for table, n in rows.items():
                receipt.rows[f"{extra.name}:{table}"] = n
            receipt.unregistered_tables.extend(f"{extra.name}:{t}" for t in unregistered)
        async with self._db.transaction():
            rows, unregistered = await erase_rows(self._db.conn, user_id, email)
            receipt.rows.update(rows)
            receipt.unregistered_tables.extend(unregistered)
            now = datetime.now(UTC)
            await self._db.conn.execute(
                "INSERT INTO audit_log (id, timestamp, user_id, api_key_id, action, resource_id, ip_address, success,"
                " error_message) VALUES (?, ?, ?, NULL, ?, ?, NULL, 1, ?)",
                (
                    f"audit_{secrets.token_urlsafe(16)}",
                    now.isoformat(),
                    f"erased:{receipt.digest[:32]}",
                    AuditAction.ACCOUNT_ERASED.value,
                    f"sha256:{receipt.digest}",
                    f"rows={receipt.total_rows};vectors={receipt.vectors}",
                ),
            )
        from remembra.auth import keys as keys_module

        keys_module.evict_user_from_cache(user_id)
        log.info(
            "account_erased",
            digest=receipt.digest[:16],
            rows=receipt.total_rows,
            vectors=receipt.vectors,
            tables=len(receipt.rows),
        )
        return receipt

    async def due_accounts(self, grace: timedelta, now: datetime | None = None) -> list[str]:
        """Accounts whose self-serve deletion is older than ``grace``."""
        now = now or datetime.now(UTC)
        try:
            cursor = await self._db.conn.execute("SELECT id, deleted_at FROM users WHERE deleted_at IS NOT NULL")
        except Exception as e:  # pre-migration database
            log.warning("erasure_due_query_failed", error_type=type(e).__name__)
            return []
        due: list[str] = []
        for user_id, deleted_at in await cursor.fetchall():
            try:
                stamp = datetime.fromisoformat(str(deleted_at).replace("Z", "+00:00"))
            except ValueError:
                stamp = now  # unreadable stamp: erase on the next run past the grace
            stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
            if stamp + grace <= now:
                due.append(str(user_id))
        return due

    async def erase_due(self, grace: timedelta, now: datetime | None = None) -> list[ErasureReceipt]:
        """Erase every account past its grace period; one failure never blocks the others."""
        receipts: list[ErasureReceipt] = []
        for user_id in await self.due_accounts(grace, now):
            try:
                receipts.append(await self.erase(user_id))
            except Exception as e:
                log.error("account_erasure_failed", digest=erasure_digest(user_id)[:16], error_type=type(e).__name__)
        return receipts


async def run_erasure_loop(eraser: AccountEraser, *, grace_days: int, interval_seconds: float) -> None:
    """Background loop: erase accounts past the grace period, then sleep."""
    grace = timedelta(days=grace_days)
    while True:
        try:
            receipts = await eraser.erase_due(grace)
            if receipts:
                log.info("account_erasure_run", erased=len(receipts))
        except Exception as e:  # never let the loop die
            log.error("account_erasure_run_failed", error_type=type(e).__name__)
        await asyncio.sleep(interval_seconds)


def eraser_for(app_state: Any) -> AccountEraser:
    """The app's eraser (``app.state.account_eraser``), else one over its database and vector store."""
    existing = getattr(app_state, "account_eraser", None)
    if existing is not None:
        return existing  # type: ignore[no-any-return]
    qdrant = getattr(app_state, "qdrant", None)
    if qdrant is None:
        qdrant = getattr(getattr(app_state, "memory_service", None), "qdrant", None)
    return AccountEraser(app_state.db, qdrant)
