"""The "You still need to" list that ends ``remembra-relay connect``, printed only when something is left.

``connect`` writes hook files; a few steps it cannot take remain: saving a
key (the user types it into ``remembra-install``'s hidden prompt), running
it again with ``--apply`` after a dry run, writing unverified adapters on
purpose, and trusting the hooks inside Codex, which skips untrusted hooks
without a message.

The list is worked out after ``connect`` has run, from the files as they are
now: each agent ``connect`` looked at is planned again with the same relay
command. A plan with no change means the hooks are in place; a change left
after ``--apply`` means this run did not write them. Whether Codex trust is
still needed is read from ``~/.codex/config.toml`` against the hooks as
written (after a dry run: as ``--apply`` would write them); see
:mod:`remembra.marshal.codex_hooks`.
"""

from __future__ import annotations

import argparse
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from remembra.marshal import codex_hooks, words
from remembra.marshal import commands as cmd
from remembra.marshal.signals import config_file
from remembra.relay.adapters import REGISTRY, relay_command
from remembra.relay.config import load_config

IN_PLACE = "in_place"
DRY_RUN = "dry_run"
SKIPPED_UNVERIFIED = "skipped_unverified"
NOT_WRITTEN = "not_written"
UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Outcome:
    agent: str
    state: str  # in_place | dry_run | skipped_unverified | not_written | unreadable
    planned: str | None = None  # the file as --apply would write it (dry run)
    path: str | None = None


def outcomes(
    home: Path,
    relay: str,
    wanted: Sequence[str],
    applied: bool,
    include_unverified: bool,
    which: Callable[[str], str | None] = shutil.which,
) -> list[Outcome]:
    """What connect left for each agent it looked at (named with --agent, else found here)."""
    out: list[Outcome] = []
    for name, adapter in REGISTRY.items():
        if wanted and name not in wanted:
            continue
        if not wanted and not adapter.detect(home, which):
            continue
        path = str(config_file(adapter, home))
        try:
            change = adapter.plan(home, relay)
        except Exception:
            out.append(Outcome(name, UNREADABLE, path=path))
            continue
        if not change.changed:
            state = IN_PLACE
        elif not applied:
            state = DRY_RUN
        elif not adapter.spec.verified and not include_unverified:
            state = SKIPPED_UNVERIFIED
        else:
            state = NOT_WRITTEN
        out.append(Outcome(name, state, change.after if state == DRY_RUN else None, path))
    return out


def codex_trust_step(home: Path, outcome: Outcome) -> str | None:
    """The Codex trust item, or None when every relay hook in hooks.json is trusted already."""
    hooks_path = Path(outcome.path) if outcome.path else None
    planned = outcome.state == DRY_RUN
    trust = codex_hooks.read_trust(home, outcome.planned if planned else None, planned=planned, hooks_path=hooks_path)
    if not trust.relay_hooks or trust.all_trusted:
        return None
    # The same step, in the same words, as the doctor's CODEX_TRUST_MISSING and the dashboard's slip.
    step = words.codex_trust_fix(t.hook.event for t in trust.per_hook if t.status != codex_hooks.TRUSTED)
    if trust.config_state == "unreadable":
        step += " (~/.codex/config.toml could not be read to check.)"
    else:
        step += " " + words.say("CODEX_TRUST_MISSING", "detail")
    return ("After --apply: " + step) if planned else step


# How a self-hosted user names their server when none is set up yet (a placeholder, never a template command).
OWN_SERVER_HINT = "\n  On your own server instead of Remembra Cloud:\n    remembra-install --all --url <your server URL>"


def key_step_here() -> str:
    """The key step for the server this machine's relay uses: the command the to-do list and the doctor give.

    ``remembra-relay connect``'s own no-key warning leads with it too, so connect gives one key command.
    """
    try:
        return cmd.key_step(load_config().url)
    except Exception:  # never let a warning fail: the step every page shows
        return cmd.INSTALL_KEEP_SERVER


def own_server_hint() -> str:
    """The self-hosted variant, labelled, for a machine with no server set up; empty when one is."""
    return "" if "--url" in key_step_here() else OWN_SERVER_HINT


def connect_todo(
    home: Path,
    results: Sequence[Outcome],
    *,
    applied: bool,
    missing_key: bool,
    server_url: str | None,
    wanted: Sequence[str] = (),
) -> list[str]:
    items: list[str] = []
    if missing_key:
        items.append(
            f"Save your key: run `{cmd.key_step(server_url)}` in your own terminal; it asks for the key at a hidden prompt."
        )
    for outcome in results:
        if outcome.state == UNREADABLE:
            where = outcome.path or f"the {outcome.agent} config"
            items.append(f"Repair {where} (it could not be read), then run connect again.")
    if not applied:
        pending = [o for o in results if o.state == DRY_RUN]
        verified = [o.agent for o in pending if REGISTRY[o.agent].spec.verified]
        unverified = [o.agent for o in pending if not REGISTRY[o.agent].spec.verified]
        if verified:
            agents = [a for a in verified if a in wanted] if wanted else []
            items.append(f"Write the hooks (this was a dry run): `{cmd.connect(agents)}`.")
        if unverified:
            command = cmd.connect(unverified, unverified=True)
            items.append(f"Unverified adapters, never run against the real tool, only if you want them: `{command}`.")
    else:
        skipped = [o.agent for o in results if o.state == SKIPPED_UNVERIFIED]
        if skipped:
            items.append(f"Unverified adapters were skipped; to write them anyway: `{cmd.connect(skipped, unverified=True)}`.")
        for outcome in results:
            if outcome.state == NOT_WRITTEN:
                items.append(f"{words.agent_name(outcome.agent)}: not written by this run; see its lines above.")
    for outcome in results:
        if outcome.agent == "codex" and outcome.state in (IN_PLACE, DRY_RUN):
            step = codex_trust_step(home, outcome)
            if step:
                items.append(step)
    return items


def after_connect(args: argparse.Namespace, which: Callable[[str], str | None] = shutil.which) -> list[str]:
    """The to-do items for the ``connect`` run described by ``args`` (none when an agent name was unknown)."""
    wanted = [a.lower() for a in (getattr(args, "agent", None) or [])]
    if any(a not in REGISTRY for a in wanted):
        return []
    home = Path(os.environ.get("HOME") or Path.home())
    relay = getattr(args, "relay_command", None) or relay_command()
    applied = bool(getattr(args, "apply", False))
    results = outcomes(home, relay, wanted, applied, bool(getattr(args, "include_unverified", False)), which)
    config = load_config()
    return connect_todo(home, results, applied=applied, missing_key=not config.api_key, server_url=config.url, wanted=wanted)


def format_todo(items: Sequence[str]) -> str:
    """The printed block; empty when nothing is left."""
    if not items:
        return ""
    lines = ["", "You still need to:"]
    lines += [f"  {n}. {item}" for n, item in enumerate(items, 1)]
    lines.append(f"Then check everything with: {cmd.doctor()}")
    return "\n".join(lines)
