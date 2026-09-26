"""The local snapshot writer and reader (spec §4.4, §8.1 ``snapshot/<crew>.json``, docs/crew/snapshot.md).

crewd fetches ``GET /crews/{id}/snapshot`` and adds what only this host knows: when it synced
(``synced_at``, server time), the measured clock skew, this host's id, **this host's checkouts**
(one entry per checkout and session, with case sensitivity), the gate's settings subset,
whether no-zone bootstrap zones are in force, and an HMAC from a crewd-held key (tamper
evidence only, never a security boundary). The file is replaced atomically; the gate only reads it.

Clock rule (D31): the server clock is the one clock. ``skew_s = local_now - server_time`` at
sync; the gate computes ``server_now = local_now - skew_s`` for lease horizons and ages.

Stdlib only (vendored with the gate).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from remembra.crew import schemas as S
from remembra.relay.crew.outbox import atomic_write_json

SETTINGS_KEYS: Final = tuple(S.SNAPSHOT_SETTINGS.fields)
DEFAULT_SETTINGS: Final[Mapping[str, Any]] = {
    "enforcement": "enforce",
    "undeclared_policy": "footprint",
    "auto_claim": True,
    "auto_claim_leaf_only": True,
    "max_exclusive_claims_per_session": 3,
    "interactive_override": False,
    "fail_closed_zones": [],
    "lease_ttl_s": 600,
    "readonly_fence_for_advisory": True,
}
FRESH_S: Final = 15.0  # §8.2: use as is
ASYNC_REFRESH_S: Final = 120.0  # 15–120 s: use, refresh in the background; older: sync refresh (≤400 ms)


def parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def format_ts(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch, UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def settings_subset(settings: Mapping[str, Any] | None, enforcement: str | None = None) -> dict[str, Any]:
    """The ``SnapshotSettings`` the gate needs, from the crew settings (defaults where missing)."""
    src = dict(settings or {})
    out = {k: src.get(k, DEFAULT_SETTINGS[k]) for k in SETTINGS_KEYS}
    if enforcement in S.ENFORCEMENT_LEVELS:
        out["enforcement"] = enforcement
    out["fail_closed_zones"] = [str(z) for z in (out.get("fail_closed_zones") or [])][:50]
    return out


def checkout_entry(
    *,
    toplevel: str,
    worktree_id: str,
    git_common_dir: str,
    case_insensitive: bool,
    session_id: str | None,
    default_branch: str | None,
) -> dict[str, Any]:
    return {
        "toplevel": toplevel,
        "worktree_id": worktree_id,
        "git_common_dir": git_common_dir,
        "case_insensitive": bool(case_insensitive),
        "session_id": session_id,
        "default_branch": default_branch,
    }


def build_local_snapshot(
    server: Mapping[str, Any],
    *,
    settings: Mapping[str, Any] | None,
    host_id: str,
    checkouts: Iterable[Mapping[str, Any]],
    local_now: float,
    hmac_key: bytes,
) -> dict[str, Any]:
    """Merge a server snapshot with this host's view and seal it with the HMAC."""
    snap: dict[str, Any] = {k: v for k, v in server.items() if k != "hmac"}
    server_time = parse_ts(server.get("server_time"))
    skew = (local_now - server_time.timestamp()) if server_time else 0.0
    snap["synced_at"] = server.get("server_time") or format_ts(local_now)
    snap["skew_s"] = round(skew, 3)
    snap["host_id"] = host_id
    snap["checkouts"] = [dict(c) for c in checkouts][:50]
    crew = server.get("crew") or {}
    snap["settings"] = settings_subset(settings, crew.get("enforcement") if isinstance(crew, Mapping) else None)
    snap["bootstrap_zones"] = any((z or {}).get("source") == "suggested" for z in server.get("zones") or ())
    if isinstance(snap.get("tasks"), list):
        snap["tasks"] = [police_task(t) for t in snap["tasks"]]
    snap["hmac"] = S.snapshot_hmac(hmac_key, snap)
    return snap


def police_task(task: Any) -> Any:
    """A task with its agent-authored title under the brief's trust policy (R-14).

    The vendored gate is stdlib only and shows task titles (inside its data
    block) straight from this snapshot, so the title is policed here, before
    the snapshot is sealed: low-trust text becomes the fixed withheld note and
    commands, URLs and hidden characters are marked (:func:`remembra.relay.handoff.police_item`).
    """
    if not isinstance(task, Mapping) or not isinstance(task.get("title"), str):
        return task
    from remembra.relay.handoff import police_item

    return {**task, "title": police_item(task["title"])}


def snapshot_path(snapshot_dir: Path, crew_id: str) -> Path:
    safe = "".join(ch for ch in crew_id if ch.isalnum() or ch in "_-")[:80]
    return snapshot_dir / f"{safe}.json"


def write_snapshot(path: Path, snapshot: Mapping[str, Any]) -> None:
    atomic_write_json(path, dict(snapshot))


def load_snapshot(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def server_now(snapshot: Mapping[str, Any] | None, local_now: float) -> float:
    """The server clock estimated from the measured skew (D31)."""
    skew = 0.0
    if snapshot is not None:
        try:
            skew = float(snapshot.get("skew_s") or 0.0)
        except (TypeError, ValueError):
            skew = 0.0
    return local_now - skew


def age_s(snapshot: Mapping[str, Any] | None, local_now: float) -> float | None:
    """Seconds since the snapshot was synced, on the server clock; ``None`` without a snapshot."""
    if snapshot is None:
        return None
    synced = parse_ts(snapshot.get("synced_at"))
    if synced is None:
        return None
    return max(0.0, server_now(snapshot, local_now) - synced.timestamp())


def verify(snapshot: Mapping[str, Any], hmac_key: bytes) -> bool:
    return S.verify_snapshot_hmac(hmac_key, snapshot)
