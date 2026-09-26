"""The "You still need to" list that ends ``remembra-relay connect``, printed only when something is left.

``connect`` writes hook files; a few steps it cannot take remain: saving a
key (the user types it into ``remembra-install``'s hidden prompt), running
it again with ``--apply`` after a dry run, writing unverified adapters on
purpose, and trusting the hooks inside Codex, which skips untrusted hooks
without a message. Whether Codex trust is still needed is read from
``~/.codex/config.toml`` against the hooks as written (or, after a dry run,
as ``--apply`` would write them); see :mod:`remembra.marshal.codex_hooks`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from remembra.marshal import codex_hooks
from remembra.marshal import commands as cmd
from remembra.relay.adapters import REGISTRY
from remembra.relay.config import DEFAULT_URL

WRITTEN = "written"
ALREADY = "already"
DRY_RUN = "dry_run"
SKIPPED_UNVERIFIED = "skipped_unverified"
UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Outcome:
    agent: str
    state: str  # written | already | dry_run | skipped_unverified | unreadable
    planned: str | None = None  # the file as --apply would write it (dry run)
    path: str | None = None


def _key_command(server_url: str | None) -> str:
    if not server_url or server_url.rstrip("/") == DEFAULT_URL:
        return cmd.INSTALL_KEEP_SERVER
    try:
        return cmd.save_key_command(server_url)
    except ValueError:
        return cmd.INSTALL_KEEP_SERVER


def codex_trust_step(home: Path, outcome: Outcome) -> str | None:
    """The Codex trust item, or None when every relay hook in hooks.json is trusted already."""
    planned = outcome.state == DRY_RUN
    trust = codex_hooks.read_trust(home, outcome.planned, planned=planned) if planned else codex_hooks.read_trust(home)
    if not trust.relay_hooks or trust.all_trusted:
        return None
    n = len(REGISTRY["codex"].events())
    step = f"Codex: trust the {n} remembra-relay hooks in Codex Settings > Hooks, or run /hooks in the Codex CLI."
    if trust.config_state == "unreadable":
        step += " (~/.codex/config.toml could not be read to check.)"
    else:
        step += " Codex skips untrusted hooks without a message."
    return ("After --apply: " + step) if planned else step


def connect_todo(
    home: Path,
    outcomes: Sequence[Outcome],
    *,
    applied: bool,
    missing_key: bool,
    server_url: str | None,
    wanted: Sequence[str] = (),
) -> list[str]:
    items: list[str] = []
    if missing_key:
        items.append(
            f"Save your key: run `{_key_command(server_url)}` in your own terminal; it asks for the key at a hidden prompt."
        )
    for outcome in outcomes:
        if outcome.state == UNREADABLE:
            where = outcome.path or f"the {outcome.agent} config"
            items.append(f"Repair {where} (it could not be read), then run connect again.")
    if not applied:
        pending = [o for o in outcomes if o.state == DRY_RUN]
        verified = [o.agent for o in pending if REGISTRY[o.agent].spec.verified]
        unverified = [o.agent for o in pending if not REGISTRY[o.agent].spec.verified]
        if verified:
            agents = [a for a in verified if a in wanted] if wanted else []
            items.append(f"Write the hooks (this was a dry run): `{cmd.connect(agents)}`.")
        if unverified:
            command = cmd.connect(unverified, unverified=True)
            items.append(f"Unverified adapters, never run against the real tool, only if you want them: `{command}`.")
    else:
        skipped = [o.agent for o in outcomes if o.state == SKIPPED_UNVERIFIED]
        if skipped:
            items.append(f"Unverified adapters were skipped; to write them anyway: `{cmd.connect(skipped, unverified=True)}`.")
    for outcome in outcomes:
        if outcome.agent == "codex" and outcome.state in (WRITTEN, ALREADY, DRY_RUN):
            step = codex_trust_step(home, outcome)
            if step:
                items.append(step)
    return items


def format_todo(items: Sequence[str]) -> str:
    """The printed block; empty when nothing is left."""
    if not items:
        return ""
    lines = ["", "You still need to:"]
    lines += [f"  {n}. {item}" for n, item in enumerate(items, 1)]
    lines.append(f"Then check everything with: {cmd.doctor()}")
    return "\n".join(lines)
