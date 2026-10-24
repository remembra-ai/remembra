"""The relay brief says crew mode is automatic only where the reading agent's crew hooks are installed."""

import json
from pathlib import Path
from types import SimpleNamespace

from remembra.crew.schemas import CREW_HOOK_MARKER
from remembra.relay.adapters import crew_hooks_installed
from remembra.relay.cli import crew_footer_here
from remembra.relay.handoff import CREW_FOOTER, crew_footer_off, render_crew_block


def _gate(home: Path) -> None:
    gate = home / ".remembra" / "crew" / "bin" / "crew-gate.py"
    gate.parent.mkdir(parents=True)
    gate.write_text("# gate\n")


def _settings(path: Path, command: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    hooks = {"PreToolUse": [{"hooks": [{"type": "command", "command": command}]}]}
    path.write_text(json.dumps({"hooks": hooks}))


def _ctx(home: Path, repo: Path, agent: str = "claude-code") -> SimpleNamespace:
    return SimpleNamespace(agent=agent, home=home, cwd=repo, repo=SimpleNamespace(toplevel=str(repo)))


def test_not_installed_without_hooks(tmp_path: Path) -> None:
    _gate(tmp_path)
    _settings(tmp_path / ".claude" / "settings.json", "remembra-relay brief --hook claude-code")
    assert crew_hooks_installed(tmp_path, "claude-code", None) is False


def test_installed_globally(tmp_path: Path) -> None:
    _gate(tmp_path)
    _settings(tmp_path / ".claude" / "settings.json", f"crew-gate pretool {CREW_HOOK_MARKER}")
    assert crew_hooks_installed(tmp_path, "claude-code", None) is True


def test_installed_for_one_repo_only(tmp_path: Path) -> None:
    home, repo, other = tmp_path / "home", tmp_path / "repo", tmp_path / "other"
    home.mkdir()
    _gate(home)
    _settings(repo / ".claude" / "settings.local.json", f"crew-gate pretool {CREW_HOOK_MARKER}")
    assert crew_hooks_installed(home, "claude-code", str(repo)) is True
    assert crew_hooks_installed(home, "claude-code", str(other)) is False


def test_hooks_without_the_gate_do_nothing(tmp_path: Path) -> None:
    _settings(tmp_path / ".claude" / "settings.json", f"crew-gate pretool {CREW_HOOK_MARKER}")
    assert crew_hooks_installed(tmp_path, "claude-code", None) is False


def test_unknown_agent_is_undecided(tmp_path: Path) -> None:
    assert crew_hooks_installed(tmp_path, "no-such-agent", None) is None


def test_brief_footer_rewritten_where_hooks_are_missing(tmp_path: Path) -> None:
    rendered = "Last session: x\n" + render_crew_block({"project_id": "p", "live": 0})
    assert rendered.endswith(CREW_FOOTER)
    out = crew_footer_here(_ctx(tmp_path, tmp_path), rendered)
    assert CREW_FOOTER not in out
    assert out.splitlines()[-1] == crew_footer_off("claude-code")
    assert "remembra-crew connect --agent claude-code" in out
    assert out.splitlines()[0] == "Last session: x"


def test_brief_footer_kept_where_hooks_are_installed(tmp_path: Path) -> None:
    _gate(tmp_path)
    _settings(tmp_path / ".claude" / "settings.json", f"crew-gate pretool {CREW_HOOK_MARKER}")
    rendered = render_crew_block({"project_id": "p", "live": 0})
    assert crew_footer_here(_ctx(tmp_path, tmp_path), rendered) == rendered


def test_brief_without_crew_block_untouched(tmp_path: Path) -> None:
    assert crew_footer_here(_ctx(tmp_path, tmp_path), "Last session: x") == "Last session: x"
