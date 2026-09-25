#!/usr/bin/env python3
"""Claude Code SessionStart hook: inject the Remembra session brief as context.

Claude Code runs this at session start and adds whatever it prints on stdout
to the model's context. It calls ``GET /api/v1/session/brief`` and prints the
latest handoff, this agent's unread inbox, current status values and the most
recent memories (by time), so the session starts with context instead of
relying on the model to remember to call a recall tool.

Standard library only — runs with any python3, no venv needed.

Configuration (first match wins):
  1. Environment: REMEMBRA_URL, REMEMBRA_API_KEY, REMEMBRA_PROJECT,
     REMEMBRA_AGENT_ID, REMEMBRA_PROJECT_ALIASES.
  2. The ``env`` block of the ``remembra`` MCP server in ~/.claude.json
     (override the path with REMEMBRA_HOOK_CLAUDE_CONFIG), so the key lives in
     one place.
  Agent id defaults to "claude-code".

Never blocks a session: on any failure it prints one line saying the brief is
unavailable and exits 0. The API key is never printed.

Install (settings.json):
  {"hooks": {"SessionStart": [{"hooks": [{"type": "command",
     "command": "python3 /path/to/session_start.py", "timeout": 15}]}]}}
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_AGENT_ID = "claude-code"
TIMEOUT_SECONDS = float(os.environ.get("REMEMBRA_HOOK_TIMEOUT", "8"))
MAX_OUTPUT_CHARS = 8000
RECENT_N = int(os.environ.get("REMEMBRA_HOOK_RECENT_N", "10"))


def _mcp_env_from_claude_config(path: Path) -> dict[str, str]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    candidates: list[dict[str, Any]] = []
    servers = data.get("mcpServers") or {}
    if isinstance(servers.get("remembra"), dict):
        candidates.append(servers["remembra"])
    for project in (data.get("projects") or {}).values():
        server = ((project or {}).get("mcpServers") or {}).get("remembra")
        if isinstance(server, dict):
            candidates.append(server)
    for server in candidates:
        env = server.get("env")
        if isinstance(env, dict) and env.get("REMEMBRA_API_KEY"):
            return {k: str(v) for k, v in env.items()}
    return {}


def load_config(environ: dict[str, str] | None = None) -> dict[str, str]:
    """Resolve url/key/project/agent from env, falling back to ~/.claude.json."""
    env = dict(os.environ if environ is None else environ)
    config_path = Path(env.get("REMEMBRA_HOOK_CLAUDE_CONFIG") or Path.home() / ".claude.json")
    fallback = _mcp_env_from_claude_config(config_path) if not env.get("REMEMBRA_API_KEY") else {}

    def pick(name: str, default: str = "") -> str:
        return (env.get(name) or fallback.get(name) or default).strip()

    return {
        "url": pick("REMEMBRA_URL", "http://localhost:8787").rstrip("/"),
        "api_key": pick("REMEMBRA_API_KEY"),
        "project": normalize_project(pick("REMEMBRA_PROJECT", "default"), pick("REMEMBRA_PROJECT_ALIASES")),
        "agent_id": pick("REMEMBRA_AGENT_ID", DEFAULT_AGENT_ID),
    }


def normalize_project(project: str, aliases_spec: str) -> str:
    """Same rules as remembra.client.project.normalize_project_id."""
    cleaned = "-".join(project.split()) or "default"
    aliases = {}
    for part in aliases_spec.split(","):
        if "=" in part:
            alias, canonical = (x.strip() for x in part.split("=", 1))
            if alias and canonical:
                aliases[alias.lower()] = canonical
    return aliases.get(cleaned.lower(), cleaned)


def fetch_brief(config: dict[str, str]) -> dict[str, Any]:
    query = urllib.parse.urlencode({"project_id": config["project"], "agent_id": config["agent_id"], "recent_n": RECENT_N})
    request = urllib.request.Request(
        f"{config['url']}/api/v1/session/brief?{query}",
        headers={"X-API-Key": config["api_key"], "Accept": "application/json", "User-Agent": "remembra-claude-hook"},
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
        body: dict[str, Any] = json.loads(response.read().decode("utf-8"))
        return body


def _clip(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _day(ts: str | None) -> str:
    return (ts or "")[:16].replace("T", " ")


def format_brief(brief: dict[str, Any]) -> str:
    """Render the brief as compact markdown for the model's context."""
    lines = [
        f"# Remembra session brief (project: {brief.get('project_id')}, agent: {brief.get('agent_id')})",
    ]

    handoff = brief.get("handoff")
    lines.append("")
    if handoff:
        who = f" by {handoff['agent_id']}" if handoff.get("agent_id") else ""
        lines.append(f"## Latest handoff ({_day(handoff.get('created_at'))}{who})")
        lines.append(_clip(handoff.get("content", ""), 2000))
    else:
        lines.append("## Latest handoff: none stored for this project")

    inbox = brief.get("inbox")
    lines.append("")
    if inbox is None:
        lines.append("## Inbox: not checked (no agent id)")
    elif not inbox.get("available", True):
        lines.append("## Inbox: unavailable on this server")
    else:
        count = inbox.get("unread_count", 0)
        lines.append(f"## Inbox: {count} unread")
        for item in inbox.get("items", []):
            lines.append(
                f"- [{item.get('inbox_id')}] from {item.get('from_agent')} ({_day(item.get('created_at'))}): "
                f"{_clip(item.get('subject', ''), 120)} — {_clip(item.get('body_preview', ''), 200)}"
            )
        if count:
            lines.append("Act on these: read full bodies with get_inbox, then ack_inbox(inbox_id, result='done'|'blocked').")

    status_items = brief.get("status_items") or []
    if status_items:
        lines.append("")
        lines.append("## Current status")
        for item in status_items:
            lines.append(f"- {item.get('key')}: {_clip(str(item.get('value', '')), 300)} ({_day(item.get('updated_at'))})")

    recent = brief.get("recent") or []
    if recent:
        lines.append("")
        lines.append("## Recent memories (newest first)")
        for mem in recent:
            who = f" [{mem['agent_id']}]" if mem.get("agent_id") else ""
            kind = f" ({mem['memory_type']})" if mem.get("memory_type") else ""
            lines.append(f"- {_day(mem.get('created_at'))}{who}{kind}: {_clip(mem.get('content', ''), 300)}")

    warnings = brief.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append("## Warnings")
        lines.extend(f"- {w}" for w in warnings)

    text = "\n".join(lines)
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[: MAX_OUTPUT_CHARS - 40] + "\n... (brief truncated; call session_brief)"
    return text


def main() -> int:
    try:
        sys.stdin.read() if not sys.stdin.isatty() else None  # hook input JSON (unused)
    except Exception:
        pass
    config = load_config()
    if not config["api_key"]:
        print("Remembra session brief unavailable: no REMEMBRA_API_KEY (env or ~/.claude.json remembra MCP env).")
        return 0
    try:
        brief = fetch_brief(config)
    except urllib.error.HTTPError as e:
        print(f"Remembra session brief unavailable: HTTP {e.code} from {config['url']}. Call the session_brief tool.")
        return 0
    except Exception as e:  # network, timeout, bad JSON
        print(f"Remembra session brief unavailable: {type(e).__name__}. Call the session_brief tool.")
        return 0
    print(format_brief(brief))
    return 0


if __name__ == "__main__":
    sys.exit(main())
