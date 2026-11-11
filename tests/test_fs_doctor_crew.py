"""The doctor on a machine in crew mode (F19).

``remembra-crew connect`` replaces the relay's ``brief`` / ``close`` hooks with
``remembra-crew start`` / ``end`` (which run the brief and the close). The
doctor looked for relay hooks only, so on every crew machine it reported
"hooks NOT written" as a proven blocker, exited 1, and offered
``remembra-relay connect --apply``, which wrote a second set of hooks next to
the crew ones.

The hook files here are written by the product's own renderers
(``crew_hooks.render`` and the relay adapters), as ``FakeHome`` does.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from remembra.marshal import codex_hooks, commands, doctor, rules
from remembra.marshal.render import Style
from remembra.relay import cli as relay_cli
from remembra.relay.adapters import REGISTRY
from remembra.relay.adapters import crew_hooks as ch
from remembra.relay.adapters.claude_code import CREW_LEGACY_MARKERS
from tests.marshal_fixtures import NOW, FakeHome, FakeTrail, entry, iso

GATE_REL = Path(".remembra") / "crew" / "bin" / "crew-gate.py"


def _cmds(fh: FakeHome) -> ch.CrewCommands:
    crew_bin = fh.bin / "remembra-crew"
    if not crew_bin.exists():
        crew_bin.write_text("#!/bin/sh\n")
        crew_bin.chmod(0o755)
    return ch.CrewCommands(python=sys.executable, gate=str(fh.home / GATE_REL), crew=shlex.quote(str(crew_bin)))


def crew(fh: FakeHome, agent: str, *, gate: bool = True, remove: bool = False) -> Path:
    """What ``remembra-crew connect --apply`` leaves in ``agent``'s hook file (relay entries are taken out)."""
    if gate:
        (fh.home / GATE_REL).parent.mkdir(parents=True, exist_ok=True)
        (fh.home / GATE_REL).write_text("# gate\n")
    path = REGISTRY[agent].spec.config_path(fh.home)
    path.parent.mkdir(parents=True, exist_ok=True)
    legacy = CREW_LEGACY_MARKERS if agent == "claude-code" else ()
    before = path.read_text() if path.exists() else None
    text, _ = ch.render(before, ch.crew_specs()[agent], _cmds(fh), remove=remove, legacy_markers=legacy)
    path.write_text(text)
    return path


def trust_everything(fh: FakeHome) -> None:
    """``[hooks.state]`` records for every hook in ~/.codex/hooks.json, as Codex's /hooks writes them."""
    hooks_path = fh.home / ".codex" / "hooks.json"
    lines = []
    for event, groups in json.loads(hooks_path.read_text())["hooks"].items():
        for g, group in enumerate(groups):
            for i, hook in enumerate(group["hooks"]):
                key = f"{hooks_path}:{codex_hooks.EVENT_KEYS[event]}:{g}:{i}"
                lines.append(
                    f'\n[hooks.state."{key}"]\ntrusted_hash = "{codex_hooks.hook_hash(event, group.get("matcher"), hook)}"\n'
                )
    with (fh.home / ".codex" / "config.toml").open("a") as fh_out:
        fh_out.write("".join(lines))


@pytest.fixture()
def fh(tmp_path: Path) -> FakeHome:
    return FakeHome(tmp_path)


@pytest.fixture()
def crew_home(fh: FakeHome) -> FakeHome:
    """The owner's machine: relay connect ran once, then crew connect replaced its hooks; Codex trusts them."""
    fh.install("claude", "codex")
    fh.credentials()
    fh.claude_mcp()
    fh.codex_mcp()
    for agent in ("claude-code", "codex"):
        fh.hooks(agent)
        crew(fh, agent)
    trust_everything(fh)
    return fh


def run(fh: FakeHome, trail: FakeTrail | None = None, agents: list[str] | None = None) -> doctor.Report:
    return doctor.run(
        fh.home,
        agents,
        trail is not None,
        environ=fh.environ(),
        transport=trail.transport if trail else None,
        which=fh.which,
        now=NOW,
        cwd=fh.root,
    )


def ids(report: doctor.Report, agent: str | None = "any") -> list[str]:
    return [f.id for f in report.findings if agent == "any" or f.agent == agent]


