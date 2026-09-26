"""The real lid-close soak for a Mac (spec §13.4 "24 h soak on macOS with lid close/open"; WP-15).

    python -m tests.crew.load.soak_macos --url https://<staging API> --key <test-account key> \\
        --jwt <test-account dashboard token> --hours 24 --out soak.json

The server must run on **another machine** (staging): a server on this laptop sleeps with it and its
reaper would never see the silence. Use a dedicated test account, never production.

It installs crew mode into a temp HOME with the real ``connect``, starts crewd and one fake Claude Code
session (``tests.crew.e2e.harness``) that holds POS and keeps editing it every minute while the lid is
open, and samples the crew every minute. Close and open the lid as often as you like. Every wall-clock
gap over 90 s is a sleep; for each one the report lists what the server recorded and checks §13.4:

* a sleep under 30 min: no ``session.lost``, the session active again after wake, POS still held by it;
* a sleep under 20 min: no real-time notification (``GET /notifications`` gains nothing);
* a longer sleep: ``lost`` and one alert are expected, then ``session.recovered`` on wake.

``--local`` runs against a local server instead, for a quick check of the plumbing only (no sleeps).
Exit status 0 when every sleep passed.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any


from tests.crew.e2e.harness import ServerProc, World


@dataclass
class External:
    """A server this script did not start (staging)."""

    url: str
    key: str
    jwt: str
    port: int = 0

    def stop(self) -> None:
        return None


def sample(world: World, crew: str, sid: str) -> dict[str, Any]:
    snap = world.snapshot(crew)
    s = next((x for x in snap["sessions"] if x["id"] == sid), None)
    claims = [c for c in snap["claims"] if c.get("holder_session_id") == sid]
    with world.human() as h:
        notes = h.get("/notifications").json()
    return {
        "at": time.time(),
        "state": s["state"] if s else "gone",
        "claims": [(c["zone_id"], c["state"]) for c in claims],
        "notifications": len(notes.get("notifications") or notes.get("items") or []),
        "last_seq": snap["crew"].get("last_seq") or snap.get("as_of_seq"),
    }


def judge(sleep_s: float, events: list[dict[str, Any]], after: dict[str, Any], alerts: int) -> dict[str, Any]:
    types = [e["type"] for e in events]
    lost = "session.lost" in types
    checks: dict[str, bool] = {"active_after_wake": after["state"] in ("active", "idle")}
    if sleep_s < 30 * 60:
        checks["not_lost"] = not lost
        checks["pos_still_held"] = any(state == "active" for _zone, state in after["claims"])
    else:
        checks["lost_then_recovered"] = lost and "session.recovered" in types
    if sleep_s < 20 * 60:
        checks["no_alert"] = alerts == 0
    return {"sleep_s": round(sleep_s), "types": types, "checks": checks, "ok": all(checks.values())}


def run(args: argparse.Namespace) -> dict[str, Any]:
    tmp = Path(args.workdir or tempfile.mkdtemp(prefix="crew-soak-"))
    world = World(tmp)
    local: ServerProc | None = None
    try:
        world.make_repos(("wt-a",))
        if args.local:
            local = world.start_server()
        else:
            world.server = External(args.url.rstrip("/"), args.key, args.jwt)  # type: ignore[assignment]
        world.connect()
        wts = {"wt-a": (tmp / "wt-a").resolve()}
        a = world.agent("A", wts["wt-a"])
        a.start()
        crew, sid = str(a.local_session()["crew_id"]), str(a.local_session()["session_id"])
        assert not a.write("src/app/pos/split.ts", "export const split = 0;\n")["denied"]
        samples = [sample(world, crew, sid)]
        sleeps: list[dict[str, Any]] = []
        end = time.time() + args.hours * 3600
        n = 0
        while time.time() < end:
            before = time.time()
            time.sleep(60)
            gap = time.time() - before
            n += 1
            if gap > 90:  # the lid was closed
                seq0 = samples[-1]["last_seq"] or 0
                a.cli("renew")  # what crewd's next heartbeat does on wake
                time.sleep(5)
                after = sample(world, crew, sid)
                events = world.events(crew, since=int(seq0))
                alerts = after["notifications"] - samples[-1]["notifications"]
                sleeps.append({"started": before, **judge(gap - 60, events, after, alerts)})
                samples.append(after)
                print(json.dumps(sleeps[-1]), flush=True)
            res = a.write("src/app/pos/split.ts", f"export const split = {n};\n")
            if res["denied"]:
                sleeps.append({"started": time.time(), "denied": res["reason"], "ok": False})
            samples.append(sample(world, crew, sid))
        return {"sleeps": sleeps, "samples": len(samples), "ok": all(s.get("ok") for s in sleeps)}
    finally:
        world.close()
        if local is not None:
            local.stop()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--url")
    p.add_argument("--key")
    p.add_argument("--jwt")
    p.add_argument("--local", action="store_true", help="a local server (plumbing check only; it sleeps with the Mac)")
    p.add_argument("--hours", type=float, default=24.0)
    p.add_argument("--workdir")
    p.add_argument("--out")
    args = p.parse_args(argv)
    if not args.local and not (args.url and args.key and args.jwt):
        p.error("--url, --key and --jwt (a staging test account) are required unless --local")
    report = run(args)
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())


__all__ = ["External", "judge", "main", "run", "sample"]
