"""Fake HOME directories and a fake Remembra trail for Marshal's tests.

:class:`FakeHome` writes the files Marshal reads, the way the real tools
write them: hook files through the relay's own adapters, outbox entries
through ``remembra.relay.outbox``, Codex trust in ``[hooks.state]`` with the
hash Codex computes. :class:`FakeTrail` is an ``httpx.MockTransport`` that
answers the two GETs the doctor may make and records every request, so a
test can prove nothing else was asked.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from remembra.marshal import codex_hooks
from remembra.relay import outbox
from remembra.relay.adapters import REGISTRY

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC).timestamp()
KEY = "rem_7Kq2Vx9LmP4tR8sW1nB6cY3hJ5dF0gZa"  # key-shaped and random-looking: every output is checked for it
URL = "https://api.remembra.test"
CLOUDFLARE_PAGE = (
    '<!DOCTYPE html>\n<html lang="en-US"><head><title>Attention Required! | Cloudflare</title></head>'
    "<body>Sorry, you have been blocked. Cloudflare Ray ID: <strong>8c1f2e3d4a5b6c7d</strong></body></html>"
)


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


class FakeHome:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.home = root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.bin = root / "bin"
        self.bin.mkdir(exist_ok=True)
        self.bins: set[str] = set()
        self.relay = str(self.bin / "remembra-relay")
        Path(self.relay).write_text("#!/bin/sh\n")
        os.chmod(self.relay, 0o755)

    # -- environment -------------------------------------------------------

    def environ(self, **extra: str) -> dict[str, str]:
        return {"HOME": str(self.home), "PATH": str(self.bin), **extra}

    def which(self, name: str) -> str | None:
        return str(self.bin / name) if name in self.bins else None

    def install(self, *names: str) -> None:
        for name in names:
            self.bins.add(name)
            path = self.bin / name
            path.write_text("#!/bin/sh\n")
            os.chmod(path, 0o755)

    # -- keys and MCP entries ----------------------------------------------

    def credentials(self, key: str = KEY, url: str = URL, project: str | None = None) -> Path:
        path = self.home / ".remembra" / "credentials"
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {"api_key": key, "url": url}
        if project:
            data["project"] = project
        path.write_text(json.dumps(data))
        return path

    def claude_mcp(self, project: str = "default", key: str = KEY, url: str = URL) -> Path:
        path = self.home / ".claude.json"
        env = {"REMEMBRA_URL": url, "REMEMBRA_API_KEY": key, "REMEMBRA_PROJECT": project, "REMEMBRA_AGENT_ID": "claude-code"}
        path.write_text(json.dumps({"mcpServers": {"remembra": {"command": "remembra-mcp", "env": env}}}))
        return path

    def codex_mcp(self, project: str = "default", key: str = KEY, url: str = URL, extra: str = "") -> Path:
        path = self.home / ".codex" / "config.toml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            'model = "gpt-5"\n\n[mcp_servers.remembra]\ncommand = "remembra-mcp"\n\n[mcp_servers.remembra.env]\n'
            f'REMEMBRA_URL = "{url}"\nREMEMBRA_API_KEY = "{key}"\nREMEMBRA_PROJECT = "{project}"\n' + extra
        )
        return path

    # -- hooks ----------------------------------------------------------------

    def hooks(self, agent: str, missing: Iterable[str] = (), relay: str | None = None) -> Path:
        """The hook file ``connect --apply`` writes for ``agent``, minus the ``missing`` events."""
        adapter = REGISTRY[agent]
        path = adapter.spec.config_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        text, _ = adapter.render(path.read_text() if path.exists() else None, relay or self.relay)
        drop = set(missing)
        if drop:
            data = json.loads(text)
            data["hooks"] = {e: g for e, g in data["hooks"].items() if e not in drop}
            text = json.dumps(data, indent=2) + "\n"
        path.write_text(text)
        return path

    def trust_codex(
        self,
        trusted: Iterable[str] | None = None,
        stale: Iterable[str] = (),
        disabled: Iterable[str] = (),
        key_path: str | None = None,
    ) -> Path:
        """Append ``[hooks.state]`` records for the relay hooks in ~/.codex/hooks.json, as /hooks writes them."""
        hooks_path = self.home / ".codex" / "hooks.json"
        _, relay = codex_hooks.relay_hooks_in(hooks_path.read_text())
        trusted_set = {h.event for h in relay} if trusted is None else set(trusted)
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        lines = []
        for hook in relay:
            key = f"{key_path or hooks_path}:{hook.key_suffix}"
            if hook.event in disabled:
                lines.append(f'\n[hooks.state."{key}"]\nenabled = false\ntrusted_hash = "{hook.hash}"\n')
            elif hook.event in stale:
                lines.append(f'\n[hooks.state."{key}"]\ntrusted_hash = "sha256:{"0" * 64}"\n')
            elif hook.event in trusted_set:
                lines.append(f'\n[hooks.state."{key}"]\ntrusted_hash = "{hook.hash}"\n')
        with config.open("a") as fh:
            fh.write("".join(lines))
        return config

    # -- relay state ----------------------------------------------------------

    def queue(
        self,
        agent: str = "codex",
        session: str = "sess-1",
        error: str = "ConnectError: [Errno 61] Connection refused",
        status: int | None = None,
        url: str = URL,
        source: str | None = None,
        attempts: int = 2,
        queued_ts: float | None = None,
    ) -> Path:
        path = outbox.enqueue(
            self.home,
            {"agent_id": agent, "session_id": session, "facts": {"branch": "main"}},
            url=url,
            config_source=source if source is not None else f"credentials:{self.home / '.remembra' / 'credentials'}",
            error=error,
            http_status=status,
        )
        assert path is not None
        data = json.loads(path.read_text())
        data["attempts"] = attempts
        data["queued_ts"] = queued_ts or NOW - 2 * 3600
        path.write_text(json.dumps(data))
        return path

    def status(self, agents: dict[str, dict[str, Any]], keys: dict[str, Any] | None = None) -> Path:
        path = outbox.status_path(self.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"agents": agents, "keys": keys or {}}))
        return path

    def close_log(self, lines: list[str], mtime: float) -> Path:
        path = self.home / ".remembra" / "relay" / "last-detached-close.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n")
        os.utime(path, (mtime, mtime))
        return path

    def rollout(self, kind: str, n: int, age_days: float = 1.0) -> None:
        folder = self.home / ".codex" / "sessions" / "2026" / "09" / "25"
        folder.mkdir(parents=True, exist_ok=True)
        stamp = NOW - age_days * 86400
        for i in range(n):
            meta: dict[str, Any] = {"session_id": f"{kind}-{i}", "cwd": "/home/dev/widget", "source": "vscode"}
            if kind in ("automation", "subagent", "user"):
                meta["thread_source"] = kind
            path = folder / f"rollout-2026-09-25T10-00-{kind}-{age_days:g}d-{i:03d}.jsonl"
            path.write_text(json.dumps({"type": "session_meta", "payload": meta}) + "\n" + json.dumps({"type": "event"}) + "\n")
            os.utime(path, (stamp, stamp))


def tree_digest(root: Path) -> dict[str, tuple[int, int, bytes]]:
    """Every file under ``root``: (mode, mtime_ns, content). Equal before and after means nothing was written."""
    out: dict[str, tuple[int, int, bytes]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in sorted(dirnames):
            st = os.stat(os.path.join(dirpath, name))
            out[os.path.join(dirpath, name) + "/"] = (st.st_mode, st.st_mtime_ns, b"")
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            st = os.stat(path)
            with open(path, "rb") as fh:
                out[path] = (st.st_mode, st.st_mtime_ns, fh.read())
    return out


class FakeTrail:
    """A mock Remembra API for the two reads the doctor may make; every request is recorded."""

    def __init__(
        self,
        agents: dict[str, dict[str, Any]] | None = None,
        items: list[dict[str, Any]] | None = None,
        key_status: int = 200,
        key_body: Any = None,
        key_text: str | None = None,
        key_headers: dict[str, str] | None = None,
        html_403: bool = False,
        trail_status: int = 200,
        agent_items: dict[str, list[dict[str, Any]]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.agents = agents or {}
        self.items = items or []
        self.key_status = key_status
        self.key_body = key_body
        self.key_text = key_text  # a raw (non-JSON) answer to the key check, with key_headers
        self.key_headers = key_headers
        self.html_403 = html_403
        self.trail_status = trail_status
        self.agent_items = agent_items or {}
        self.error = error
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        path = request.url.path
        if path == "/api/v1/trail/summary":
            if self.html_403:
                headers = {"content-type": "text/html", "server": "cloudflare", "cf-ray": "8c1f2e3d4a5b6c7d-IAD"}
                return httpx.Response(403, text=CLOUDFLARE_PAGE, headers=headers)
            if self.key_text is not None:
                return httpx.Response(self.key_status, text=self.key_text, headers=self.key_headers or {})
            if self.key_status != 200:
                return httpx.Response(self.key_status, json=self.key_body or {"detail": "Invalid API key"})
            agents = [
                {
                    "agent_id": name,
                    "handoffs": spec.get("handoffs", 0),
                    "checkpoints": spec.get("checkpoints", 0),
                    "last_active": spec.get("last_active"),
                    "sessions_7d": spec.get("sessions_7d", spec.get("handoffs", 0)),
                    "daily": spec.get("daily", [0] * 7),
                }
                for name, spec in self.agents.items()
            ]
            return httpx.Response(200, json={"agents": agents, "projects": [], "week": {}})
        if path == "/api/v1/trail":
            if self.trail_status != 200:
                return httpx.Response(self.trail_status, json={"detail": "This API key is restricted to multiple projects."})
            agent = request.url.params.get("agent_id")
            items = self.agent_items.get(agent, []) if agent else self.items
            return httpx.Response(200, json={"items": items, "total": len(items)})
        return httpx.Response(404, json={"detail": "Not Found"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def entry(agent: str, kind: str, at: float, picked: list[tuple[str, float]] | None = None) -> dict[str, Any]:
    """One trail item as ``GET /api/v1/trail`` returns it (ids, types and times only are read)."""
    return {
        "id": f"{agent}-{kind}-{int(at)}",
        "agent_id": agent,
        "memory_type": kind,
        "created_at": iso(at),
        "headline": "SECRET HEADLINE that must never reach a slip",
        "picked_up_by": [{"agent_id": a, "agent_verified": False, "picked_up_at": iso(t)} for a, t in (picked or [])],
    }