def only(report: doctor.Report, rule: str, agent: str | None = "any") -> rules.Finding:
    found = [f for f in report.findings if f.id == rule and (agent == "any" or f.agent == agent)]
    assert len(found) == 1, [(f.id, f.agent, f.what) for f in report.findings]
    return found[0]


def hook_commands(path: Path) -> dict[str, list[str]]:
    data = json.loads(path.read_text())
    return {event: [h["command"] for g in groups for h in g["hooks"]] for event, groups in data.get("hooks", {}).items()}


def active_trail() -> FakeTrail:
    return FakeTrail(
        agents={
            "claude-code": {"handoffs": 37, "last_active": iso(NOW - 2700), "daily": [10, 10, 10, 10, 10, 20, 18]},
            "codex": {"handoffs": 38, "last_active": iso(NOW - 2880), "daily": [20, 20, 20, 20, 20, 24, 24]},
            "claude-desktop": {"handoffs": 5, "last_active": iso(NOW - 9000), "daily": [5, 5, 5, 5, 8, 10, 10]},
        },
        items=[
            entry("claude-code", "handoff", NOW - 2700, picked=[("codex", NOW - 2000)]),
            entry("codex", "handoff", NOW - 2880, picked=[("claude-code", NOW - 2800)]),
        ],
    )


# ---------------------------------------------------------------------------
# The fault
# ---------------------------------------------------------------------------


def test_the_fixture_is_crew_mode_as_the_product_writes_it(crew_home: FakeHome) -> None:
    claude = hook_commands(crew_home.home / ".claude" / "settings.json")
    assert claude["SessionStart"][0].endswith("start --hook claude-code --agent claude-code # remembra-crew")
    assert claude["SessionEnd"][0].endswith("end --hook claude-code --agent claude-code # remembra-crew")
    assert not [c for cs in claude.values() for c in cs if "remembra-relay" in c]  # crew connect took them out
    codex = hook_commands(crew_home.home / ".codex" / "hooks.json")
    assert sum(len(cs) for cs in codex.values()) == 7 and not [c for cs in codex.values() for c in cs if "remembra-relay" in c]


def test_crew_hooks_count_as_hooks(crew_home: FakeHome) -> None:
    report = run(crew_home)
    assert "HOOKS_NOT_WRITTEN" not in ids(report)
    assert report.exit_code == 0, [(f.id, f.agent, f.marker, f.what) for f in report.findings]
    text = report.text(Style())
    assert "hooks NOT written" not in text and "[!!]" not in text
    assert text.count("crew hooks written") == 2
    for finding in report.findings:  # nothing sends the user to the relay's connect
        assert finding.fix is None or not (finding.fix.command or "").startswith("remembra-relay connect"), finding
    for agent in report.json()["agents"]:
        if agent["name"] in ("claude-code", "codex"):
            assert agent["hooks"]["state"] == "crew hooks written" and agent["hooks"]["mode"] == "crew"
            assert agent["hooks"]["missing"] == []


def test_the_whole_slip_on_an_active_crew_machine_has_nothing_to_do(crew_home: FakeHome) -> None:
    report = run(crew_home, active_trail())
    assert report.exit_code == 0
    assert not [f for f in report.findings if f.actionable], [(f.id, f.agent, f.what) for f in report.findings]
    text = report.text(Style())
    assert "Nothing to do." in text and "trusted" in text
    for line in text.splitlines():
        assert len(line) <= 76 or commands.is_allowed(line.replace("┊", "").strip()), line


def test_relay_connect_plans_no_change_over_crew_hooks(crew_home: FakeHome) -> None:
    for name in ("claude-code", "codex"):
        path = REGISTRY[name].spec.config_path(crew_home.home)
        before = path.read_text()
        change = REGISTRY[name].plan(crew_home.home, crew_home.relay)
        assert change.changed is False and change.after == before
        assert any("remembra-crew" in line for line in change.summary), change.summary


