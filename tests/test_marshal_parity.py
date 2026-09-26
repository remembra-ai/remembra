"""Marshal says one thing everywhere: the doctor, the dashboard's "why?" slips, remembra_setup and setup.md.

Two halves were built apart and are held together here:

* **Rules.** Where the doctor (``remembra-relay doctor``, ``remembra_doctor``) and a slip
  (``dashboard/src/lib/marshal.ts``) reach the same fault, they use the same rule id, sentence and
  page. The doctor runs on the slip's own verdict fixture (``tests/fixtures/marshal/diagnosis_cases.json``,
  which vitest runs through ``diagnoseAgent``) and must reach the same verdict in the same words. The
  dashboard gets the words from ``marshalWords.ts``, generated from ``remembra.marshal.words``.
* **Setup.** remembra.dev/setup.md (what the user's agent follows), the MCP tool ``remembra_setup``
  (through the real stdio server) and the dashboard's command catalog (``dashboard/src/lib/agents.ts``,
  run by Node when it can strip types, else read as source) give the same commands in the same order.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from remembra.marshal import commands, doctor, tools, words
from remembra.marshal.rules import RULE_IDS, Finding
from remembra.relay.adapters import REGISTRY
from tests.marshal_fixtures import NOW, FakeHome, FakeTrail

ROOT = Path(__file__).resolve().parents[1]
AGENTS_TS = ROOT / "dashboard" / "src" / "lib" / "agents.ts"
WORDS_TS = ROOT / "dashboard" / "src" / "lib" / "marshalWords.ts"
SETUP_MD = ROOT / "landing" / "setup.md"
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "marshal" / "diagnosis_cases.json").read_text())
CATALOG_JS = ROOT / "tests" / "js" / "agents_catalog.mjs"
CLOUD = "https://api.remembra.dev"


# ---------------------------------------------------------------------------
# The dashboard's catalog: agents.ts run by Node, or read as source
# ---------------------------------------------------------------------------


def _node_strips_types() -> str | None:
    node = shutil.which("node")
    if node is None:
        return None
    probe = subprocess.run([node, "--experimental-strip-types", "--no-warnings", "-e", ""], capture_output=True, timeout=30)
    return node if probe.returncode == 0 else None


NODE = _node_strips_types()
needs_node = pytest.mark.skipif(NODE is None, reason="needs Node 22.6+ (type stripping) to run agents.ts")


def _catalog() -> dict[str, Any]:
    assert NODE is not None
    out = subprocess.run(
        [NODE, "--experimental-strip-types", "--no-warnings", str(CATALOG_JS), str(AGENTS_TS), CLOUD],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def _ts_block(name: str) -> str:
    text = AGENTS_TS.read_text()
    return text.split(f"const {name}", 1)[1].split("};", 1)[0]


# ---------------------------------------------------------------------------
# setup.md, section by section
# ---------------------------------------------------------------------------


def _sections() -> dict[str, str]:
    """``{"4": body, ..., "The same steps in the user's terminal": body}`` keyed by step number or title."""
    out: dict[str, str] = {}
    for chunk in SETUP_MD.read_text().split("\n## ")[1:]:
        title, _, body = chunk.partition("\n")
        step = re.match(r"(\d+)\. ", title)
        out[step.group(1) if step else title.strip()] = body
    return out


def _bash(body: str) -> list[list[str]]:
    return [[ln.strip() for ln in block.splitlines() if ln.strip()] for block in re.findall(r"```bash\n(.*?)```", body, re.S)]


def _prose(body: str) -> str:
    return " ".join(re.sub(r"```.*?```", "", body, flags=re.S).replace("`", "").split())


# The read-only look around in step 1: not setup commands, and in no catalog.
LOOK = ("uname -s", 'echo "$SHELL"', "command -v claude codex cursor-agent cursor gemini qwen kimi pipx")
OS_BULLETS = {"macOS": "macos", "Debian or Ubuntu": "linux-apt", "Fedora": "linux-dnf", "Anything else": "other"}


def _pipx_bullets() -> dict[str, str]:
    found = dict(re.findall(r"^- ([^:\n]+): `([^`]+)`$", _sections()["2"], re.M))
    return {OS_BULLETS[label]: command for label, command in found.items()}


# ---------------------------------------------------------------------------
# remembra_setup through the real MCP server
# ---------------------------------------------------------------------------


def _fresh_machine(tmp_path: Path) -> FakeHome:
    """No pipx, no remembra, no key; Claude Code, Codex and Gemini CLI installed."""
    fh = FakeHome(tmp_path)
    fh.install("claude", "codex", "gemini")
    return fh


def _setup_over_stdio(fh: FakeHome) -> dict[str, Any]:
    pytest.importorskip("mcp")
    from tests.test_mcp_marshal_tools import _stdio, _text

    out = _stdio(fh, [("tools/call", {"name": "remembra_setup", "arguments": {}})], {"PATH": str(fh.bin), "SHELL": "/bin/zsh"})
    payload = _text(out[2])
    assert payload["status"] == "ok" and payload["changed_nothing"] is True
    return payload


def test_setup_md_remembra_setup_and_the_dashboard_catalog_give_the_same_commands(tmp_path: Path) -> None:
    """The three agree, step for step, on a new machine with Claude Code, Codex and Gemini CLI."""
    payload = _setup_over_stdio(_fresh_machine(tmp_path))
    steps = payload["steps"]
    assert payload["agents"] == ["claude-code", "codex", "gemini"]
    sections = _sections()
    catalog = _catalog() if NODE else None

    # Each remembra_setup step lands in the setup.md section that says the same thing, in the same order.
    where: list[int] = []
    for step in steps:
        if step["command"]:
            n = next((k for k, body in sections.items() if k.isdigit() and [step["command"]] in _bash(body)), None)
            if n is None and step["command"] in _pipx_bullets().values():
                n = "2"
            assert n is not None, f"setup.md has no step running {step['command']!r}"
        elif step["runs_where"] == "codex_ui":
            n = "8"
            assert step["action"] in _prose(sections[n]) and step["note"] in _prose(sections[n])
        elif step["stop"]:
            n = "3"
            assert commands.SIGNUP_URL in step["action"] and commands.SIGNUP_URL in sections[n]
        else:
            n = "9"
            assert step["title"] == "Restart your agents" and "restart" in _prose(sections[n]).lower()
        where.append(int(n))
    assert where == sorted(where), list(zip([s["title"] for s in steps], where, strict=True))
    assert payload["os"] in _pipx_bullets() or payload["os"] == "linux"
    assert steps[0]["command"] == _pipx_bullets()[payload["os"] if payload["os"] != "linux" else "other"]

    # And setup.md runs nothing else: every command in steps 2 to 10 is one remembra_setup gives, or the
    # doctor for an install that predates it.
    given = {s["command"] for s in steps if s["command"]}
    for n in map(str, range(2, 11)):
        for block in _bash(sections[n]):
            for line in block:
                assert line in given, f"step {n}: {line!r} is not in remembra_setup's output"
    assert commands.pipx_run_doctor() in _prose(sections["10"])
    assert all(line in LOOK for block in _bash(sections["1"]) for line in block)

    # The dashboard's catalog: the same strings, from the TypeScript the dashboard runs.
    first_run = _bash(sections["The same steps in the user's terminal"])
    uninstall = [line for block in _bash(sections["Taking it off again"]) for line in block]
    by_title = {s["title"]: s["command"] for s in steps}
    expected = {
        "install": (by_title["Install remembra (with the MCP server)"], _bash(sections["4"])[0][0]),
        "key": (by_title["Save the key and add the Remembra MCP server"], _bash(sections["5"])[0][0]),
        "apply": (by_title["Write the hooks"], _bash(sections["7"])[0][0]),
        "unverified": (by_title["Only if you want them: Gemini CLI's hooks"], _bash(sections["7"])[1][0]),
        "doctor": (by_title["Check"], _bash(sections["10"])[0][0]),
    }
    for name, (from_setup, from_md) in expected.items():
        assert from_setup == from_md, name
    assert first_run == [[expected["install"][0], expected["key"][0], expected["apply"][0]]]
    if catalog is not None:
        assert catalog["PIPX_INSTALL"] == expected["install"][0]
        assert catalog["saveKeyCommand"][""] == expected["key"][0]  # no server named: the machine's own
        assert catalog["oneLineInstall"][""].split(" && ") == first_run[0]
        assert catalog["agentConnectCommand"]["gemini"] == expected["unverified"][0]
        assert catalog["doctorCommand"][""] == expected["doctor"][0]
        assert catalog["pipxRunDoctorCommand"][""] == commands.pipx_run_doctor()
        assert [s["command"] for s in catalog["UNINSTALL_STEPS"]] == uninstall
        assert catalog["CONNECTABLE_AGENTS"] == list(REGISTRY)
    # Without Node, the Python templates the parity test ties to agents.ts' source stand in.
    assert commands.one_line_install("").split(" && ") == first_run[0]
    assert commands.agent_connect("gemini") == expected["unverified"][0]
    assert [c for c, _ in commands.UNINSTALL_STEPS] == uninstall


@pytest.mark.parametrize("os_id", sorted(OS_BULLETS.values()))
def test_the_pipx_line_for_each_os_is_setup_mds(tmp_path: Path, os_id: str) -> None:
    fh = _fresh_machine(tmp_path)
    payload = tools.setup_payload(None, environ=fh.environ(), home=fh.home, which=fh.which, os_id=os_id, shell="bash")
    assert payload["steps"][0]["command"] == _pipx_bullets()[os_id] == commands.PIPX_BOOTSTRAP[os_id]


def test_a_known_server_gets_its_url_everywhere(tmp_path: Path) -> None:
    """A self-hosted machine: remembra_setup names its server, as the dashboard names its own; setup.md says to."""
    fh = _fresh_machine(tmp_path)
    url = "https://memory.example.org"
    payload = tools.setup_payload(None, environ=fh.environ(REMEMBRA_URL=url), home=fh.home, which=fh.which, os_id="macos")
    key = next(s for s in payload["steps"] if s["runs_where"] == "user_terminal")
    assert key["command"] == commands.save_key_command(url) == f"remembra-install --all --url {url}"
    if NODE:
        assert _catalog()["saveKeyCommand"][CLOUD] == commands.save_key_command(CLOUD)
    assert "the user adds `--url` and their server's URL" in _sections()["5"]


@needs_node
def test_the_catalog_names_and_aliases_are_the_doctors() -> None:
    catalog = _catalog()
    assert catalog["names"] == {a: words.agent_name(a) for a in REGISTRY}
    assert catalog["verified"] == {a: REGISTRY[a].spec.verified for a in REGISTRY}
    assert catalog["agentConnectCommand"] == {a: commands.agent_connect(a) for a in REGISTRY}
    assert catalog["doctorCommand"] == {"": commands.doctor(), **{a: commands.doctor(a) for a in REGISTRY}}
    pipx_run = {"": commands.pipx_run_doctor(), **{a: commands.pipx_run_doctor(a) for a in REGISTRY}}
    assert catalog["pipxRunDoctorCommand"] == pipx_run


def test_names_and_aliases_match_agents_ts_source() -> None:
    known = dict(re.findall(r"^\s+'?([\w-]+)'?: \{ name: '([^']+)'", _ts_block("KNOWN"), re.M))
    assert {a: known[a] for a in REGISTRY} == {a: words.agent_name(a) for a in REGISTRY}
    aliases = dict(re.findall(r"^\s+'?([\w -]+?)'?: '([\w-]+)',", _ts_block("ALIASES"), re.M))
    assert aliases == words.AGENT_ALIASES


# ---------------------------------------------------------------------------
# Rules: the doctor on the slip's own fixture
# ---------------------------------------------------------------------------


def test_the_dashboard_words_are_generated_from_the_doctors() -> None:
    sys.path.insert(0, str(ROOT / "scripts"))
    try:
        import sync_marshal_words
    finally:
        sys.path.pop(0)
    assert WORDS_TS.read_text() == sync_marshal_words.render(), "run python scripts/sync_marshal_words.py"
    check = subprocess.run([sys.executable, "scripts/sync_marshal_words.py", "--check"], cwd=ROOT, capture_output=True, text=True)
    assert check.returncode == 0, check.stderr
    assert set(words.SHARED_RULES) <= set(RULE_IDS)
    assert not set(words.DASHBOARD_ONLY) & set(RULE_IDS)


def _summary(items: list[dict[str, Any]], given: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """The summary the server would report for these entries (7 days), with the fixture's own row when it has one."""
    out: dict[str, dict[str, Any]] = {}
    for item in items:
        agent = words.canonical_agent(item["agent_id"])
        row = out.setdefault(agent, {"handoffs": 0, "checkpoints": 0, "last_active": None})
        row["handoffs" if item["memory_type"] == "handoff" else "checkpoints"] += 1
        row["last_active"] = max(row["last_active"] or "", item["created_at"])
    if given:
        out[words.canonical_agent(given["agent_id"])] = {k: given[k] for k in ("handoffs", "checkpoints", "last_active")}
    for row in out.values():
        row["sessions_7d"] = row["handoffs"]
        row["daily"] = [0] * 6 + [row["handoffs"] + row["checkpoints"]]
    return out


