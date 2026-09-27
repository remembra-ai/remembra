"""Marshal on the web (M1) says what the code does.

The dashboard's "why?" slips (dashboard/src/lib/marshal.ts) are rules over
data the dashboard reads; the facts they state about this machine's side
(which agents close in the background and where that logs, how many hooks
Codex must trust, the doctor command lines) are written in TypeScript. These
tests hold that text to the Python it describes, and hold the shared verdict
fixture to the verdict table.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from remembra.marshal import words
from remembra.marshal.rules import RULE_IDS
from remembra.relay.adapters import REGISTRY
from remembra.relay.cli import _close_log_path

ROOT = Path(__file__).resolve().parent.parent
AGENTS_TS = (ROOT / "dashboard" / "src" / "lib" / "agents.ts").read_text()
MARSHAL_TS = (ROOT / "dashboard" / "src" / "lib" / "marshal.ts").read_text()
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "marshal" / "diagnosis_cases.json").read_text())


def _ts_const(name: str) -> str:
    m = re.search(rf"export const {name} =\s*\n?\s*(['\"])(.*?)\1;", MARSHAL_TS, re.S)
    assert m, name
    return m.group(2).replace("\\'", "'")


def test_the_agents_that_close_in_the_background_are_the_adapters_that_detach() -> None:
    detached_ts = set(re.findall(r"'?([\w-]+)'?: \{[^}]*detachClose: true", AGENTS_TS))
    detached_py = {name for name, adapter in REGISTRY.items() if adapter.spec.detach_close}
    assert detached_ts == detached_py == {"codex", "cursor", "gemini", "qwen"}


def test_the_detached_close_log_is_where_the_relay_writes_it() -> None:
    assert _ts_const("DETACHED_CLOSE_LOG") == "~/" + _close_log_path(Path("~")).relative_to("~").as_posix()


def test_codex_trust_names_the_hooks_connect_writes() -> None:
    spec = REGISTRY["codex"].spec
    events = [spec.start_event, spec.prompt_event, spec.end_event]
    assert all(events) and len(events) == 3
    assert list(words.CODEX_TRUST_EVENTS) == events
    assert words.codex_trust_call() == "Codex needs you to trust 3 hooks: Codex Settings > Hooks > Trust."
    assert words.codex_trust_fix().endswith(f"and trust {events[0]}, {events[1]} and {events[2]}.")
    # The slip builds both lines from the generated words, not from its own copy.
    assert "SHARED_RULES.CODEX_TRUST_MISSING.what" in MARSHAL_TS and "SHARED_RULES.CODEX_TRUST_MISSING.fix" in MARSHAL_TS
    assert "/hooks" in spec.setup_note and "three remembra-relay hooks" in spec.setup_note


def test_every_verdict_is_a_doctor_rule_or_one_only_a_slip_reaches() -> None:
    assert "export type VerdictCode = SharedRule | (typeof DASHBOARD_ONLY)[number];" in MARSHAL_TS
    codes = {c["expect"]["code"] for c in FIXTURE["cases"]}
    assert codes == set(words.SHARED_RULES) | set(words.DASHBOARD_ONLY)
    # A shared verdict is one of the doctor's own rule ids; a slip-only one never is.
    assert set(words.SHARED_RULES) <= set(RULE_IDS)
    assert not set(words.DASHBOARD_ONLY) & set(RULE_IDS)


def test_the_fixture_only_uses_agents_the_relay_can_connect() -> None:
    for case in FIXTURE["cases"]:
        assert case["input"]["agent_id"] in REGISTRY, case["name"]


def test_doctor_lines_pin_the_release_that_has_doctor() -> None:
    release = re.search(r"export const DOCTOR_RELEASE = '([\d.]+)';", AGENTS_TS)
    assert release and release.group(1) == "0.16.1"
    assert "return `pipx run --spec 'remembra>=${DOCTOR_RELEASE}' ${doctorCommand(agentId)}`;" in AGENTS_TS
    assert "`remembra-relay doctor --agent ${adapter}`" in AGENTS_TS
    for case in FIXTURE["cases"]:
        for cmd in case["expect"].get("commands", []):
            if "pipx run" in cmd:
                assert f"'remembra>={release.group(1)}'" in cmd


def test_the_slip_links_headings_the_relay_guide_has() -> None:
    guide = (ROOT / "docs" / "guides" / "relay.md").read_text()
    assert _ts_const("RELAY_GUIDE") == "https://docs.remembra.dev/guides/relay/"
    anchors = {"#setup"}  # mkdocs' toc makes "## Setup" #setup; the others are set with {#...}
    anchors |= {"#" + a for a in re.findall(r"^#{2,3} .*\{#([a-z-]+)\}$", guide, re.M)}
    assert "\n## Setup\n" in guide
    for rule, parts in words.SHARED_RULES.items():
        assert parts["doc"] == "" or parts["doc"] in anchors, rule


def test_the_slip_footer_says_no_model_wrote_it() -> None:
    assert _ts_const("SLIP_FOOTER") == "Built by rules from your keys and trail. No model wrote this."
    assert "openai" not in MARSHAL_TS.lower() and "fetch(" not in MARSHAL_TS  # reads go through the API client only
