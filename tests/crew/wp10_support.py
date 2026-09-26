"""Shared helpers for the WP-10 (adapters and installers) tests: temp HOME, temp git repos, the stub gate."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

STUB_GATE = Path(__file__).with_name("wp10_stub_gate.py")


def make_crew_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A temp HOME and an isolated git config: nothing reads or writes the real user config."""
    home = tmp_path / "home"
    home.mkdir()
    gitconfig = tmp_path / "gitconfig"
    gitconfig.write_text("[user]\n\tname = wp10\n\temail = wp10@example.invalid\n[init]\n\tdefaultBranch = main\n")
    log = tmp_path / "stub-gate.log"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(gitconfig))
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("WP10_STUB_LOG", str(log))
    scratch = tmp_path / "tmpdir"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    for var in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "HUSKY", "LEFTHOOK"):
        monkeypatch.delenv(var, raising=False)
    return {"home": home, "tmp": tmp_path, "gitconfig": gitconfig, "log": log}


def git(repo: Path, *args: str, check: bool = True, input: str | None = None) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, input=input, timeout=60)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stdout}{proc.stderr}")
    return proc


def make_repo(path: Path) -> Path:
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "README.md").write_text("# wp10 test repo\n")
    git(path, "add", "README.md")
    git(path, "commit", "-qm", "init")
    return path


def stub_log(env: dict[str, Any]) -> list[dict[str, Any]]:
    path: Path = env["log"]
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def verbs(env: dict[str, Any]) -> list[str]:
    return [entry["verb"] for entry in stub_log(env)]


def gate_command(env: dict[str, Any]):  # -> githooks.GateCommand
    from remembra.relay.crew.githooks import GateCommand

    return GateCommand(python=sys.executable, gate=str(STUB_GATE))


def tree_snapshot(root: Path) -> dict[str, str]:
    """Every file under ``root`` with its content (to prove that nothing was written)."""
    out = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            try:
                out[str(path.relative_to(root))] = path.read_text(errors="replace")
            except OSError:
                out[str(path.relative_to(root))] = "<unreadable>"
    return out
