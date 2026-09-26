"""WP-10: crew hook entries in each agent's config (spec §8.2, §8.3) and the AGENTS.md crew block."""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from remembra.crew import gatecore
from remembra.crew import schemas as S
from remembra.relay.adapters import agents_md, claude_code, codex, cursor, gemini, kimi, qwen
from remembra.relay.adapters.crew_hooks import CREW_MARKER, CrewCommands, CrewHook, crew_specs, render

CMDS = CrewCommands(python="/usr/bin/python3", gate="/home/u/.remembra/crew/bin/crew-gate.py", crew="/opt/bin/remembra-crew")
PY_GATE = "/usr/bin/python3 -I /home/u/.remembra/crew/bin/crew-gate.py"
MATCHER = "Edit|Write|MultiEdit|NotebookEdit|Bash|mcp__.*"

# Spec §8.2, entry by entry (plus the S0 asyncRewake waiter on Stop).
EXPECTED_CLAUDE = {
    "SessionStart": [
        {
            "hooks": [
                {
                    "type": "command",
                    "timeout": 15,
                    "command": "/opt/bin/remembra-crew start --hook claude-code --agent claude-code # remembra-crew",
                }
            ]
        }
    ],
    "UserPromptSubmit": [
        {"hooks": [{"type": "command", "timeout": 3, "command": f"{PY_GATE} turn --hook claude-code # remembra-crew"}]}
    ],
    "PreToolUse": [
        {
            "matcher": MATCHER,
            "hooks": [{"type": "command", "timeout": 5, "command": f"{PY_GATE} pretool --hook claude-code # remembra-crew"}],
        }
    ],
    "PostToolUse": [
        {
            "matcher": MATCHER,
            "hooks": [
                {
                    "type": "command",
                    "async": True,
                    "timeout": 30,
                    "command": f"{PY_GATE} posttool --hook claude-code # remembra-crew",
                }
            ],
        }
    ],
    "Stop": [
        {"hooks": [{"type": "command", "timeout": 5, "command": f"{PY_GATE} stop --hook claude-code # remembra-crew"}]},
        {
            "hooks": [
                {
                    "type": "command",
                    "asyncRewake": True,
                    "timeout": 300,
                    "command": f"{PY_GATE} rewake --hook claude-code # remembra-crew",
                }
            ]
        },
    ],
    "StopFailure": [
        {
            "hooks": [
                {
                    "type": "command",
                    "timeout": 20,
                    "command": "/opt/bin/remembra-crew stall --hook claude-code --agent claude-code # remembra-crew",
                }
            ]
        }
    ],
    "PreCompact": [
        {"hooks": [{"type": "command", "timeout": 10, "command": f"{PY_GATE} precompact --hook claude-code # remembra-crew"}]}
    ],
    "SessionEnd": [
        {
            "hooks": [
                {
                    "type": "command",
                    "timeout": 20,
                    "command": "/opt/bin/remembra-crew end --hook claude-code --agent claude-code # remembra-crew",
                }
            ]
        }
    ],
}

USER_SETTINGS = {
    "model": "opus",
    "permissions": {"allow": ["Bash(ls:*)"]},
    "hooks": {
        "SessionStart": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "/usr/local/bin/remembra-relay brief --hook claude-code --agent claude-code",
                        "timeout": 15,
                    }
                ]
            },
            {"hooks": [{"type": "command", "command": "python3 /x/integrations/claude-code/session_start.py"}]},
            {"hooks": [{"type": "command", "command": "echo my-own-start"}]},
        ],
        "SessionEnd": [
            {
                "hooks": [
                    {
                        "type": "command",
                        "command": "/usr/local/bin/remembra-relay close --hook claude-code --agent claude-code",
                        "timeout": 15,
                    }
                ]
            }
        ],
        "PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "my-bash-guard"}]}],
        "Notification": [{"hooks": [{"type": "command", "command": "say done"}]}],
    },
}


def _render_claude(before: str | None, remove: bool = False) -> tuple[str, list[str]]:
    return render(before, claude_code.CREW, CMDS, remove=remove, legacy_markers=claude_code.CREW_LEGACY_MARKERS)


def test_claude_block_matches_spec_on_an_empty_file():
    text, summary = _render_claude(None)
    assert json.loads(text) == {"hooks": EXPECTED_CLAUDE}
    assert len(summary) == 9


def test_claude_block_replaces_relay_and_legacy_and_keeps_user_hooks():
    before = json.dumps(USER_SETTINGS, indent=2)
    text, summary = _render_claude(before)
    data = json.loads(text)
    assert data["model"] == "opus" and data["permissions"] == USER_SETTINGS["permissions"]
    hooks = data["hooks"]
    assert hooks["Notification"] == USER_SETTINGS["hooks"]["Notification"]
    assert hooks["SessionStart"] == [
        {"hooks": [{"type": "command", "command": "echo my-own-start"}]},
        *EXPECTED_CLAUDE["SessionStart"],
    ]
    assert hooks["SessionEnd"] == EXPECTED_CLAUDE["SessionEnd"]
    assert hooks["PreToolUse"] == [USER_SETTINGS["hooks"]["PreToolUse"][0], *EXPECTED_CLAUDE["PreToolUse"]]
    assert "remembra-relay" not in text and "session_start.py" not in text
    assert any("remembra-relay brief" in line for line in summary)
    assert any("session_start.py" in line for line in summary)

    # Idempotent: the exact same text, and nothing to report.
    again, again_summary = _render_claude(text)
    assert again == text and again_summary == []


