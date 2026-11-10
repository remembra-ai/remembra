"""What a crew fix from the doctor rewrites, and crew hooks written for one repository (F19, review round).

Two things the first fix left open:

* ``CREW_GATE_MISSING`` offered ``remembra-crew connect --apply`` with no ``--agent``. Crew connect then wires
  every agent it detects, so an agent in plain relay mode had its relay hooks replaced by crew hooks: a fix that
  said "put the gate back" and rewrote hook files it never named.
* ``remembra-crew connect --scope project`` writes Claude Code's crew hooks into the repository's
  ``.claude/settings.json``. The doctor read the global file only, so in such a repository it still said
  "hooks NOT written" and offered ``remembra-relay connect --apply``.

The hook files are written by the product's own renderers, and each fix command is run (as a dry run) through
the real ``remembra-crew connect`` to see what it would write.
"""

from __future__ import annotations

import io
import json
import shlex
import subprocess
from pathlib import Path

import pytest

from remembra.marshal import commands
from remembra.marshal.render import Style
from remembra.relay.adapters import REGISTRY
from remembra.relay.adapters import crew_hooks as ch
from remembra.relay.adapters.claude_code import CREW_LEGACY_MARKERS
from remembra.relay.crew import install
from tests.marshal_fixtures import FakeHome
from tests.test_fs_doctor_crew import GATE_REL, _cmds, crew, ids, only, run, trust_everything


@pytest.fixture()
def fh(tmp_path: Path) -> FakeHome:
    return FakeHome(tmp_path)


def crew_dry_run(fh: FakeHome, command: str, cwd: Path, monkeypatch) -> str:
    """What the fix command would write: the same arguments through ``remembra-crew connect``, without --apply."""
    argv = shlex.split(command)
    assert argv[:3] == ["remembra-crew", "connect", "--apply"]
    monkeypatch.chdir(cwd)
    out = io.StringIO()
    code = install.main([*argv[3:], "--no-service"], home=fh.home, out=out)
    assert code == 0, out.getvalue()
    return out.getvalue()


def claude_json(report) -> dict:
    return next(a for a in report.json()["agents"] if a["name"] == "claude-code")


