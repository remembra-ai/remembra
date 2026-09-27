"""``remembra-relay projects split|undo``: one project per repository again.

Before 0.16.1 a configured project (``REMEMBRA_PROJECT``, the MCP env or
``~/.remembra/credentials``) named every git repository the server had not seen
yet, so for a user who had one, every repository shared one trail::

    remembra-relay projects split                 # dry run: what would move, and why
    remembra-relay projects split --apply         # carry it out (logged, reversible)
    remembra-relay projects undo [--batch ID] [--apply]

``split`` asks the server for the plan (see :mod:`remembra.services.relay_split`),
reads with git every checkout of those repositories on this machine (the paths
the server has on record for this host, the current directory and ``--repo``)
to find which of them holds each commit an older handoff recorded, and shows
the result: every repository and the project it gets, every handoff that moves
and the evidence, and what stays with the reason. Nothing changes without
``--apply``; nothing is ever deleted. ``undo`` reverses a split. What agents and
tools recorded (paths, names, headlines) is printed inside one untrusted-data
block, after the server's trust policy. API keys and connections restricted to
the project lose the split repositories; they are listed, and ``--apply`` then
needs ``--keys-lose-access`` (or add the new projects to them first).

These are not hooks: they may take longer than a hook's budget, and exit 1 on
failure (2 when there is nothing to split).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any

from remembra.relay import facts as factlib
from remembra.security.untrusted import TOOL_PREAMBLE, wrap_untrusted

SPLIT_BUDGET_SECONDS = 300.0
SPLIT_HTTP_TIMEOUT_SECONDS = 120.0
GIT_CHECK_TIMEOUT_SECONDS = 30.0
REPO_READ_SECONDS = 10.0
MAX_STAYS_SHOWN = 10  # per reason, in the text output (--format json has every one)


class SplitFailed(Exception):
    """The server refused or could not be reached."""


def add_parser(sub: Any) -> None:
    p = sub.add_parser("projects", help="Give each repository of a shared project its own project (dry run by default)")
    psub = p.add_subparsers(dest="projects_command", required=True)

    split = psub.add_parser("split", help="Show (or, with --apply, carry out) one project per repository")
    split.add_argument("--project", help="The shared project (default: the configured REMEMBRA_PROJECT)")
    split.add_argument(
        "--repo", action="append", default=[], help="Also read this checkout with git to match older handoffs (repeatable)"
    )
    split.add_argument("--apply", action="store_true", help="Carry it out (logged; `projects undo` reverses it)")
    split.add_argument(
        "--keys-lose-access",
        action="store_true",
        help="Confirm that API keys/connections restricted to the project lose the split repositories",
    )
    split.add_argument("--format", choices=["text", "json"], default="text")
    split.add_argument("--cwd", help=argparse.SUPPRESS)
    split.set_defaults(func=cmd_split)

    undo = psub.add_parser("undo", help="Reverse a split (the latest one unless --batch; dry run by default)")
    undo.add_argument("--batch", help="The split batch id to reverse (printed by split --apply)")
    undo.add_argument("--apply", action="store_true", help="Carry it out")
    undo.add_argument("--format", choices=["text", "json"], default="text")
    undo.set_defaults(func=cmd_undo)


def _context(args: argparse.Namespace) -> Any:
    from remembra.relay import cli

    ns = argparse.Namespace(hook=None, agent=None, cwd=getattr(args, "cwd", None), project=None, session_id=None)
    ctx = cli.Context(ns, payload={})
    ctx.deadline = factlib.Deadline(SPLIT_BUDGET_SECONDS)
    ctx.http_timeout = SPLIT_HTTP_TIMEOUT_SECONDS
    return ctx


def _post(ctx: Any, path: str, body: dict[str, Any]) -> dict[str, Any]:
    from remembra.relay import cli

    try:
        response = ctx.request("POST", path, json=body)
    except Exception as e:
        raise SplitFailed(f"{e.__class__.__name__}: {e}") from e
    if response.status_code >= 400:
        raise SplitFailed(cli._http_error(response))
    data = response.json()
    if not isinstance(data, dict):
        raise SplitFailed("the server answered with something other than a JSON object")
    return data


def source_project(ctx: Any, explicit: str | None) -> str | None:
    """``--project``, else the configured project (``REMEMBRA_PROJECT`` / MCP env / credentials, not ``default``)."""
    if explicit and explicit.strip():
        return str(ctx.normalize_project(explicit) or explicit.strip())
    configured = ctx.normalize_project(ctx.config.project) or ctx.single_namespace()
    return str(configured) if configured else None


def commits_present(repo: Path, shas: list[str]) -> list[str]:
    """Which of ``shas`` are commits in the repository at ``repo`` (``git cat-file --batch-check``).

    Read-only and offline: a partial clone never fetches a missing object.
    Returns [] when git fails or times out.
    """
    if not shas:
        return []
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_NO_LAZY_FETCH": "1", "GIT_OPTIONAL_LOCKS": "0", "LC_ALL": "C"}
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "cat-file", "--batch-check"],
            input="\n".join(shas) + "\n",
            capture_output=True,
            text=True,
            timeout=GIT_CHECK_TIMEOUT_SECONDS,
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    found = []
    for line in proc.stdout.splitlines():
        parts = line.split()
        if len(parts) == 3 and parts[1] == "commit":
            found.append(parts[0].lower())
    wanted = set(shas)
    return [sha for sha in found if sha in wanted]


def find_checkouts(ctx: Any, plan: dict[str, Any], extra: list[str]) -> list[dict[str, Any]]:
    """Checkouts on this machine the plan's repositories name, plus the current directory and ``extra``.

    Each is read with git (remote, root commit, top level) and checked for the
    commits older handoffs recorded (``commit_candidates``).
    """
    host = (ctx.host or "").strip().lower()
    candidates: list[Path] = []
    for place in [*(p for r in plan.get("repositories") or [] for p in r.get("paths") or []), *(plan.get("folders") or [])]:
        if str(place.get("host") or "").lower() == host and place.get("path"):
            candidates.append(Path(str(place["path"])))
    if ctx.repo.is_git and ctx.repo.toplevel:
        candidates.append(Path(ctx.repo.toplevel))
    candidates.extend(Path(p).expanduser() for p in extra)
    shas = sorted({str(sha) for c in plan.get("commit_candidates") or [] for sha in c.get("commits") or []})
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for path in candidates:
        try:
            if not path.is_dir():
                continue
        except OSError:
            continue
        info = factlib.repo_info(path, factlib.Deadline(REPO_READ_SECONDS))
        if not info.is_git or not info.toplevel or info.toplevel in seen:
            continue
        seen.add(info.toplevel)
        locator = info.locator(Path(info.toplevel), ctx.host)
        out.append({**locator, "present": commits_present(Path(info.toplevel), shas)})
    return out


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def _when(value: Any) -> str:
    return str(value or "")[:16].replace("T", " ")


def _move_line(move: dict[str, Any]) -> str:
    how = "recorded location" if move.get("matched_by") == "location" else "recorded commit"
    versions = len(move.get("versions") or [])
    extra = f" (+{versions} earlier version{'s' if versions != 1 else ''})" if versions else ""
    return (
        f"    {_when(move.get('created_at'))}  {str(move.get('agent_id') or '?'):<12} {move.get('memory_type')}{extra}"
        f"  [{how}] {move.get('headline') or ''}\n      evidence: {move.get('evidence')}"
    )


def render_split(result: dict[str, Any]) -> str:
    """The split as text: the server's own lines outside, everything recorded (repository names and paths,
    handoff headlines, key names) inside one untrusted-data block."""
    project = result.get("project")
    applied = bool(result.get("applied"))
    mode = "carried out" if applied else "dry run: nothing changes until you add --apply"
    head = f"Split project '{project}' ({mode})"
    lines: list[str] = []
    repos = result.get("repositories") or []
    moving = [r for r in repos if not r.get("keeps_project") and (r.get("bindings") or not r.get("already_split"))]
    if not repos:
        lines.append(f"No git repository is bound to '{project}': there is nothing to split.")
    else:
        lines.append(f"Repositories bound to '{project}' and the project each gets:")
        for repo in repos:
            name = repo.get("name") or repo.get("key")
            note = ""
            if repo.get("keeps_project"):
                note = " (named like the project: it keeps it)"
            elif repo.get("already_split") and not repo.get("bindings"):
                note = " (split out earlier)"
            elif repo.get("already_split"):
                note = " (split out earlier; more of its locations move now)"
            where = ", ".join(str(c.get("root_path")) for c in repo.get("checkouts") or [])
            lines.append(f"  {str(name):<24} -> {repo.get('target_project')}{note}  [{repo.get('key')}]")
            bindings = repo.get("bindings") or []
            if bindings and not repo.get("keeps_project"):
                lines.append(f"      location bindings that move: {', '.join(str(b) for b in bindings)}")
            if where:
                lines.append(f"      checked out here: {where}")
    folders = result.get("folders") or []
    if folders:
        lines.append(f"Folders (not git repositories) that stay in '{project}': {len(folders)}")

    moves = result.get("moves") or []
    verb = "Moved" if applied else "Would move"
    lines.append(f"\n{verb} {len(moves)} handoff{'s' if len(moves) != 1 else ''}:")
    by_target: dict[str, list[dict[str, Any]]] = {}
    for move in moves:
        by_target.setdefault(str(move.get("to_project")), []).append(move)
    for target, items in sorted(by_target.items()):
        lines.append(f"  -> {target} ({len(items)})")
        lines.extend(_move_line(m) for m in items)

    stays = result.get("stays") or []
    total = int(result.get("stays_total") or len(stays))
    lines.append(f"\nStay in '{project}': {total}")
    by_reason: dict[str, list[dict[str, Any]]] = {}
    for item in stays:
        by_reason.setdefault(str(item.get("reason")), []).append(item)
    for reason, items in sorted(by_reason.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"  {len(items)} · {reason}")
        for item in items[:MAX_STAYS_SHOWN]:
            lines.append(
                f"    {_when(item.get('created_at'))}  {str(item.get('agent_id') or '?'):<12} {item.get('memory_type')}"
                f"  {item.get('headline') or ''}"
            )
        if len(items) > MAX_STAYS_SHOWN:
            lines.append(f"    … and {len(items) - MAX_STAYS_SHOWN} more (--format json lists every one)")

    checkouts = result.get("checkouts") or []
    if checkouts:
        read = ", ".join(f"{c.get('root_path')} ({c.get('matched') or 'not bound here'})" for c in checkouts)
        lines.append(f"\nCheckouts read with git on this machine: {read}")

    credentials = result.get("restricted_credentials") or []
    if credentials:
        lines.append(f"\nRestricted to '{project}', refused in the split repositories {'now' if applied else 'afterwards'}:")
        for cred in credentials:
            kind = "API key" if cred.get("kind") == "api_key" else "connection"
            lines.append(
                f"  {kind} {cred.get('name') or '(no name)'} (id {cred.get('id')}): loses {', '.join(cred.get('loses') or [])}"
            )

    tail: list[str] = []
    if not checkouts:
        tail.append(
            "No checkout of these repositories was found on this machine, so older handoffs (no recorded location)"
            " could not be matched by commit. Pass --repo PATH, or run this where they are checked out."
        )
    if credentials and applied:
        tail.append(
            f"{len(credentials)} API key(s) or connection(s) restricted to '{project}' are now refused in the split"
            " repositories (listed above). Add the new projects to them in the dashboard to give that access back."
        )
    elif credentials:
        tail.append(
            f"{len(credentials)} API key(s) or connection(s) restricted to '{project}' would be refused in the split"
            " repositories (listed above). Add the new projects to them in the dashboard, or apply with"
            " --keys-lose-access to confirm they lose them."
        )
    if applied:
        moved = result.get("moved") or {}
        tail.append(
            f"Done: {moved.get('bindings', 0)} location binding(s) and {moved.get('memories', 0)} handoff record(s)"
            f" moved (batch {result.get('batch_id')}). Nothing was deleted."
        )
        if moved.get("vector_payload_errors"):
            tail.append(f"  {moved['vector_payload_errors']} search-index update(s) failed; the server log has details.")
        tail.append(f"To reverse it: remembra-relay projects undo --batch {result.get('batch_id')} --apply")
    elif moving or moves:
        tail.append("Nothing is deleted. Run again with --apply to carry this out; `remembra-relay projects undo` reverses it.")
    else:
        tail.append("Nothing to move.")
    return "\n".join([head, wrap_untrusted("\n".join(lines), TOOL_PREAMBLE), *tail])


def render_undo(result: dict[str, Any]) -> str:
    batch = result.get("batch_id")
    if result.get("already_undone"):
        return f"Split {batch} was already undone: nothing to do."
    back = result.get("moves_back") or []
    left = result.get("left") or []
    applied = bool(result.get("applied"))
    head = f"Undo split {batch} ({'carried out' if applied else 'dry run: nothing changes until you add --apply'})"
    lines = [head, f"{'Moved back' if applied else 'Would move back'}: {len(back)}"]
    for item in back:
        since = "  (written since the split)" if item.get("since_split") else ""
        lines.append(f"  {item['kind']:<8} {item['ref']}  {item['from_project']} -> {item['to_project']}{since}")
    if left:
        lines.append(f"Left alone (changed since the split): {len(left)}")
        for item in left:
            lines.append(f"  {item['kind']:<8} {item['ref']}  {item.get('reason')}")
    for project, count in sorted((result.get("written_since") or {}).items()):
        lines.append(
            f"{count} handoff(s) or checkpoint(s) written in '{project}' since the split stay there"
            " (no location of the repository recorded with them)."
        )
    if not applied and back:
        lines.append(f"Run again with --apply to move them back: remembra-relay projects undo --batch {batch} --apply")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_split(args: argparse.Namespace) -> int:
    from remembra.relay import cli

    try:
        ctx = _context(args)
        if not ctx.config.api_key:
            cli._err("projects split: no API key (set REMEMBRA_API_KEY or run remembra-install --all)")
            return 1
        project = source_project(ctx, args.project)
        if not project:
            cli._err(
                "projects split: no project to split. REMEMBRA_PROJECT is not set (or is 'default'), so every"
                " repository already has its own project. Pass --project <id> to split another one."
            )
            return 2
        if ctx.single_namespace() and not args.project:
            cli._err(
                "note: REMEMBRA_RELAY_PROJECT is set, which keeps every NEW location in one project; repositories"
                " split now keep their own project."
            )
        first = _post(ctx, "/api/v1/projects/split", {"project": project, "apply": False})
        checkouts = find_checkouts(ctx, first, list(args.repo or []))
        body = {"project": project, "apply": bool(args.apply), "checkouts": checkouts}
        if args.keys_lose_access:
            body["restricted_keys_lose_access"] = True
        result = _post(ctx, "/api/v1/projects/split", body)
    except SplitFailed as e:
        cli._err(f"projects split failed: {e}")
        return 1
    if args.format == "json":
        print(json.dumps(result, indent=2, default=str))
    else:
        print(render_split(result))
    return 0


def cmd_undo(args: argparse.Namespace) -> int:
    from remembra.relay import cli

    try:
        ctx = _context(args)
        if not ctx.config.api_key:
            cli._err("projects undo: no API key (set REMEMBRA_API_KEY or run remembra-install --all)")
            return 1
        result = _post(ctx, "/api/v1/projects/split/undo", {"batch_id": args.batch, "apply": bool(args.apply)})
    except SplitFailed as e:
        cli._err(f"projects undo failed: {e}")
        return 1
    print(json.dumps(result, indent=2, default=str) if args.format == "json" else render_undo(result))
    return 0