def test_claude_rerender_updates_an_outdated_crew_entry_in_place():
    text, _ = _render_claude(None)
    data = json.loads(text)
    data["hooks"]["PreToolUse"][0]["hooks"][0]["timeout"] = 99  # an older crew version
    data["hooks"]["PreToolUse"].insert(0, {"matcher": "Bash", "hooks": [{"type": "command", "command": "mine"}]})
    fixed, summary = _render_claude(json.dumps(data))
    assert json.loads(fixed)["hooks"]["PreToolUse"] == [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "mine"}]},
        *EXPECTED_CLAUDE["PreToolUse"],
    ]
    assert any("replace crew hook" in s for s in summary)


def test_claude_uninstall_removes_only_crew_entries():
    installed, _ = _render_claude(json.dumps(USER_SETTINGS))
    removed, summary = _render_claude(installed, remove=True)
    data = json.loads(removed)
    assert CREW_MARKER not in removed
    assert data["hooks"] == {
        "SessionStart": [{"hooks": [{"type": "command", "command": "echo my-own-start"}]}],
        "PreToolUse": USER_SETTINGS["hooks"]["PreToolUse"],
        "Notification": USER_SETTINGS["hooks"]["Notification"],
    }
    assert len(summary) == 9


def test_every_crew_command_is_a_clean_shell_line_with_the_marker():
    for spec in crew_specs().values():
        for hook in spec.hooks:
            command = CMDS.command(spec.adapter, hook)
            assert command.endswith(" # remembra-crew")
            argv = shlex.split(command, comments=True)
            assert argv[-2:] == ["--hook", spec.adapter] or argv[-4:] == ["--hook", spec.adapter, "--agent", spec.adapter]
            assert "KEY" not in command.upper().replace("HOOK", "")  # never a secret in a hook command (S0)


def test_gate_verbs_are_checked():
    with pytest.raises(ValueError):
        CrewHook("PreToolUse", "bogus", "gate")
    with pytest.raises(ValueError):
        CrewHook("SessionStart", "pretool", "cli")


def test_installed_entries_are_protected_by_the_gate(tmp_path):
    """The gate's surgical settings protection (D28, §5.2 row 2) sees every entry we write."""
    home = tmp_path / "home"
    settings = home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    text, _ = _render_claude(json.dumps(USER_SETTINGS))
    settings.write_text(text)
    assert len(gatecore.crew_hook_entries(text)) == 9

    vector = json.loads((Path(__file__).parent / "vectors" / "guard" / "concrete.json").read_text())
    snapshot = vector["snapshot"]
    cwd = snapshot["checkouts"][0]["toplevel"]
    caller = snapshot["checkouts"][0]["session_id"]
    now = snapshot["server_time"]
    pretool = next(g for g in json.loads(text)["hooks"]["PreToolUse"] if g.get("matcher") == MATCHER)["hooks"][0]["command"]

    removing = gatecore.evaluate(
        "Edit",
        {"file_path": str(settings), "old_string": pretool, "new_string": "true"},
        snapshot=snapshot,
        caller=caller,
        cwd=cwd,
        home=str(home),
        now=now,
    )
    assert removing.decision == "deny" and removing.rule == 2

    unrelated = gatecore.evaluate(
        "Edit",
        {"file_path": str(settings), "old_string": '"model": "opus"', "new_string": '"model": "sonnet"'},
        snapshot=snapshot,
        caller=caller,
        cwd=cwd,
        home=str(home),
        now=now,
    )
    assert unrelated.decision != "deny"


# ---------------------------------------------------------------------------
# unverified adapters (observe mode)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", [codex, gemini, qwen])
def test_json_hook_adapters_render_idempotently(module):
    spec = module.CREW
    assert not spec.verified
    relay_start = f"/usr/local/bin/remembra-relay brief --hook {spec.adapter} --agent {spec.adapter}"
    before = json.dumps({"theme": "x", "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": relay_start}]}]}})
    text, summary = render(before, spec, CMDS)
    data = json.loads(text)
    assert data["theme"] == "x"
    events = [h.event for h in spec.hooks]
    assert set(data["hooks"]) == set(events)
    for groups in data["hooks"].values():
        for group in groups:
            assert "matcher" not in group  # unverified tool names: the gate filters
            for hook in group["hooks"]:
                assert "timeout" not in hook  # unverified timeout units
                assert hook["command"].endswith(CREW_MARKER)
    assert "remembra-relay" not in text
    assert render(text, spec, CMDS) == (text, [])
    removed, _ = render(text, spec, CMDS, remove=True)
    assert json.loads(removed) == {"theme": "x"}