def _doctor_on(case: dict[str, Any], tmp_path: Path) -> list[Finding]:
    """This machine as the slip assumes it: the agent's hooks written (Codex's trusted unless the case is about
    trust), the key saved when the account has one in use; the trail and summary the fixture shows."""
    given = case["input"]
    agent = given["agent_id"]
    fh = FakeHome(tmp_path)
    if any(k.get("active") is not False for k in given["keys"]):
        fh.credentials()
    fh.hooks(agent)
    if agent == "codex" and case["expect"]["code"] != "CODEX_TRUST_MISSING":
        fh.trust_codex()
    items = given["trail"] + [i for i in given["agent_trail"] if i["id"] not in {t["id"] for t in given["trail"]}]
    trail = FakeTrail(
        agents=_summary(items, given.get("summary_agent")), items=given["trail"], agent_items={agent: given["agent_trail"]}
    )
    report = doctor.run(fh.home, [agent], True, environ=fh.environ(), transport=trail.transport, which=fh.which, now=NOW)
    assert all(r.method == "GET" for r in trail.requests)
    return report.findings


@pytest.mark.parametrize("case", FIXTURE["cases"], ids=[c["name"] for c in FIXTURE["cases"]])
def test_the_doctor_reaches_the_slips_verdict_in_the_same_words(case: dict[str, Any], tmp_path: Path) -> None:
    want = case["expect"]
    code = want["code"]
    agent = case["input"]["agent_id"]
    findings = _doctor_on(case, tmp_path)
    if code in words.DASHBOARD_ONLY:
        if code == "HANDED_OFF":  # nothing wrong: the doctor has nothing to say about the agent either
            assert not [f for f in findings if f.agent == agent and f.actionable], findings
        return
    found = [f for f in findings if f.id == code and f.agent in (agent, None)]
    assert len(found) == 1, [(f.id, f.agent, f.what) for f in findings]
    f = found[0]
    rule = words.SHARED_RULES[code]
    assert "https://" + (f.doc or "") == want["doc"] == "https://docs.remembra.dev/guides/relay/" + rule["doc"]
    # The same fault from the other side: proven the same way, except Codex trust, which only the doctor
    # can see in ~/.codex/config.toml (the slip infers it from silence).
    if code == "CODEX_TRUST_MISSING":
        assert (f.inferred, want["proven"]) == (False, False)
    else:
        assert (not f.inferred) == want["proven"]
    if code == "KEY_MISSING":
        # One template, two places: this machine and the account.
        assert f.what == words.say(code, "what", where="on this machine")
        assert want.get("verdict", words.say(code, "what", where="active on your account")) == words.say(
            code, "what", where="active on your account"
        )
        return
    verdict = want.get("verdict")
    if verdict is not None:
        assert f.what == verdict or f.what.startswith(verdict + " "), (f.what, verdict)
        detail = want.get("detail")
        if "detail" in rule and detail:
            assert detail.startswith(words.say(code, "detail", ago="3h ago") if code == "STALE_CHECKPOINT" else rule["detail"])
            assert f.what == f"{verdict} {detail}" or f.what == f"{verdict} {rule['detail']}"
    if "fix" in rule and want.get("fix") and not want.get("unverified"):
        assert f.fix is not None and f.fix.text.startswith(want["fix"]), (f.fix, want["fix"])
