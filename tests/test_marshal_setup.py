"""``remembra_setup``: the exact steps for this machine's OS and agents, with what is already done."""

from __future__ import annotations

from pathlib import Path

import pytest

from remembra.marshal import commands, setup_plan, tools
from tests.marshal_fixtures import FakeHome


@pytest.fixture()
def fh(tmp_path: Path) -> FakeHome:
    return FakeHome(tmp_path)


def steps(fh: FakeHome, agents: list[str] | None = None, os_id: str = "macos", **env: str) -> dict:
    payload = tools.setup_payload(agents, environ=fh.environ(**env), home=fh.home, which=fh.which, os_id=os_id, shell="zsh")
    assert payload["status"] == "ok" and payload["changed_nothing"] is True
    return payload


def test_a_new_machine_gets_every_step_key_first(fh: FakeHome) -> None:
    payload = steps(fh, ["claude-code", "codex", "cursor"])
    by_title = {s["title"]: s for s in payload["steps"]}
    order = [s["title"] for s in payload["steps"]]
    assert order == [
        "Install pipx",
        "Get a free key",
        "Install remembra (with the MCP server)",
        "Save the key and add the Remembra MCP server",
        "See what connect would change (dry run)",
        "Write the hooks",
        "Trust the hooks in Codex",
        "Restart your agents",
        "Check",
    ]
    assert by_title["Install pipx"]["command"] == "brew install pipx && pipx ensurepath"
    key = by_title["Get a free key"]
    assert key["stop"] is True and key["runs_where"] == "user" and commands.SIGNUP_URL in key["action"]
    assert by_title["Install remembra (with the MCP server)"]["command"] == commands.PIPX_INSTALL
    save = by_title["Save the key and add the Remembra MCP server"]
    assert save["command"] == "remembra-install --all" and save["runs_where"] == "user_terminal"
    assert "Never paste a key into a chat." in save["note"]
    assert by_title["See what connect would change (dry run)"]["command"] == (
        "remembra-relay connect --agent claude-code --agent codex --agent cursor"
    )
    write = by_title["Write the hooks"]
    assert (
        write["command"] == "remembra-relay connect --apply --agent claude-code --agent codex --agent cursor --include-unverified"
    )
    assert write["needs_yes"] is True and "cursor" in write["note"]
    assert by_title["Check"]["command"] == "remembra-relay doctor"
    for step in payload["steps"]:
        assert step["command"] is None or commands.is_allowed(step["command"])
    assert payload["rendered"].rstrip().endswith("Nothing was changed.")
    assert payload["rendered"].startswith("remembra setup · macOS · zsh")
    assert "  agents: claude-code, codex, cursor" in payload["rendered"]
    assert all(len(line) <= 76 or commands.is_allowed(line.strip()) for line in payload["rendered"].splitlines())


def test_done_steps_are_marked_from_this_machine(fh: FakeHome) -> None:
    fh.install("pipx", "remembra-relay", "remembra-mcp", "codex")
    fh.credentials()
    fh.codex_mcp()
    fh.hooks("codex")
    fh.trust_codex()
    payload = steps(fh, ["codex"])
    assert all(s["done"] for s in payload["steps"] if s["title"] != "Check"), payload["steps"]
    assert [s["title"] for s in payload["steps"]] == [
        "pipx is installed",
        "A key is saved (~/.codex/config.toml)",  # the first source the hooks read that holds a key
        "remembra is installed",
        "Key saved and MCP server added",
        "Hooks are written",
        "Codex trusts the hooks",
        "Check",
    ]
    assert "1 step left." in payload["rendered"]


def test_default_agents_are_the_ones_found_here(fh: FakeHome) -> None:
    fh.install("codex")
    (fh.home / ".cursor").mkdir()
    assert steps(fh)["agents"] == ["codex", "cursor"]


@pytest.mark.parametrize(
    ("os_id", "command"),
    [
        ("linux-apt", "sudo apt install pipx && pipx ensurepath"),
        ("linux-dnf", "sudo dnf install pipx && pipx ensurepath"),
        ("linux", "python3 -m pip install --user pipx && python3 -m pipx ensurepath"),
    ],
)
def test_pipx_per_os(fh: FakeHome, os_id: str, command: str) -> None:
    assert steps(fh, ["claude-code"], os_id=os_id)["steps"][0]["command"] == command


def test_windows_stops_at_the_docs(fh: FakeHome) -> None:
    payload = steps(fh, ["claude-code"], os_id="windows")
    assert len(payload["steps"]) == 1 and payload["steps"][0]["stop"] is True
    assert "not been run on Windows" in payload["steps"][0]["action"]


def test_qwen_and_kimi_get_the_mcp_by_hand_pointer_and_a_self_hosted_url(fh: FakeHome) -> None:
    payload = steps(fh, ["qwen", "kimi"], REMEMBRA_URL="https://memory.example.org")
    titles = [s["title"] for s in payload["steps"]]
    assert "Add the MCP server by hand: Qwen Code, Kimi Code CLI" in titles
    save = next(s for s in payload["steps"] if s["runs_where"] == "user_terminal")
    assert save["command"] == "remembra-install --all --url https://memory.example.org"


def test_unknown_agents_are_refused(fh: FakeHome) -> None:
    payload = tools.setup_payload(["notepad"], environ=fh.environ(), home=fh.home, which=fh.which)
    assert payload["status"] == "error" and "unknown agent" in payload["error"]


@pytest.mark.parametrize(
    ("system", "release", "expected"),
    [
        ("Darwin", "", "macos"),
        ("Linux", "ID=ubuntu\nID_LIKE=debian\n", "linux-apt"),
        ("Linux", 'ID="fedora"\n', "linux-dnf"),
        ("Linux", 'ID=rocky\nID_LIKE="rhel centos fedora"\n', "linux-dnf"),
        ("Linux", "ID=arch\n", "linux"),
        ("Windows", "", "windows"),
        ("FreeBSD", "", "other"),
    ],
)
def test_detect_os(tmp_path: Path, system: str, release: str, expected: str) -> None:
    path = tmp_path / "os-release"
    path.write_text(release)
    assert setup_plan.detect_os({"SHELL": "/bin/zsh"}, system=system, os_release=path) == (expected, "zsh")
