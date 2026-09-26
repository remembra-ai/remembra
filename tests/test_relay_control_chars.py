"""CLI-02: terminal control characters in agent-written text never reach a terminal.

A writer to the account (a prompt-injected agent calling close_session, or a
leaked write key) could plant OSC 52 (clipboard overwrite), OSC 8 (disguised
links), CSI sequences (erase the "[contains a command or URL…]" warning) or a
bare carriage return in handoff fields, inbox messages or memories. They are
removed when the text is written and again before the relay prints anything,
so rows stored before this fix are safe too. A branch name must also be a
valid git branch name (no leading "-", no "..", no control characters).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from remembra.relay.handoff import redact, show_text
from remembra.security.untrusted import strip_controls, strip_hidden
from tests.agent_api_harness import row, seed
from tests.test_relay_cli_inprocess import _run, api, wired  # noqa: F401 - fixtures

ESC, BEL, CSI_C1 = "\x1b", "\x07", "\x9b"
OSC52 = f"{ESC}]52;c;cm0gLXJmIH4K{BEL}"  # clipboard := "rm -rf ~"
OSC8 = f"{ESC}]8;;https://evil.example/x{BEL}docs{ESC}]8;;{BEL}"  # a link whose target is hidden
ERASE = f"{ESC}[2K{ESC}[1A"  # erase line, cursor up: hides the line printed before
COLOR = f"{ESC}[31m"
BAD = (ESC, BEL, CSI_C1, "\r", "\x00", "\x9d")


def _clean(text: str) -> bool:
    return not any(c in text for c in BAD)


def test_strip_controls_removes_sequences_and_keeps_layout():
    text = f"a{OSC52}b {OSC8} c{ERASE}d{COLOR}red{ESC}[0m\te\nf{CSI_C1}31mg\x00h\rline1\r\nline2{ESC}(Bi{ESC}P1$r{ESC}\\j"
    out = strip_controls(text)
    assert _clean(out), repr(out)
    assert out == "ab docs cdred\te\nfghline1\nline2ij"
    assert strip_controls("plain\ttext\nkept") == "plain\ttext\nkept"
    assert strip_controls("") == "" and strip_controls(None) is None  # type: ignore[arg-type]
    visible, kinds, _ = strip_hidden(f"ok{OSC52}")
    assert visible == "ok" and "control" in kinds
    assert show_text(f"next {ERASE}step").endswith("[hidden characters removed]")
    # CRLF line endings are layout, not hidden text.
    assert strip_hidden("a\r\nb") == ("a\nb", [], "")


def test_handoff_redact_strips_controls_from_every_string():
    counts: dict[str, int] = {}
    out = redact({"notes": f"n{OSC52}", "commits": [{"subject": f"fix{COLOR} red"}], f"k{BEL}": "v"}, counts)
    assert out == {"notes": "n", "commits": [{"subject": "fix red"}], "k": "v"}


def _facts(**over):
    facts = {
        "branch": "feat/x",
        "head_commit": "a" * 40,
        "notes": f"note {ERASE} hidden",
        "next_step": f"next {OSC52} step",
        "commits": [{"sha": "b" * 40, "subject": f"fix{COLOR} red {OSC8}"}],
        "commands": [{"cmd": f"make{ESC}[8m deploy", "exit_code": 2}],
        "errors": [f"boom\rsafe-looking {CSI_C1}0m"],
        "todos_open": [f"todo {OSC52}"],
    }
    facts.update(over)
    return facts


def test_close_stores_and_serves_no_control_characters(api):  # noqa: F811
    body = {"agent_id": "claude-code", "session_id": "s-esc", "project_id": "esc", "facts": _facts()}
    res = api["http"].post("/api/v1/session/close", json=body, headers={})
    assert res.status_code == 200, res.text
    out = res.json()
    assert _clean(out["rendered"]) and "note" in out["rendered"] and "next" in out["rendered"]
    stored = row(api, out["handoff_id"])
    assert _clean(stored["content"]) and _clean(json.dumps(json.loads(stored["metadata"]), ensure_ascii=False))
    trail = api["http"].get("/api/v1/trail", params={"project": "esc"}).json()
    assert _clean(json.dumps(trail, ensure_ascii=False))
    brief = api["http"].get("/api/v1/session/brief", params={"project_id": "esc", "agent_id": "codex"}).json()
    assert _clean(json.dumps(brief, ensure_ascii=False))


@pytest.mark.parametrize(
    "branch",
    ["-fCmain", "--detach", "feat/../main", f"feat{ESC}]52;c;eA=={BEL}", "feat\nmain", "a b", "x~1", "y^", "z:w", "q?", "*"],
)
def test_close_rejects_a_branch_git_would_refuse(api, branch):  # noqa: F811
    body = {"agent_id": "claude-code", "session_id": "s-branch", "project_id": "esc", "facts": _facts(branch=branch)}
    res = api["http"].post("/api/v1/session/close", json=body)
    assert res.status_code == 422, (branch, res.text)


@pytest.mark.parametrize("branch", ["main", "feat/rounding", "release-1.2", "(detached)", "user@host/topic", "HEAD", None])
def test_close_accepts_real_branch_names(api, branch):  # noqa: F811
    body = {"agent_id": "claude-code", "session_id": "s-ok", "project_id": "esc", "facts": _facts(branch=branch)}
    res = api["http"].post("/api/v1/session/close", json=body)
    assert res.status_code == 200, (branch, res.text)


def test_inbox_and_memory_writes_drop_control_characters(api):  # noqa: F811
    codex = api["make_client"](project="esc", agent_id="codex")
    sent = codex.send_to_inbox(to_agent="claude-code", subject=f"deploy{ERASE}", body=f"run {OSC52} now\rok")
    stored = api["http"].get("/api/v1/inbox", params={"agent_id": "claude-code", "status": "all"}).json()
    item = next(i for i in stored if i["inbox_id"] == sent["inbox_id"])
    assert _clean(item["subject"] + item["body"]) and item["subject"] == "deploy"
    res = api["http"].post("/api/v1/memories", json={"content": f"note{CSI_C1}31m red{OSC8}", "project_id": "esc"})
    assert res.status_code == 201, res.text
    assert _clean(row(api, res.json()["id"])["content"])


def test_cli_trail_and_brief_print_no_control_characters_even_for_old_rows(wired, monkeypatch, capsys, tmp_path):  # noqa: F811
    app = wired["api"]
    # A handoff stored before this fix: raw sequences in the text and the relay block.
    relay = {
        "agent_id": "claude-code",
        "branch": f"feat{OSC8}",
        "head_commit": "c" * 40,
        "headline": f"shipped {ERASE}{OSC52}",
        "failing": [f"`make` exited 2 {COLOR}"],
        "not_done": [],
        "next": f"next{BEL}",
        "closed_at": datetime.now(UTC).isoformat(),
    }
    seed(
        app,
        "h-old",
        f"[HANDOFF] claude-code · feat{OSC8}\nDone: shipped {ERASE}{OSC52}\nNext step: next{BEL}",
        datetime.now(UTC).replace(tzinfo=None),
        project_id="esc",
        memory_type="handoff",
        metadata={"agent_id": "claude-code", "session_id": "old", "relay": relay},
    )
    app["make_client"](project="esc", agent_id="claude-code").store_status("deploy", f"live{ERASE}")
    for argv in (
        ["trail", "--project", "esc"],
        ["brief", "--agent", "codex", "--project", "esc"],
        ["brief", "--agent", "codex", "--project", "esc", "--format", "hook-json"],
        ["brief", "--agent", "codex", "--project", "esc", "--format", "json"],
    ):
        code, out, err = _run(monkeypatch, capsys, argv)
        assert code == 0, err
        assert _clean(out), (argv, [c for c in BAD if c in out])
        if argv[-1] == "hook-json":
            context = json.loads(out.strip().splitlines()[-1])["hookSpecificOutput"]["additionalContext"]
            assert _clean(context) and "shipped" in context
