"""Shared fixtures for relay tests: temp git repos and Claude Code JSONL transcripts."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Relay Test",
    "GIT_AUTHOR_EMAIL": "relay@example.test",
    "GIT_COMMITTER_NAME": "Relay Test",
    "GIT_COMMITTER_EMAIL": "relay@example.test",
    "GIT_CONFIG_NOSYSTEM": "1",
}


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, **GIT_ENV, "HOME": str(cwd)}
    env.pop("GIT_DIR", None)
    out = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True)
    return out.stdout.strip()


def commit(repo: Path, path: str, content: str, message: str) -> str:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_remote_and_clones(tmp: Path, names: tuple[str, ...] = ("laptop",)) -> tuple[Path, dict[str, Path]]:
    """A bare 'origin' with one initial commit and a clone per name."""
    bare = tmp / "origin.git"
    git(tmp, "init", "-q", "--bare", "-b", "main", str(bare))
    seed = tmp / "seed"
    git(tmp, "init", "-q", "-b", "main", str(seed))
    commit(seed, "README.md", "# widget\n", "chore: initial commit")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "-q", "origin", "main")
    clones = {}
    for name in names:
        dest = tmp / name / "widget"
        dest.parent.mkdir(parents=True, exist_ok=True)
        git(tmp, "clone", "-q", str(bare), str(dest))
        clones[name] = dest
    return bare, clones


class Transcript:
    """Builds a Claude Code session JSONL (assistant tool_use + user tool_result lines)."""

    def __init__(self, session_id: str, cwd: Path, started: str = "2026-09-25T10:00:00.000Z") -> None:
        self.session_id = session_id
        self.cwd = str(cwd)
        self.lines: list[dict[str, Any]] = []
        self.n = 0
        self.ts = started
        self.lines.append(self._base("user", {"role": "user", "content": "please build the widget"}))

    def _base(self, kind: str, message: dict[str, Any], **extra: Any) -> dict[str, Any]:
        self.n += 1
        return {
            "type": kind,
            "sessionId": self.session_id,
            "cwd": self.cwd,
            "gitBranch": "main",
            "timestamp": self.ts,
            "uuid": f"u{self.n}",
            "message": message,
            **extra,
        }

    def tool(self, name: str, tool_input: dict[str, Any], result: Any, is_error: bool = False, tur: Any = None) -> None:
        tool_id = f"toolu_{self.n:04d}"
        self.lines.append(
            self._base(
                "assistant",
                {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": name, "input": tool_input}]},
            )
        )
        block: dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_id, "content": result}
        if is_error:
            block["is_error"] = True
        extra = {"toolUseResult": tur} if tur is not None else {}
        self.lines.append(self._base("user", {"role": "user", "content": [block]}, **extra))

    def bash(self, command: str, exit_code: int = 0, output: str = "") -> None:
        if exit_code:
            text = f"Exit code {exit_code}\n{output}"
            self.tool("Bash", {"command": command, "description": "run"}, text, is_error=True, tur=f"Error: {text}")
        else:
            self.tool(
                "Bash",
                {"command": command, "description": "run"},
                output,
                tur={"stdout": output, "stderr": "", "interrupted": False, "isImage": False},
            )

    def write(self, path: Path) -> Path:
        path.write_text("\n".join(json.dumps(line) for line in self.lines) + "\n")
        return path
