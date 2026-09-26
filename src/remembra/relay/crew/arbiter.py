"""The crewd local arbiter (spec D12, D32, §8.1 "Local arbiter", §10.3).

While the server is unreachable, crewd grants and denies claims **among the sessions on this
host** from a local lease table (``arbiter.json``), with pid liveness as the truth:

* the table is seeded from the last snapshot (claims held by local sessions);
* an exclusive zone held by a live local session is denied to every other local session,
  **regardless of snapshot age** (fail-closed locally);
* a holder whose pid is confirmed dead frees the zone, except for a ``fail_closed`` zone, which
  becomes **reserved** (adopt-only: only a session in the dead holder's checkout may take it);
* every local grant is recorded as ``source='local_arbiter'`` and replayed to the server on
  reconnect; the server is the tie-breaker, and a lost race removes the local grant.

Remote holders are not decided here: the gate's offline policy (fresh snapshot, bounded skew,
``fail_closed_zones``) handles them. Stdlib only.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from remembra.relay.crew.outbox import atomic_write_json

ARBITER_VERSION: Final = 1
HOLD_STATES: Final = ("active", "offered", "reserved")


@dataclass(frozen=True)
class LocalSessionRef:
    session_id: str
    pid: int | None
    checkout: str | None  # realpath of the checkout's top level


@dataclass(frozen=True)
class ArbiterDecision:
    result: str  # granted | conflict
    winner_session_id: str | None = None
    reason: str = ""


class Arbiter:
    """The local lease table. Not thread-safe: crewd calls it from its one event loop."""

    def __init__(self, path: Path, *, is_alive: Callable[[int | None], bool], clock: Callable[[], float] = time.time) -> None:
        self.path = path
        self.is_alive = is_alive
        self.clock = clock
        self.data: dict[str, Any] = {"v": ARBITER_VERSION, "crews": {}}
        self.load()

    # -- persistence ------------------------------------------------------
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(raw, dict) and isinstance(raw.get("crews"), dict):
            self.data = raw

    def save(self) -> None:
        atomic_write_json(self.path, self.data)

    def leases(self, crew_id: str) -> dict[str, dict[str, Any]]:
        crew = self.data["crews"].setdefault(crew_id, {"leases": {}})
        leases: dict[str, dict[str, Any]] = crew.setdefault("leases", {})
        return leases

    # -- seeding ----------------------------------------------------------
    def seed(self, crew_id: str, snapshot: Mapping[str, Any], local: Mapping[str, LocalSessionRef]) -> None:
        """Rebuild the table from a snapshot: every hold of a local session (keeps unreplayed local grants)."""
        leases = self.leases(crew_id)
        kept = {z: rec for z, rec in leases.items() if rec.get("source") == "local_arbiter" and not rec.get("replayed")}
        fresh: dict[str, dict[str, Any]] = {}
        zones = {z.get("id"): z for z in snapshot.get("zones") or ()}
        fail_closed = set((snapshot.get("settings") or {}).get("fail_closed_zones") or ())
        for c in snapshot.get("claims") or ():
            zid = c.get("zone_id")
            holder = c.get("holder_session_id")
            if not zid or c.get("state") not in HOLD_STATES or holder not in local:
                continue
            ref = local[holder]
            zone = zones.get(zid) or {}
            fresh[str(zid)] = {
                "holder_session_id": holder,
                "holder_pid": ref.pid,
                "checkout": ref.checkout,
                "mode": c.get("mode"),
                "state": "reserved" if c.get("state") == "reserved" else "active",
                "epoch": c.get("epoch"),
                "claim_id": c.get("id"),
                "fail_closed": bool(zone.get("fail_closed") or zone.get("slug") in fail_closed),
                "source": "snapshot",
                "replayed": True,
                "granted_at": self.clock(),
            }
        fresh.update(kept)
        self.data["crews"][crew_id] = {"leases": fresh, "seeded_at": self.clock()}
        self.save()

    # -- decisions ----------------------------------------------------------
    def decide(
        self,
        crew_id: str,
        zone_id: str,
        caller: LocalSessionRef,
        *,
        mode: str = "exclusive",
        fail_closed: bool = False,
    ) -> ArbiterDecision:
        """Grant or deny ``zone_id`` to a local session while the server is unreachable."""
        leases = self.leases(crew_id)
        rec = leases.get(zone_id)
        if rec is not None and rec.get("holder_session_id") != caller.session_id:
            holder_alive = self.is_alive(rec.get("holder_pid"))
            if rec.get("state") == "reserved" or (not holder_alive and (rec.get("fail_closed") or fail_closed)):
                # dead holder of a fail_closed zone: adopt-only, and only from the holder's own checkout
                rec["state"] = "reserved"
                if caller.checkout and rec.get("checkout") and caller.checkout == rec.get("checkout"):
                    return self._grant(leases, crew_id, zone_id, caller, mode, fail_closed, adopted_from=rec)
                self.save()
                return ArbiterDecision("conflict", rec.get("holder_session_id"), "reserved")
            if holder_alive and (rec.get("mode") == "exclusive" or mode == "exclusive"):
                return ArbiterDecision("conflict", rec.get("holder_session_id"), "held_locally")
            if not holder_alive:
                leases.pop(zone_id, None)
        if rec is not None and rec.get("holder_session_id") == caller.session_id and rec.get("state") != "reserved":
            return ArbiterDecision("granted", caller.session_id, "own")
        return self._grant(leases, crew_id, zone_id, caller, mode, fail_closed)

    def _grant(
        self,
        leases: dict[str, dict[str, Any]],
        crew_id: str,
        zone_id: str,
        caller: LocalSessionRef,
        mode: str,
        fail_closed: bool,
        adopted_from: Mapping[str, Any] | None = None,
    ) -> ArbiterDecision:
        leases[zone_id] = {
            "holder_session_id": caller.session_id,
            "holder_pid": caller.pid,
            "checkout": caller.checkout,
            "mode": mode,
            "state": "active",
            "epoch": None,
            "claim_id": None,
            "fail_closed": fail_closed or bool(adopted_from and adopted_from.get("fail_closed")),
            "source": "local_arbiter",
            "replayed": False,
            "granted_at": self.clock(),
            "adopted_from": adopted_from.get("holder_session_id") if adopted_from else None,
        }
        self.save()
        return ArbiterDecision("granted", caller.session_id, "adopted" if adopted_from else "local_grant")

    def release_session(self, crew_id: str, session_id: str) -> list[str]:
        leases = self.leases(crew_id)
        gone = [z for z, rec in leases.items() if rec.get("holder_session_id") == session_id and rec.get("state") != "reserved"]
        for z in gone:
            leases.pop(z, None)
        if gone:
            self.save()
        return gone

    def mark_dead(self, crew_id: str, session_id: str) -> list[str]:
        """A local holder's pid died: fail_closed zones become reserved, the others are freed."""
        leases = self.leases(crew_id)
        changed: list[str] = []
        for z, rec in list(leases.items()):
            if rec.get("holder_session_id") != session_id:
                continue
            if rec.get("fail_closed"):
                rec["state"] = "reserved"
            else:
                leases.pop(z, None)
            changed.append(z)
        if changed:
            self.save()
        return changed

    # -- reconciliation -------------------------------------------------------
    def pending_replay(self, crew_id: str) -> list[tuple[str, dict[str, Any]]]:
        return [
            (z, rec)
            for z, rec in self.leases(crew_id).items()
            if rec.get("source") == "local_arbiter" and not rec.get("replayed")
        ]

    def all_pending(self) -> list[tuple[str, str, dict[str, Any]]]:
        out: list[tuple[str, str, dict[str, Any]]] = []
        for crew_id in list(self.data["crews"]):
            out.extend((crew_id, z, rec) for z, rec in self.pending_replay(crew_id))
        return out

    def confirm(self, crew_id: str, zone_id: str, *, claim_id: str | None, epoch: int | None) -> None:
        rec = self.leases(crew_id).get(zone_id)
        if rec is not None:
            rec.update({"replayed": True, "claim_id": claim_id, "epoch": epoch})
            self.save()

    def lost_race(self, crew_id: str, zone_id: str) -> None:
        """The server gave the zone to someone else: drop the local grant (the loser is denied next time)."""
        self.leases(crew_id).pop(zone_id, None)
        self.save()
