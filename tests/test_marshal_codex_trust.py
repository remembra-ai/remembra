"""Codex hook trust from files only, checked against what Codex itself reports.

``tests/fixtures/marshal/codex_trust/recorded.json`` is ``codex app-server``
``hooks/list`` output for ``hooks.json`` in that folder, recorded in a temp
CODEX_HOME (no credentials, no model) with codex-cli 0.155.0-alpha.16.4 and
0.157.1: once with an empty ``config.toml``, once with ``config_mixed.toml``.
The hash Marshal computes must equal Codex's ``currentHash`` for every hook,
and the trust status must equal Codex's ``trustStatus`` (``enabled = false``
is reported as disabled). When a Codex binary is on this machine the same
comparison runs live, in a temp CODEX_HOME.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from remembra.marshal import codex_hooks
from remembra.relay.adapters import REGISTRY

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "marshal" / "codex_trust"
HOOKS_TEXT = (FIXTURES / "hooks.json").read_text()
RECORDED = json.loads((FIXTURES / "recorded.json").read_text())


def _hooks() -> list[tuple[str, str, int, int, dict]]:
    data = json.loads(HOOKS_TEXT)
    out = []
    for event, groups in data["hooks"].items():
        for g, group in enumerate(groups):
            for i, hook in enumerate(group["hooks"]):
                out.append((event, group.get("matcher"), g, i, hook))
    return out


@pytest.mark.parametrize("version", sorted(RECORDED["versions"]))
def test_hash_equals_codex_current_hash(version: str) -> None:
    recorded = RECORDED["versions"][version]["untrusted_config"]
    assert len(recorded) == len(_hooks()) == 6
    for event, matcher, g, i, hook in _hooks():
        key = f"{codex_hooks.EVENT_KEYS[event]}:{g}:{i}"
        assert codex_hooks.hook_hash(event, matcher, hook) == recorded[key]["currentHash"], (version, key)


@pytest.mark.parametrize("version", sorted(RECORDED["versions"]))
def test_trust_status_equals_codex_trust_status(version: str) -> None:
    hooks_path = Path("/home/dev/.codex/hooks.json")
    config = (FIXTURES / "config_mixed.toml").read_text().replace("@HOOKS@", str(hooks_path))
    trust = codex_hooks.trust_for(hooks_path, HOOKS_TEXT, hooks_path.with_name("config.toml"), config)
    recorded = RECORDED["versions"][version]["mixed_config"]
    assert trust.state_entries == 4  # the record for another hooks.json is not ours
    mine = {t.hook.key_suffix: t for t in trust.per_hook}
    # Only remembra-relay hooks are ours (PreToolUse "echo pre" is someone else's).
    assert set(mine) == {k for k in recorded if not k.startswith("pre_tool_use")}
    for key, item in mine.items():
        codex = recorded[key]
        expected = codex_hooks.DISABLED if not codex["enabled"] else codex["trustStatus"]
        assert item.status == expected, (version, key, item.status, codex)
    assert mine["user_prompt_submit:0:0"].proven is False  # a different hash is reported, not asserted
    assert mine["session_start:0:0"].proven and mine["session_start:1:0"].proven


def test_the_hooks_connect_writes_are_three_and_hash_cleanly(tmp_path: Path) -> None:
    text, _ = REGISTRY["codex"].render(None, "/opt/remembra/bin/remembra-relay")
    readable, hooks = codex_hooks.relay_hooks_in(text)
    assert readable and [h.event for h in hooks] == ["SessionStart", "UserPromptSubmit", "SessionEnd"]
    assert all(h.certain and h.hash and h.hash.startswith("sha256:") for h in hooks)


def test_unreadable_or_missing_config() -> None:
    path = Path("/home/dev/.codex/hooks.json")
    missing = codex_hooks.trust_for(path, HOOKS_TEXT, path.with_name("config.toml"), None)
    assert missing.config_state == "missing" and {t.status for t in missing.per_hook} == {codex_hooks.UNTRUSTED}
    broken = codex_hooks.trust_for(path, HOOKS_TEXT, path.with_name("config.toml"), "[hooks.state\n")
    assert broken.config_state == "unreadable" and {t.status for t in broken.per_hook} == {codex_hooks.UNCHECKED}
    assert not broken.all_trusted
    denied = codex_hooks.trust_for(path, HOOKS_TEXT, path.with_name("config.toml"), None, "PermissionError")
    assert denied.config_state == "unreadable" and not denied.all_trusted
    garbage = codex_hooks.relay_hooks_in("{not json")
    assert garbage == (False, ())


def test_a_hook_with_fields_this_check_does_not_model_is_never_called_stale() -> None:
    text = json.dumps(
        {
            "hooks": {
                "SessionStart": [
                    {
                        "hooks": [
                            {"type": "command", "command": "remembra-relay brief --hook codex --agent codex", "futureField": 1}
                        ]
                    }
                ]
            }
        }
    )
    path = Path("/home/dev/.codex/hooks.json")
    config = f'[hooks.state."{path}:session_start:0:0"]\ntrusted_hash = "sha256:{"a" * 64}"\n'
    trust = codex_hooks.trust_for(path, text, path.with_name("config.toml"), config)
    assert [t.status for t in trust.per_hook] == [codex_hooks.UNCHECKED]


def test_live_codex_agrees_when_it_is_installed(tmp_path: Path) -> None:
    """Runs ``codex app-server`` ``hooks/list`` in a temp CODEX_HOME; skipped without a Codex binary."""
    from tests.codex_live_harness import find_codex, list_hooks

    found = find_codex()
    if found is None:
        pytest.skip("no codex binary on this machine")
    codex, _version = found
    home = tmp_path / "home"
    codex_home = home / ".codex"
    codex_home.mkdir(parents=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    (codex_home / "hooks.json").write_text(HOOKS_TEXT)
    (codex_home / "config.toml").write_text("")
    env = {"HOME": str(home), "CODEX_HOME": str(codex_home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    try:
        listed = list_hooks(codex, env, repo, timeout=60)
    except Exception as e:  # a codex that can't start here is not this check's subject
        pytest.skip(f"codex app-server did not answer: {e.__class__.__name__}")
    for event, matcher, g, i, hook in _hooks():
        key = f"{codex_hooks.EVENT_KEYS[event]}:{g}:{i}"
        match = [h for h in listed if h["key"].endswith(":" + key)]
        assert match and match[0]["currentHash"] == codex_hooks.hook_hash(event, matcher, hook), key