def test_relay_connect_apply_leaves_a_crew_file_alone(crew_home: FakeHome, monkeypatch, capsys) -> None:
    """The command the doctor used to offer: it must not add relay hooks next to the crew ones."""
    monkeypatch.setenv("HOME", str(crew_home.home))
    monkeypatch.setenv("PATH", str(crew_home.bin))
    for key in ("REMEMBRA_API_KEY", "REMEMBRA_URL", "CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        monkeypatch.delenv(key, raising=False)
    files = {n: REGISTRY[n].spec.config_path(crew_home.home) for n in ("claude-code", "codex")}
    before = {n: p.read_bytes() for n, p in files.items()}
    code = relay_cli.main(
        ["connect", "--apply", "--agent", "claude-code", "--agent", "codex", "--relay-command", crew_home.relay]
    )
    out = capsys.readouterr().out
    assert code == 0, out
    for name, path in files.items():
        assert path.read_bytes() == before[name], name
        assert not list(path.parent.glob(f"{path.name}.bak-relay-*"))  # nothing was written, so no backup
        assert not [c for cs in hook_commands(path).values() for c in cs if "remembra-relay" in c]
    assert out.count("crew hooks") >= 2 and "remembra-crew connect" in out


def test_relay_connect_works_again_once_crew_is_uninstalled(crew_home: FakeHome) -> None:
    crew(crew_home, "claude-code", remove=True)
    change = REGISTRY["claude-code"].plan(crew_home.home, crew_home.relay)
    assert change.changed and "brief --hook claude-code --agent claude-code" in change.after
    report = run(crew_home, agents=["claude-code"])
    f = only(report, "HOOKS_NOT_WRITTEN", "claude-code")
    assert f.fix is not None and f.fix.command == "remembra-relay connect --apply --agent claude-code"


@pytest.mark.parametrize("agent", sorted(ch.crew_specs()))
def test_every_adapters_crew_hooks_are_recognised(fh: FakeHome, agent: str) -> None:
    """The three hook-file shapes (Claude-style JSON, Cursor's JSON, Kimi's TOML tables), relay first then crew."""
    fh.credentials()
    if agent != "kimi":  # its relay hooks are TOML tables: crew connect on a fresh file is the case here
        fh.hooks(agent)
    path = crew(fh, agent)
    before = path.read_text()
    report = run(fh, agents=[agent])
    item = report.json()["agents"][0]
    assert item["hooks"]["mode"] == "crew" and item["hooks"]["state"] == "crew hooks written", item["hooks"]
    assert not {"HOOKS_NOT_WRITTEN", "UNVERIFIED_NOT_WRITTEN", "HOOKS_INCOMPLETE", "HOOKS_DUPLICATED"} & set(ids(report))
    change = REGISTRY[agent].plan(fh.home, fh.relay)
    assert change.changed is False and path.read_text() == before
    for finding in report.findings:
        assert finding.fix is None or not (finding.fix.command or "").startswith("remembra-relay connect"), finding


def test_another_adapters_crew_hooks_do_not_count_for_this_one(fh: FakeHome) -> None:
    """Gemini CLI and Qwen Code write the same shape: a crew hook is matched by its own --hook name."""
    fh.install("claude")
    fh.credentials()
    path = crew(fh, "claude-code")
    path.write_text(path.read_text().replace("--hook claude-code", "--hook codex"))
    report = run(fh, agents=["claude-code"])
    assert report.json()["agents"][0]["hooks"]["mode"] == "none"
    assert only(report, "HOOKS_NOT_WRITTEN", "claude-code").fix.command == "remembra-relay connect --apply --agent claude-code"
    assert REGISTRY["claude-code"].plan(fh.home, fh.relay).changed is True


@pytest.mark.parametrize(
    "text", ["", "{}", '{"hooks": []}', '{"hooks": {"SessionStart": "x"}}', '{"hooks": {"SessionStart": [1, null]}}']
)
def test_odd_hook_files_are_not_crew_mode(fh: FakeHome, text: str) -> None:
    fh.install("claude")
    path = fh.home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    report = run(fh, agents=["claude-code"])
    assert report.json()["agents"][0]["hooks"]["mode"] == "none"
    assert report.json()["agents"][0]["hooks"]["crew"] is None


def test_plain_relay_mode_is_untouched(fh: FakeHome) -> None:
    fh.install("claude")
    fh.credentials()
    fh.hooks("claude-code")
    report = run(fh, agents=["claude-code"])
    assert ids(report) == [] and "hooks written" in report.text(Style()) and "crew" not in report.text(Style())
    assert report.json()["agents"][0]["hooks"]["mode"] == "relay"
    assert REGISTRY["claude-code"].plan(fh.home, fh.relay).changed is False


# ---------------------------------------------------------------------------
# What crew mode does not cover is named, as a note
# ---------------------------------------------------------------------------


def test_missing_companion_hooks_are_actionable(crew_home: FakeHome) -> None:
    for agent in ("claude-code", "codex"):
        path = REGISTRY[agent].spec.config_path(crew_home.home)
        data = json.loads(path.read_text())
        for groups in data["hooks"].values():
            for group in groups:
                group["hooks"] = [h for h in group["hooks"] if " relay --hook " not in h.get("command", "")]
        path.write_text(json.dumps(data))
    report = run(crew_home)
    for agent in ("claude-code", "codex"):
        gap = only(report, "CREW_RELAY_GAP", agent)
        assert gap.severity == "warn" and gap.actionable
        assert gap.fix and "--agent " + agent in gap.fix.command


def test_current_companion_hooks_cover_noncrew_events(crew_home: FakeHome) -> None:
    report = run(crew_home)
    assert "CREW_RELAY_GAP" not in ids(report)


# ---------------------------------------------------------------------------
# Real crew-mode faults
# ---------------------------------------------------------------------------


def test_relay_and_crew_hooks_on_one_event_are_reported(crew_home: FakeHome) -> None:
    crew_home.hooks("claude-code")  # the render the old `connect --apply` wrote: relay hooks beside the crew ones
    start = hook_commands(crew_home.home / ".claude" / "settings.json")["SessionStart"]
    assert any("remembra-crew" in c for c in start) and any(
        c.endswith("brief --hook claude-code --agent claude-code") for c in start
    )
    report = run(crew_home)
    f = only(report, "HOOKS_DUPLICATED", "claude-code")
    assert (f.severity, f.inferred) == ("warn", False)
    assert "SessionStart" in f.what and "SessionEnd" in f.what
    assert f.fix is not None and f.fix.command == "remembra-crew connect --apply --agent claude-code"
    assert f.fix.runs_where == "user_terminal" and commands.is_allowed(f.fix.command)
    assert "HOOKS_NOT_WRITTEN" not in ids(report) and "HOOKS_INCOMPLETE" not in ids(report)
    text = report.text(Style())
    assert "it asks you to confirm" in text and "it involves your key" not in text
    # crew connect really does take them out again
    crew(crew_home, "claude-code")
    assert "HOOKS_DUPLICATED" not in ids(run(crew_home))


def test_unverified_crew_adapter_gets_the_flag_crew_connect_needs(crew_home: FakeHome) -> None:
    crew_home.hooks("codex")
    f = only(run(crew_home), "HOOKS_DUPLICATED", "codex")
    assert ch.crew_specs()["codex"].verified is False
    assert f.fix is not None and f.fix.command == "remembra-crew connect --apply --agent codex --include-unverified"
    assert commands.is_allowed(f.fix.command)


def test_missing_gate_is_reported_once(crew_home: FakeHome) -> None:
    (crew_home.home / GATE_REL).unlink()
    report = run(crew_home)
    f = only(report, "CREW_GATE_MISSING")
    assert (f.severity, f.agent, f.inferred) == ("warn", None, False)
    assert any("crew-gate.py" in e for e in f.evidence)
    # The agents that hold crew hooks are named: without --agent crew connect wires every agent it detects.
    assert f.fix is not None
    assert f.fix.command == "remembra-crew connect --apply --agent claude-code --agent codex --include-unverified"
    assert f.fix.runs_where == "user_terminal"
    assert report.exit_code == 1


def test_crew_hooks_calling_a_missing_command(crew_home: FakeHome) -> None:
    (crew_home.bin / "remembra-crew").unlink()
    report = run(crew_home)
    f = only(report, "HOOKS_STALE_COMMAND", "claude-code")
    assert f.severity == "blocker" and "its crew hooks call" in f.what and "no longer exists" in f.what
    assert any("remembra-crew: no such file" in e for e in f.evidence) or any("no such file" in e for e in f.evidence)
    assert f.fix is not None and f.fix.command == "remembra-crew connect --apply --agent claude-code"
    assert "hooks call a missing command" in report.text(Style())


def test_half_written_crew_hooks_are_not_fixed_with_relay_connect(crew_home: FakeHome) -> None:
    path = crew_home.home / ".claude" / "settings.json"
    data = json.loads(path.read_text())
    del data["hooks"]["SessionEnd"]
    path.write_text(json.dumps(data, indent=2))
    report = run(crew_home)
    f = only(report, "HOOKS_NOT_WRITTEN", "claude-code")
    assert f.severity == "blocker" and any("SessionEnd" in e for e in f.evidence)
    assert f.fix is not None and f.fix.command == "remembra-crew connect --apply --agent claude-code"
    assert REGISTRY["claude-code"].plan(crew_home.home, crew_home.relay).changed is False  # still crew's file


def test_untrusted_crew_hooks_in_codex_are_found(crew_home: FakeHome) -> None:
    config = crew_home.home / ".codex" / "config.toml"
    config.write_text(config.read_text().split("\n[hooks.state.")[0])
    report = run(crew_home)
    f = only(report, "CODEX_TRUST_MISSING", "codex")
    assert f.severity == "blocker" and any("7 remembra-crew hooks" in e for e in f.evidence)
    assert "trust NOT recorded" in report.text(Style())


def test_trusted_crew_hooks_in_codex_say_trusted(crew_home: FakeHome) -> None:
    report = run(crew_home)
    assert not [i for i in ids(report, "codex") if i.startswith("CODEX_")]
    block = report.text(Style()).split("codex", 1)[1]
    assert "crew hooks written" in block.splitlines()[0] and "trusted" in block.splitlines()[0]


# ---------------------------------------------------------------------------
# Evidence about backups
# ---------------------------------------------------------------------------


def test_a_crew_backup_is_named_instead_of_never_wrote(fh: FakeHome) -> None:
    fh.install("codex")
    fh.credentials()
    hooks = fh.home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text("{}\n")
    (fh.home / ".codex" / "hooks.json.bak-crew-20261024-195829").write_text("{}")
    f = only(run(fh, agents=["codex"]), "HOOKS_NOT_WRITTEN", "codex")
    assert any("hooks.json.bak-crew-20261024-195829" in e and "remembra-crew connect" in e for e in f.evidence), f.evidence
    assert not any("never wrote" in e for e in f.evidence)


def test_no_backup_no_longer_claims_connect_never_wrote_the_file(fh: FakeHome) -> None:
    """connect keeps no backup of a file it creates, so "no backup" never proved "never wrote"."""
    fh.install("codex")
    fh.credentials()
    hooks = fh.home / ".codex" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text("{}\n")
    f = only(run(fh, agents=["codex"]), "HOOKS_NOT_WRITTEN", "codex")
    assert not any("never wrote" in e for e in f.evidence), f.evidence
    assert any("no *.bak-relay-* backup here" in e and "dry run only, or never run" in e for e in f.evidence)


# ---------------------------------------------------------------------------
# The "here" line: a failure a later run of the same command outlived
# ---------------------------------------------------------------------------


def slot(command: str, ago: float, **extra: object) -> dict:
    """A status.json slot as ``outbox.record`` writes it."""
    return {"command": command, "at": iso(NOW - ago), "ts": NOW - ago, **extra}


def _here(report: doctor.Report, agent: str) -> str:
    lines = report.text(Style()).splitlines()
    start = next(i for i, line in enumerate(lines) if f" {agent} " in line and "hooks" in line)
    return next((line for line in lines[start + 1 : start + 4] if "here" in line), "")


def test_a_failure_older_than_a_success_of_the_same_command_is_not_shown(crew_home: FakeHome) -> None:
    crew_home.status(
        {
            "codex": {
                "last_success": slot("close", 48 * 60),
                "last_failure": slot("close", 5 * 86400, error="ReadTimeout: The read operation timed out", http_status=None),
            },
            "claude-code": {
                "last_success": slot("brief", 45 * 60),
                "last_failure": slot("brief", 30 * 3600, error="HTTP 521", http_status=521),
            },
        }
    )
    report = run(crew_home)
    assert _here(report, "codex").strip().endswith("close ok 48m ago")
    assert _here(report, "claude-code").strip().endswith("brief ok 45m ago")
    assert "failed" not in report.text(Style())
    codex = next(a for a in report.json()["agents"] if a["name"] == "codex")
    assert codex["last_failure"]["command"] == "close" and codex["last_failure_superseded"] is True  # the record stays


def test_a_close_failure_is_not_shown_once_a_handoff_reached_the_trail(crew_home: FakeHome) -> None:
    crew_home.status(
        {
            "codex": {
                "last_success": slot("brief", 20 * 60),
                "last_failure": slot("close", 2 * 86400, error="ReadTimeout", http_status=None),
            }
        }
    )
    with_trail = run(crew_home, active_trail())  # codex handed off 48 minutes ago
    assert _here(with_trail, "codex").strip().endswith("brief ok 20m ago")
    offline = run(crew_home)  # without the trail nothing shows the close worked: the failure stays
    assert "close failed 2d ago" in _here(offline, "codex")
    codex = next(a for a in offline.json()["agents"] if a["name"] == "codex")
    assert codex["last_failure_superseded"] is False


def test_a_failure_newer_than_the_success_is_still_shown(crew_home: FakeHome) -> None:
    crew_home.status(
        {
            "codex": {
                "last_success": slot("close", 3 * 3600),
                "last_failure": slot("close", 600, error="HTTP 401: bad key", http_status=401),
            }
        }
    )
    assert "close failed 10m ago (HTTP 401)" in _here(run(crew_home), "codex")


@pytest.mark.parametrize(
    "slots",
    [
        {"last_failure": {"command": "close"}},  # no time recorded
        {"last_failure": {"command": "close", "ts": "not a time"}, "last_success": {"command": "close", "ts": None}},
        {"last_failure": {"ts": NOW - 60}, "last_success": {}},  # no command
        {"last_failure": "garbage", "last_success": ["x"]},
    ],
)
def test_odd_status_records_never_break_the_slip(crew_home: FakeHome, slots: dict) -> None:
    crew_home.status({"codex": slots})
    report = run(crew_home, active_trail())
    assert "codex" in report.text(Style()) and report.json()["agents"]


# ---------------------------------------------------------------------------
# Coverage: agents that only use the MCP server; where the crew runtime is checked
# ---------------------------------------------------------------------------


def test_an_agent_seen_on_the_trail_without_hooks_here_is_listed(crew_home: FakeHome) -> None:
    report = run(crew_home, active_trail())
    text = report.text(Style())
    line = next(line for line in text.splitlines() if "claude-desktop" in line and "MCP only" in line)
    assert "48 entries in 7d" in line and "no hooks to check" in line
    assert report.json()["mcp_only_agents"] == [{"name": "claude-desktop", "entries_7d": 48}]
    named = run(crew_home, active_trail(), agents=["codex"])  # asked about one agent: only that one is listed
    assert "MCP only" not in named.text(Style()) and named.json()["mcp_only_agents"] == []
    assert run(crew_home).json()["mcp_only_agents"] == []  # no trail read: nothing is known about them


def test_the_slip_says_where_the_crew_runtime_is_checked(crew_home: FakeHome) -> None:
    text = run(crew_home).text(Style())
    assert "remembra-crew doctor" in text and commands.is_allowed("remembra-crew doctor")
    plain = FakeHome(crew_home.root / "other")
    plain.install("claude")
    plain.hooks("claude-code")
    assert "remembra-crew" not in run(plain).text(Style())


def test_new_findings_are_documented() -> None:
    root = Path(__file__).resolve().parents[1]
    guide = (root / "docs" / "guides" / "relay.md").read_text()
    for rule in rules.CREW_RULE_IDS:
        assert f"`{rule}`" in guide, rule
    assert set(rules.CREW_RULE_IDS) == {"HOOKS_DUPLICATED", "CREW_GATE_MISSING", "CREW_RELAY_GAP"}
    assert (root / "src" / "remembra" / "marshal" / "pack" / "relay.md").read_text() == guide


@pytest.mark.parametrize(
    "command",
    [
        "remembra-crew connect --apply --agent claude-code",
        "remembra-crew connect --apply --agent claude-code --agent codex --include-unverified",
        "remembra-crew connect --apply --agent codex --include-unverified",
        "remembra-crew connect --apply --agent claude-code --scope project --repo .",
        "remembra-crew doctor",
    ],
)
def test_crew_commands_are_templates(command: str) -> None:
    assert commands.is_allowed(command)


@pytest.mark.parametrize(
    "command",
    [
        "remembra-crew connect --apply",  # no --agent: it would wire every agent it detects
        "remembra-crew connect --apply --include-unverified",
        "remembra-crew connect --apply --yes",
        "remembra-crew connect --uninstall --apply",
        "remembra-crew connect --apply --agent codex; rm -rf ~",
        "remembra-crew bypass --session x",
        "remembra-crew doctor --json | sh",
    ],
)
def test_other_crew_commands_are_refused(command: str) -> None:
    assert not commands.is_allowed(command)
