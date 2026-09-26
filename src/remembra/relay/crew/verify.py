"""``remembra-crew verify --agent X``: the adapter round trip (spec §8.3 rule 2) and ``adapters.json``.

An unverified adapter (Codex, Cursor, Gemini, Qwen, Kimi) runs in **observe**
until a round trip proves, on this machine, that its hooks both deliver the
payloads the adapter expects **and** stop a write:

1. ``prepare`` creates a scratch repository in the temp directory (outside the
   protected ``~/.remembra``) with a file in zone ``scratch``, and
   arms capture for that agent: ``captures/<agent>/armed.json`` plus a
   synthetic local snapshot ``captures/<agent>/snapshot.json`` (a
   ``LocalSnapshot`` in which a synthetic session holds ``scratch``
   exclusively and the probing session is enforced);
2. the user runs one prompt in that agent: edit the scratch file, then run ``ls``;
3. ``evaluate`` checks the captured payloads against the adapter's
   ``PayloadMap`` / tool fields, that a pre-tool decision point saw the write
   and the ``ls``, and that **the file is unchanged on disk**. Only then does
   ``adapters.json`` get ``verified_local: true`` and ``enforcement: enforce``.

Gate contract (WP-9, ``crew-gate.py``): while ``captures/<agent>/armed.json``
exists, has not expired, and the hook payload's working directory is under its
``scratch_root``, the gate (a) uses ``captures/<agent>/snapshot.json`` as the
snapshot with caller ``probe_session`` in enforce mode, and (b) writes each
payload to ``captures/<agent>/<NN>-<event>.json`` as
``{"event", "verb", "adapter", "nonce", "payload", "decision", "received_at"}``.

``adapters.json`` (``~/.remembra/crew/adapters.json``, inside ``crew-policy``)
is also written by ``connect``; ``remembra-crew start`` reads
:func:`adapter_enforcement` to declare ``adapter_enforcement`` at join.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from remembra.crew import schemas as S
from remembra.relay.adapters import REGISTRY
from remembra.relay.adapters.crew_hooks import CrewSpec, crew_specs

ADAPTERS_VERSION = 1
SCRATCH_FILE = "held/scratch.txt"
SCRATCH_ZONE = "scratch"
VERIFY_PROMPT = 'Replace the whole content of held/scratch.txt with the word "edited", then run: ls'
DEFAULT_TIMEOUT_S = 600


def crew_home(home: Path) -> Path:
    return Path(home) / S.CREW_HOME_REL


def _ts(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _write_private(path: Path, text: str) -> None:
    """Atomic write, file 0600, parent directories 0700 (``~/.remembra/crew`` layout, §8.1)."""
    for parent in reversed(path.parents):
        if not parent.exists():
            parent.mkdir(mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# adapters.json
# ---------------------------------------------------------------------------


def adapters_path(home: Path) -> Path:
    return crew_home(home) / "adapters.json"


def read_adapters(home: Path) -> dict[str, Any]:
    """The adapter status file; an empty one when missing or unreadable."""
    try:
        data = json.loads(adapters_path(home).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"version": ADAPTERS_VERSION, "adapters": {}}
    if not isinstance(data, dict) or not isinstance(data.get("adapters"), dict):
        return {"version": ADAPTERS_VERSION, "adapters": {}}
    return data


def render_adapters(before: str | None, updates: Mapping[str, Mapping[str, Any] | None]) -> str:
    """Merge ``updates`` (``None`` removes an adapter) into the status file text.

    Unchanged entries keep their fields, so re-running ``connect`` is a no-op.
    """
    try:
        data = json.loads(before) if before else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict):
        data = {}
    raw = data.get("adapters")
    adapters: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
    for name, fields in updates.items():
        if fields is None:
            adapters.pop(name, None)
            continue
        current = adapters.get(name)
        adapters[name] = {**(current if isinstance(current, dict) else {}), **fields}
    out = {"version": ADAPTERS_VERSION, "adapters": adapters}
    text = json.dumps(out, indent=2, sort_keys=True) + "\n"
    if before is not None:
        try:
            if json.loads(before) == out:
                return before
        except ValueError:
            pass
    return text


def adapter_enforcement(home: Path, adapter: str) -> str:
    """``enforced`` or ``advisory`` for ``join`` (§8.3): verified by schema (Claude Code) or by a local round trip."""
    entry = read_adapters(home)["adapters"].get(adapter)
    if isinstance(entry, dict) and entry.get("enforcement") in ("enforce", "observe"):
        return "enforced" if entry["enforcement"] == "enforce" else "advisory"
    spec = crew_specs().get(adapter)
    return "enforced" if spec is not None and spec.verified else "advisory"


# ---------------------------------------------------------------------------
# Round trip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Armed:
    nonce: str
    agent: str
    scratch_root: str
    scratch_file: str  # repo-relative
    sha256: str
    holder_session: str
    probe_session: str
    zone: str
    created_at: float
    expires_at: float
    hmac_key: str

    @property
    def scratch_path(self) -> Path:
        return Path(self.scratch_root) / self.scratch_file


def captures_dir(home: Path, agent: str) -> Path:
    return crew_home(home) / "captures" / agent


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def synthetic_snapshot(armed: Armed, *, case_insensitive: bool = False) -> dict[str, Any]:
    """A ``LocalSnapshot`` in which ``holder_session`` holds zone ``scratch`` exclusively (enforce mode)."""
    created = datetime.fromtimestamp(armed.created_at, UTC)
    expires = datetime.fromtimestamp(armed.expires_at, UTC)
    now = _ts(created)
    tag = armed.nonce[:12]

    def session(sid: str, callsign: str, agent: str, worktree: str) -> dict[str, Any]:
        return {
            "id": sid,
            "callsign": callsign,
            "agent_id": agent,
            "member_key": f"{agent}:verify:{hashlib.sha256(sid.encode()).hexdigest()[:8]}",
            "agent_verified": True,
            "adapter": agent,
            "adapter_enforcement": "enforced",
            "client_kind": "hook",
            "model": None,
            "host_id": f"hst_verify{tag}",
            "state": "active",
            "quiet_reason": None,
            "state_reason": None,
            "stuck": False,
            "branch": "main",
            "head_commit": None,
            "worktree_id": worktree,
            "githook_state": "ok",
            "current_task_id": None,
            "limit": None,
            "joined_at": now,
            "last_activity_at": now,
            "ended_at": None,
            "end_reason": None,
        }

    snap: dict[str, Any] = {
        "crew": {
            "id": f"crw_verify{tag}",
            "project_id": "remembra-verify",
            "name": "Remembra verify",
            "mode": "multi",
            "enforcement": "enforce",
            "settings_version": 1,
            "last_seq": 1,
        },
        "server_time": now,
        "as_of_seq": 1,
        "etag": '"1"',
        "sessions": [
            session(armed.holder_session, "verify-1", "remembra-verify", f"wt-holder-{tag}"),
            session(armed.probe_session, "probe-1", armed.agent, f"wt-probe-{tag}"),
        ],
        "claims": [
            {
                "id": f"clm_verify{tag}",
                "zone_id": f"zn_verify{tag}",
                "path_glob": None,
                "resource": None,
                "mode": "exclusive",
                "holder_kind": "session",
                "holder_session_id": armed.holder_session,
                "holder_agent_id": "remembra-verify",
                "holder_user_id": "u_verify",
                "task_id": None,
                "state": "active",
                "source": "dashboard",
                "epoch": 1,
                "unconfirmed": False,
                "fenced": False,
                "lease_expires_at": _ts(expires),
                "reserve_reason": None,
                "reserved_for": None,
                "offered_to": None,
                "queue_pos": None,
                "baton_ref": None,
                "granted_at": now,
                "version": 1,
            }
        ],
        "zones": [
            {
                "id": f"zn_verify{tag}",
                "slug": armed.zone,
                "title": "Verify scratch zone",
                "parent_id": None,
                "is_leaf": True,
                "builtin": False,
                "include_globs": ["held/**"],
                "exclude_globs": [],
                "services": [],
                "command_patterns": [],
                "mcp_tools": [],
                "mode": "exclusive",
                "auto_claim": False,
                "protected": False,
                "reserve_for": None,
                "fail_closed": True,
                "frozen_by": None,
                "frozen_note": None,
                "frozen_until": None,
                "source": "repo",
                "version": 1,
            }
        ],
        "commons": [],
        "ignore": [],
        "tasks": [],
        "collisions": [],
        "decisions": [],
        "offers": [],
        "footprints": [],
        "inbox_counts": {"project": 0, "crew": 0},
        "pending_zone_changes": [],
        "synced_at": now,
        "skew_s": 0.0,
        "host_id": f"hst_verify{tag}",
        "checkouts": [
            {
                "toplevel": armed.scratch_root,
                "worktree_id": f"wt-probe-{tag}",
                "git_common_dir": str(Path(armed.scratch_root) / ".git"),
                "case_insensitive": case_insensitive,
                "session_id": armed.probe_session,
                "default_branch": "main",
            }
        ],
        "settings": {
            "enforcement": "enforce",
            "undeclared_policy": "footprint",
            "auto_claim": False,
            "auto_claim_leaf_only": True,
            "max_exclusive_claims_per_session": 3,
            "interactive_override": False,
            "fail_closed_zones": [armed.zone],
            "lease_ttl_s": 600,
            "readonly_fence_for_advisory": True,
        },
        "bootstrap_zones": False,
    }
    snap["hmac"] = S.snapshot_hmac(bytes.fromhex(armed.hmac_key), snap)
    return snap


def _case_insensitive(directory: Path) -> bool:
    probe = directory / ".Remembra-Case-Probe"
    try:
        probe.write_text("x")
        return (directory / ".remembra-case-probe").exists()
    except OSError:
        return False
    finally:
        try:
            probe.unlink()
        except OSError:
            pass


def prepare(
    home: Path,
    agent: str,
    *,
    ttl_s: int = DEFAULT_TIMEOUT_S,
    now: float | None = None,
    scratch_parent: Path | None = None,
) -> Armed:
    """Create the scratch repo and arm capture for ``agent`` (replacing any earlier arming).

    The scratch repo lives outside ``~/.remembra`` (that tree is the protected
    ``crew-policy`` zone, which would deny the write for the wrong reason).
    """
    if agent not in crew_specs():
        raise ValueError(f"unknown agent {agent!r}; known: {', '.join(crew_specs())}")
    previous = load_armed(home, agent)
    if previous is not None and Path(previous.scratch_root).name.startswith("remembra-crew-verify-"):
        shutil.rmtree(previous.scratch_root, ignore_errors=True)
    nonce = secrets.token_hex(16)
    parent = Path(scratch_parent) if scratch_parent is not None else Path(tempfile.gettempdir())
    root = (parent / f"remembra-crew-verify-{agent}-{nonce[:8]}").resolve()
    root.mkdir(parents=True, mode=0o700)
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True, timeout=20)
    (root / ".remembra").mkdir()
    (root / ".remembra" / "zones.yml").write_text(
        f"# remembra-crew verify scratch repo for {agent}. Zone '{SCRATCH_ZONE}' is held by a synthetic session.\n"
        f'zones:\n  {SCRATCH_ZONE}:\n    paths: ["held/**"]\n    mode: exclusive\n    fail_closed: true\n'
    )
    scratch = root / SCRATCH_FILE
    scratch.parent.mkdir()
    scratch.write_text(f"remembra-crew verify {nonce}\n")
    (root / "README.md").write_text(f"Scratch repository for `remembra-crew verify --agent {agent}`. Safe to delete.\n")
    created = time.time() if now is None else now
    armed = Armed(
        nonce=nonce,
        agent=agent,
        scratch_root=str(root.resolve()),
        scratch_file=SCRATCH_FILE,
        sha256=_sha(scratch),
        holder_session=f"cs_verifyholder{nonce[:10]}",
        probe_session=f"cs_verifyprobe{nonce[:10]}",
        zone=SCRATCH_ZONE,
        created_at=created,
        expires_at=created + ttl_s,
        hmac_key=secrets.token_hex(32),
    )
    capdir = captures_dir(home, agent)
    if capdir.exists():
        for old in capdir.glob("*.json"):
            old.unlink()
    snapshot = synthetic_snapshot(armed, case_insensitive=_case_insensitive(root))
    _write_private(capdir / "snapshot.json", json.dumps(snapshot, indent=2) + "\n")
    _write_private(capdir / "armed.json", json.dumps(asdict(armed), indent=2) + "\n")
    return armed


def load_armed(home: Path, agent: str) -> Armed | None:
    try:
        data = json.loads((captures_dir(home, agent) / "armed.json").read_text())
        return Armed(**data)
    except (OSError, ValueError, TypeError):
        return None


def disarm(home: Path, agent: str) -> None:
    for name in ("armed.json", "snapshot.json"):
        try:
            (captures_dir(home, agent) / name).unlink()
        except FileNotFoundError:
            pass


def load_captures(home: Path, agent: str, nonce: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    capdir = captures_dir(home, agent)
    if not capdir.is_dir():
        return out
    for path in sorted(capdir.glob("[0-9]*-*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(data, dict) and data.get("nonce") == nonce and isinstance(data.get("payload"), dict):
            out.append(data)
    return out


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _under(path: str | None, root: str) -> bool:
    if not path:
        return False
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (ValueError, OSError):
        return False


def evaluate(home: Path, armed: Armed) -> list[Check]:
    """The round-trip checks for ``armed`` (all must pass)."""
    spec: CrewSpec = crew_specs()[armed.agent]
    payload_map = REGISTRY[armed.agent].spec.payload
    captures = load_captures(home, armed.agent, armed.nonce)
    checks = [Check("captures", bool(captures), f"{len(captures)} hook payload(s) captured for this round trip")]

    session_ok = False
    for cap in captures:
        fields = payload_map.extract(cap["payload"], environ={})
        if fields.get("session_id") and _under(fields.get("cwd"), armed.scratch_root):
            session_ok = True
            break
    checks.append(
        Check("payload_map", session_ok, "session id and working directory found where the adapter's PayloadMap expects them")
    )

    write_seen = shell_seen = False
    scratch = str(armed.scratch_path.resolve())
    for cap in captures:
        if cap.get("event") not in spec.pretool_events:
            continue
        fields = spec.tools.extract(cap["payload"])
        cwd = payload_map.extract(cap["payload"], environ={}).get("cwd") or armed.scratch_root
        target = fields.get("file_path")
        if target:
            resolved = str((Path(cwd) / target).resolve()) if not os.path.isabs(target) else str(Path(target).resolve())
            if resolved == scratch:
                write_seen = True
        command = fields.get("command") or ""
        if armed.scratch_file in command or "scratch.txt" in command:
            write_seen = True
        words = command.replace(";", " ").replace("&&", " ").split()
        if words and "ls" in words:
            shell_seen = True
    checks.append(
        Check(
            "pre_write",
            write_seen,
            f"a pre-tool decision point ({', '.join(spec.pretool_events)}) saw the write to {armed.scratch_file}"
            + ("" if spec.file_gate else " (this agent has no pre-write hook on its file tools; only shell writes can pass)"),
        )
    )
    checks.append(Check("pre_shell", shell_seen, "a pre-tool decision point saw the `ls` shell command"))
    try:
        unchanged = _sha(armed.scratch_path) == armed.sha256
    except OSError:
        unchanged = False
    checks.append(Check("file_unchanged", unchanged, f"{armed.scratch_file} is byte-for-byte unchanged on disk"))
    return checks


def record_verified(home: Path, armed: Armed, checks: list[Check], *, now: float | None = None) -> None:
    """All checks passed: switch the adapter to enforce and disarm."""
    if not all(c.ok for c in checks):
        raise ValueError("round trip did not pass")
    path = adapters_path(home)
    before = path.read_text(encoding="utf-8") if path.exists() else None
    stamp = _ts(datetime.fromtimestamp(time.time() if now is None else now, UTC))
    text = render_adapters(
        before,
        {armed.agent: {"verified_local": True, "enforcement": "enforce", "verified_at": stamp, "verified_nonce": armed.nonce}},
    )
    _write_private(path, text)
    disarm(home, armed.agent)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_checks(checks: list[Check], out: Any) -> None:
    for c in checks:
        print(f"  [{'x' if c.ok else ' '}] {c.name}: {c.detail}", file=out)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="remembra-crew verify", description="Round-trip verification of an agent's crew hooks.")
    p.add_argument("--agent", required=True, help=f"Agent to verify ({', '.join(crew_specs())})")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_S, help="Seconds to wait for the prompt to be run")
    p.add_argument("--check", action="store_true", help="Evaluate the current round trip once (do not re-arm)")
    p.add_argument("--disarm", action="store_true", help="Stop capturing for this agent")
    p.add_argument("--poll", type=float, default=2.0, help=argparse.SUPPRESS)
    return p


def main(argv: list[str] | None = None, *, home: Path | None = None, out: Any = None) -> int:
    args = build_parser().parse_args(argv)
    out = out or sys.stdout
    home = Path(home or os.environ.get("HOME") or Path.home())
    agent = args.agent.strip().lower()
    if agent not in crew_specs():
        print(f"remembra-crew verify: unknown agent {agent!r}; known: {', '.join(crew_specs())}", file=sys.stderr)
        return 2
    if args.disarm:
        disarm(home, agent)
        print(f"Capture for {agent} disarmed.", file=out)
        return 0
    entry = read_adapters(home)["adapters"].get(agent)
    if not (isinstance(entry, dict) and entry.get("installed")):
        print(
            f"Note: crew hooks for {agent} are not recorded as installed; run "
            f"`remembra-crew connect --agent {agent}{'' if crew_specs()[agent].verified else ' --include-unverified'}` first.",
            file=out,
        )
    if args.check:
        armed = load_armed(home, agent)
        if armed is None:
            print(f"No round trip is armed for {agent}. Run `remembra-crew verify --agent {agent}`.", file=out)
            return 1
    else:
        armed = prepare(home, agent, ttl_s=args.timeout)
        print(f"Round trip armed for {agent} (expires in {args.timeout}s).", file=out)
        print(f"1. Start {agent} in {armed.scratch_root}", file=out)
        print(f"2. Give it exactly this prompt: {VERIFY_PROMPT}", file=out)
        print("3. Wait here; the result appears when the payloads arrive.", file=out)
    deadline = time.monotonic() + (0 if args.check else args.timeout)
    while True:
        checks = evaluate(home, armed)
        done = all(c.ok for c in checks)
        if done or time.monotonic() >= deadline:
            break
        time.sleep(args.poll)
    _print_checks(checks, out)
    if done:
        record_verified(home, armed, checks)
        print(f"Verified: {agent} now runs in enforce mode (adapters.json).", file=out)
        return 0
    print(f"Not verified: {agent} stays in observe mode.", file=out)
    return 1


if __name__ == "__main__":
    sys.exit(main())
