"""The payloads behind the MCP tools ``remembra_doctor``, ``remembra_setup`` and ``remembra_help``.

Plain functions (no MCP import) so the CLI, the MCP server and tests share
them. None of them writes anything, asks for a key or returns one: config
is only ever shown through ``RelayConfig.redacted()`` and paths with ``~``.
"""

from __future__ import annotations

import os
import shutil
import textwrap
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import httpx

from remembra.marshal import doctor, knowledge, setup_plan
from remembra.marshal.render import WIDTH, Style
from remembra.marshal.signals import collect, hooks_environ
from remembra.relay.adapters import REGISTRY
from remembra.relay.config import load_config

LOCAL_ONLY: dict[str, str] = {
    "status": "local_only",
    "message": (
        "Marshal reads this machine's files; it runs in the local MCP server only. Run remembra-relay doctor in a terminal."
    ),
}
HELP_FOOTER = "Quoted from docs.remembra.dev pages bundled in remembra {version}; the page governs. No model wrote this."


def _environment(environ: Mapping[str, str] | None) -> tuple[dict[str, str], str | None]:
    """The environment the hooks see: without what the remembra MCP entry injects into this server.

    When that leaves no key anywhere but this server has one, the server's own
    key is used and a note says so (the hooks' own environment isn't visible here).
    """
    full = dict(os.environ if environ is None else environ)
    hooks = hooks_environ(full)
    if full.get("REMEMBRA_API_KEY") and not load_config(environ=hooks, home=Path(hooks.get("HOME") or Path.home())).api_key:
        return full, "the key checked is the one this MCP server uses; the hooks' own environment isn't visible from here"
    return hooks, None


def _bad_agents(agents: list[str]) -> dict[str, Any] | None:
    unknown = [a for a in agents if a not in REGISTRY]
    if unknown:
        return {"status": "error", "error": f"unknown agent(s): {', '.join(unknown)}; known: {', '.join(REGISTRY)}"}
    return None


def doctor_payload(
    agent: str | None = None,
    check_server: bool = True,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    transport: httpx.BaseTransport | None = None,
    which: Callable[[str], str | None] = shutil.which,
    now: float | None = None,
) -> dict[str, Any]:
    agents = [agent.strip().lower()] if agent and agent.strip() else []
    bad = _bad_agents(agents)
    if bad:
        return bad
    env, note = _environment(environ)
    report = doctor.run(home, agents or None, check_server, environ=env, transport=transport, which=which, now=now)
    data = report.json()
    payload: dict[str, Any] = {
        "status": "ok",
        "rendered": report.text(Style()),
        "findings": data["findings"],
        "agents": data["agents"],
        "reads": data["reads"],
        "unchecked": data["unchecked"] + ([note] if note else []),
        "things_to_do": data["things_to_do"],
        "exit_code": data["exit_code"],
        "version": data["version"],
        "changed_nothing": True,
    }
    return payload


def setup_payload(
    agents: list[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    home: Path | None = None,
    which: Callable[[str], str | None] = shutil.which,
    os_id: str | None = None,
    shell: str | None = None,
) -> dict[str, Any]:
    chosen = [a.strip().lower() for a in (agents or []) if a and a.strip()]
    bad = _bad_agents(chosen)
    if bad:
        return bad
    full = dict(os.environ if environ is None else environ)
    env, _ = _environment(full)
    home = Path(home or env.get("HOME") or Path.home())
    sig = collect(home, None, check_server=False, environ=env, which=which)
    if os_id is None:
        os_id, detected_shell = setup_plan.detect_os(env)
        shell = shell or detected_shell
    # The server this MCP server talks to is the one to save the key for, when no key names another.
    url = full.get("REMEMBRA_URL") if sig.config.get("source") == "none" else None
    plan = setup_plan.plan(sig, chosen or None, os_id=os_id, shell=shell, which=which, server_url=url, environ=env)
    return {"status": "ok", "rendered": setup_plan.render_plan(sig, plan), **plan.as_dict(), "changed_nothing": True}


def _render_help(question: str, result: dict[str, Any], version: str) -> str:
    shown = question if len(question) <= 40 else question[:39] + "…"
    head = f'remembra help · "{shown}"'
    lines = [head + " " * max(1, WIDTH - len(head) - len("marshal")) + "marshal"]
    lines.append(f"  › pack    relay guide, plans and credits (bundled in remembra {version})")
    status = result["answer_status"]
    if status == "read_the_page":
        lines.append("  = Not answered from the bundled docs: the page below governs it.")
        lines += [f"    read  {url}" for url in result["pages"]]
        lines.append("    Quote that page, or say you can't confirm.")
    elif status == "cant_confirm":
        lines.append("  = Can't confirm that from Remembra's docs.")
        lines.append(f"    ask   {result['pages'][0]}")
    else:
        for fact in result.get("facts") or []:
            lines.append(f"  = {fact}")
        for section in result["sections"]:
            lines.append(f"  = From {section['url'].removeprefix('https://')}:")
            lines += ["    " + line if line.strip() else "" for line in section["text"].splitlines()]
    footer = HELP_FOOTER.format(version=version) + " Nothing was changed."
    lines += ["  " + line for line in textwrap.wrap(footer, width=WIDTH - 2)]
    return "\n".join(lines) + "\n"


def help_payload(question: str) -> dict[str, Any]:
    from remembra import __version__

    question = " ".join(str(question or "").split())[:500]
    result = knowledge.lookup(question)
    payload: dict[str, Any] = {"status": "ok", "question": question, **result}
    if result["answer_status"] == "answered" and knowledge.wants_plan_facts(question):
        payload["plan_facts"] = knowledge.plan_facts()
        payload["pricing_page"] = knowledge.READ_PAGES["pricing"]
    payload["rendered"] = _render_help(question, result, __version__)
    payload["footer"] = HELP_FOOTER.format(version=__version__)
    return payload