def git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def project_crew(fh: FakeHome, repo: Path, name: str = "settings.json") -> Path:
    """What ``remembra-crew connect --scope project --repo R --apply`` leaves in R for Claude Code."""
    (fh.home / GATE_REL).parent.mkdir(parents=True, exist_ok=True)
    (fh.home / GATE_REL).write_text("# gate\n")
    spec = ch.crew_specs()["claude-code"]
    assert spec.project_config is not None
    path = spec.project_config(repo).with_name(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    text, _ = ch.render(None, spec, _cmds(fh), legacy_markers=CREW_LEGACY_MARKERS)
    path.write_text(text)
    return path


@pytest.fixture()
def project_home(fh: FakeHome) -> FakeHome:
    """Claude Code with crew hooks in this repository only; ``fh.root`` (the doctor's cwd) is the repository."""
    fh.install("claude")
    fh.credentials()
    fh.claude_mcp()
    git_repo(fh.root)
    project_crew(fh, fh.root)
    return fh


# ---------------------------------------------------------------------------
# CREW_GATE_MISSING: the fix names the agents that hold crew hooks, and only those
# ---------------------------------------------------------------------------


@pytest.fixture()
def mixed_home(fh: FakeHome) -> FakeHome:
    """Crew on Claude Code and Codex; Gemini detected and in plain relay mode; the gate file is gone."""
    fh.install("claude", "codex", "gemini")
    fh.credentials()
    fh.claude_mcp()
    fh.codex_mcp()
    for agent in ("claude-code", "codex"):
        fh.hooks(agent)
        crew(fh, agent)
    trust_everything(fh)
    fh.hooks("gemini")
    (fh.home / GATE_REL).unlink()
    return fh


def test_gate_fix_names_only_the_agents_in_crew_mode(mixed_home: FakeHome) -> None:
    f = only(run(mixed_home), "CREW_GATE_MISSING")
    assert f.fix is not None
    assert f.fix.command == "remembra-crew connect --apply --agent claude-code --agent codex --include-unverified"
    assert commands.is_allowed(f.fix.command)
    assert "gemini" not in f.fix.command
    # Everything the command rewrites is listed: the gate, and the hook file of each agent it names.
    assert f.fix.writes == ("~/.remembra/crew/bin/crew-gate.py", "~/.claude/settings.json", "~/.codex/hooks.json")
    assert f.fix.backup is True and f.fix.runs_where == "user_terminal"


def test_gate_fix_leaves_a_relay_mode_agent_alone(mixed_home: FakeHome, monkeypatch) -> None:
    gemini = REGISTRY["gemini"].spec.config_path(mixed_home.home)
    before = gemini.read_text()
    assert "remembra-relay" in before and "remembra-crew" not in before
    f = only(run(mixed_home), "CREW_GATE_MISSING")
    assert f.fix is not None and f.fix.command is not None
    plan = crew_dry_run(mixed_home, f.fix.command, mixed_home.root, monkeypatch)
    assert "crew-gate.py" in plan  # it does put the gate back
    assert str(gemini) not in plan and "--hook gemini" not in plan
    assert gemini.read_text() == before


def test_the_unscoped_command_would_have_rewritten_the_relay_mode_agent(mixed_home: FakeHome, monkeypatch) -> None:
    """Why the fix must carry --agent: without it crew connect takes Gemini's relay hooks out."""
    plan = crew_dry_run(mixed_home, "remembra-crew connect --apply --include-unverified", mixed_home.root, monkeypatch)
    assert "--hook gemini" in plan and "remove" in plan


def test_gate_fix_for_one_verified_agent_carries_no_unverified_flag(fh: FakeHome) -> None:
    fh.install("claude", "codex")
    fh.credentials()
    fh.claude_mcp()
    fh.hooks("claude-code")
    crew(fh, "claude-code")
    fh.hooks("codex")  # Codex stays in relay mode
    (fh.home / GATE_REL).unlink()
    f = only(run(fh), "CREW_GATE_MISSING")
    assert f.fix is not None and f.fix.command == "remembra-crew connect --apply --agent claude-code"
    assert f.fix.writes == ("~/.remembra/crew/bin/crew-gate.py", "~/.claude/settings.json")


# ---------------------------------------------------------------------------
# Crew hooks written for one repository (remembra-crew connect --scope project)
# ---------------------------------------------------------------------------


def test_project_scope_crew_hooks_count_in_that_repository(project_home: FakeHome) -> None:
    assert not (project_home.home / ".claude" / "settings.json").exists()  # nothing global
    report = run(project_home)
    assert "HOOKS_NOT_WRITTEN" not in ids(report) and "HOOKS_INCOMPLETE" not in ids(report)
    assert not [f for f in report.findings if f.fix and f.fix.command and f.fix.command.startswith("remembra-relay connect")]
    agent = claude_json(report)
    assert agent["hooks"]["mode"] == "crew"
    assert agent["hooks"]["crew"]["scope"] == "project"
    assert agent["hooks"]["crew"]["config_path"] == "./.claude/settings.json"
    assert {"SessionStart", "SessionEnd"} <= set(agent["hooks"]["crew"]["events"])
    text = report.text(Style())
    assert "crew hooks written for this repository" in text and "hooks NOT written" not in text


def test_project_scope_hooks_in_settings_local_json_count_too(fh: FakeHome) -> None:
    """A tracked settings.json: crew connect writes settings.local.json next to it."""
    fh.install("claude")
    fh.credentials()
    fh.claude_mcp()
    git_repo(fh.root)
    (fh.root / ".claude").mkdir()
    (fh.root / ".claude" / "settings.json").write_text('{"permissions": {}}\n')
    project_crew(fh, fh.root, "settings.local.json")
    report = run(fh)
    assert "HOOKS_NOT_WRITTEN" not in ids(report)
    assert claude_json(report)["hooks"]["crew"]["config_path"] == "./.claude/settings.local.json"


def test_project_scope_is_read_from_a_folder_inside_the_repository(project_home: FakeHome) -> None:
    from remembra.marshal import doctor
    from tests.marshal_fixtures import NOW

    inner = project_home.root / "src" / "pkg"
    inner.mkdir(parents=True)
    report = doctor.run(
        project_home.home, None, False, environ=project_home.environ(), which=project_home.which, now=NOW, cwd=inner
    )
    assert "HOOKS_NOT_WRITTEN" not in ids(report)


def test_outside_that_repository_the_hooks_are_still_not_written(project_home: FakeHome, tmp_path: Path) -> None:
    from remembra.marshal import doctor
    from tests.marshal_fixtures import NOW

    elsewhere = git_repo(tmp_path.parent / f"{tmp_path.name}-elsewhere")
    report = doctor.run(
        project_home.home, None, False, environ=project_home.environ(), which=project_home.which, now=NOW, cwd=elsewhere
    )
    f = only(report, "HOOKS_NOT_WRITTEN", "claude-code")
    assert f.severity == "blocker"
    assert claude_json(report)["hooks"]["mode"] == "none"


def test_a_folder_that_is_no_repository_has_no_project_hooks(fh: FakeHome) -> None:
    fh.install("claude")
    fh.credentials()
    fh.claude_mcp()
    project_crew(fh, fh.root)  # the file is there, but crew connect only writes it for a repository
    assert "HOOKS_NOT_WRITTEN" in ids(run(fh))


def test_another_adapters_project_hooks_do_not_count(fh: FakeHome) -> None:
    fh.install("claude")
    fh.credentials()
    fh.claude_mcp()
    git_repo(fh.root)
    path = project_crew(fh, fh.root)
    path.write_text(path.read_text().replace("--hook claude-code", "--hook codex"))
    assert "HOOKS_NOT_WRITTEN" in ids(run(fh))


@pytest.mark.parametrize("text", ["", "{", "[]", '{"hooks": []}', '{"hooks": {"SessionStart": "x"}}', "\x00\xff"])
def test_an_odd_project_file_never_breaks_the_doctor(fh: FakeHome, text: str) -> None:
    fh.install("claude")
    fh.credentials()
    fh.claude_mcp()
    git_repo(fh.root)
    (fh.root / ".claude").mkdir()
    (fh.root / ".claude" / "settings.json").write_bytes(text.encode("latin-1"))
    report = run(fh)
    assert "HOOKS_NOT_WRITTEN" in ids(report)
    assert claude_json(report)["hooks"]["crew"] is None


def test_global_crew_hooks_win_over_a_project_file(project_home: FakeHome) -> None:
    project_home.hooks("claude-code")
    crew(project_home, "claude-code")
    crew_json = claude_json(run(project_home))["hooks"]["crew"]
    assert crew_json["scope"] == "global" and crew_json["config_path"] == "~/.claude/settings.json"


def test_project_crew_with_global_relay_hooks_is_the_double_brief(project_home: FakeHome) -> None:
    """Crew's own install note: global relay hooks plus project crew hooks run the brief twice in that repository."""
    project_home.hooks("claude-code")
    report = run(project_home)
    f = only(report, "HOOKS_DUPLICATED", "claude-code")
    assert f.severity == "warn" and "this repository" in f.what
    assert any("~/.claude/settings.json" in e and "remembra-relay" in e for e in f.evidence)
    assert any("./.claude/settings.json" in e and "remembra-crew" in e for e in f.evidence)
    # No command rewrites either file: the global relay hooks serve the repositories outside the crew.
    assert f.fix is not None and f.fix.kind == "none" and f.fix.command is None
    assert f.to_do is False
    assert "HOOKS_NOT_WRITTEN" not in ids(report) and "CREW_RELAY_GAP" not in ids(report)


def test_project_scope_fix_rewrites_the_project_file_only(project_home: FakeHome, monkeypatch) -> None:
    (project_home.bin / "remembra-crew").unlink()
    f = only(run(project_home), "HOOKS_STALE_COMMAND", "claude-code")
    assert f.fix is not None
    assert f.fix.command == "remembra-crew connect --apply --agent claude-code --scope project --repo ."
    assert commands.is_allowed(f.fix.command)
    assert f.fix.writes == ("./.claude/settings.json",) and "this repository" in f.fix.text
    assert any("./.claude/settings.json" in e for e in f.evidence)
    plan = crew_dry_run(project_home, f.fix.command, project_home.root, monkeypatch)
    project_file = project_home.root / ".claude" / "settings.json"
    assert str(project_file.resolve()) in plan or str(project_file) in plan
    assert str(project_home.home / ".claude" / "settings.json") not in plan
    assert not (project_home.home / ".claude" / "settings.json").exists()


def test_gate_missing_with_project_hooks_only_gets_the_project_command(project_home: FakeHome) -> None:
    (project_home.home / GATE_REL).unlink()
    f = only(run(project_home), "CREW_GATE_MISSING")
    assert f.fix is not None
    assert f.fix.command == "remembra-crew connect --apply --agent claude-code --scope project --repo ."
    assert f.fix.writes == ("~/.remembra/crew/bin/crew-gate.py", "./.claude/settings.json")
    assert any("./.claude/settings.json" in e for e in f.evidence)


@pytest.mark.parametrize(
    "command",
    [
        "remembra-crew connect --apply --agent claude-code --scope project --repo /etc",
        "remembra-crew connect --apply --scope project --repo .",
        "remembra-crew connect --apply --agent claude-code --scope project --repo . --yes",
        "remembra-crew connect --apply --agent claude-code --scope project --repo ..",
        "remembra-crew connect --apply --agent claude-code --scope global",
    ],
)
def test_other_project_commands_are_refused(command: str) -> None:
    assert not commands.is_allowed(command)


def test_relay_hooks_json_is_still_valid_after_these_reads(project_home: FakeHome) -> None:
    """The doctor only reads: the project file is byte-for-byte what crew connect wrote."""
    path = project_home.root / ".claude" / "settings.json"
    before = path.read_bytes()
    run(project_home)
    assert path.read_bytes() == before and json.loads(before)["hooks"]