def test_cursor_hooks_shape():
    before = json.dumps(
        {
            "version": 1,
            "hooks": {
                "stop": [{"command": "my-stop"}],
                "sessionStart": [{"command": "/x/remembra-relay brief --hook cursor --agent cursor"}],
            },
        }
    )
    text, _ = render(before, cursor.CREW, CMDS)
    data = json.loads(text)
    assert data["version"] == 1
    assert data["hooks"]["stop"] == [{"command": "my-stop"}, {"command": f"{PY_GATE} stop --hook cursor # remembra-crew"}]
    assert data["hooks"]["beforeShellExecution"] == [{"command": f"{PY_GATE} pretool --hook cursor # remembra-crew"}]
    assert data["hooks"]["beforeMCPExecution"] == [{"command": f"{PY_GATE} pretool --hook cursor # remembra-crew"}]
    assert data["hooks"]["sessionStart"] == [
        {"command": "/opt/bin/remembra-crew start --hook cursor --agent cursor # remembra-crew"}
    ]
    assert render(text, cursor.CREW, CMDS) == (text, [])
    removed, _ = render(text, cursor.CREW, CMDS, remove=True)
    assert json.loads(removed) == {"version": 1, "hooks": {"stop": [{"command": "my-stop"}]}}


def test_kimi_block_replaces_the_relay_block():
    relay = (
        '[model]\nname = "k2"\n\n# >>> remembra-relay (managed block) >>>\n[[hooks]]\nevent = "SessionStart"\n'
        'command = "/x/remembra-relay brief --hook kimi --agent kimi"\n# <<< remembra-relay (managed block) <<<\n'
    )
    text, summary = render(relay, kimi.CREW, CMDS)
    assert "remembra-relay" not in text
    assert text.startswith('[model]\nname = "k2"\n')
    assert text.count("[[hooks]]") == len(kimi.CREW.hooks)
    assert f'command = "{PY_GATE} pretool --hook kimi # remembra-crew"' in text
    assert any("relay" in s for s in summary)
    assert render(text, kimi.CREW, CMDS) == (text, [])
    removed, _ = render(text, kimi.CREW, CMDS, remove=True)
    assert removed == '[model]\nname = "k2"\n'
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        return
    parsed = tomllib.loads(text)
    assert [h["event"] for h in parsed["hooks"]] == [h.event for h in kimi.CREW.hooks]


def test_output_modes_follow_the_spec():
    modes = {name: spec.output for name, spec in crew_specs().items()}
    assert modes == {
        "claude-code": "hook-json",
        "codex": "text",
        "cursor": "cursor-json",
        "gemini": "hook-json",
        "qwen": "hook-json",
        "kimi": "text",
    }
    assert [n for n, s in crew_specs().items() if s.verified] == ["claude-code"]


# ---------------------------------------------------------------------------
# AGENTS.md
# ---------------------------------------------------------------------------


def test_agents_md_crew_block(tmp_path):
    path = tmp_path / "AGENTS.md"
    path.write_text("# Project\n\nOwn text.\n\n" + agents_md.block("/r/remembra-relay") + "\nTail.\n")
    change = agents_md.plan(path, "/r/remembra-relay", "/c/remembra-crew")
    assert change.changed
    after = change.after
    assert after.startswith("# Project\n\nOwn text.\n\n") and after.rstrip().endswith("Tail.")
    inner = after.split(agents_md.BEGIN, 1)[1].split(agents_md.END, 1)[0]
    assert "## Crew mode (Remembra)" in inner
    assert "`/c/remembra-crew status`" in inner and "`/c/remembra-crew adopt <T-n>`" in inner
    assert "`/r/remembra-relay close`" in inner
    assert not S.BYPASS_MENTION_RE.search(after)  # D34: never in agent-visible text
    assert after.count(agents_md.BEGIN) == 1
    path.write_text(after)
    assert not agents_md.plan(path, "/r/remembra-relay", "/c/remembra-crew").changed
    # uninstall: back to the relay-only section
    back = agents_md.plan(path, "/r/remembra-relay", None)
    assert "Crew mode" not in back.after and agents_md.BEGIN in back.after


def test_relay_connect_leaves_crew_entries_alone():
    """`remembra-relay connect` must not treat crew entries as its own, even with the module fallback command."""
    cmds = CrewCommands(python="/usr/bin/python3", gate="/g/crew-gate.py", crew="/usr/bin/python3 -m remembra.relay.crew.cli")
    text, _ = render(None, claude_code.CREW, cmds)
    relay_text, relay_summary = claude_code.ADAPTER.render(text, "/usr/local/bin/remembra-relay")
    data = json.loads(relay_text)
    crew_starts = [g for g in data["hooks"]["SessionStart"] if CREW_MARKER in g["hooks"][0]["command"]]
    assert crew_starts == json.loads(text)["hooks"]["SessionStart"]
    assert not any("replace outdated relay hook" in line for line in relay_summary)
