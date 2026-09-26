"""CLI-01: credentials typed on a command line never reach a stored handoff.

A Claude Code transcript whose failed commands, test command, error output and
todo carry fake credentials goes through the real relay facts parser and the
real ``remembra-relay close`` into the production routes (PII detector off and
on). The close payload, the stored row, ``/trail``, ``/session/brief`` and the
CLI's own trail and brief output are then searched for every secret, including
tokens that straddle the parser's clip points (160, 200 and 300 characters),
where a clipped prefix used to survive because text was clipped before it was
scrubbed. Every value is synthetic and assembled at runtime.
"""

from __future__ import annotations

import base64
import json
import secrets
import string

import pytest

from remembra.relay import facts as factlib
from remembra.security.pii_detector import PIIDetector
from tests.agent_api_harness import row
from tests.relay_fixtures import Transcript, commit, git, make_remote_and_clones
from tests.test_relay_cli_inprocess import _run, api, wired  # noqa: F401 - fixtures


def _rand(n: int) -> str:
    body = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(n - 3))
    return body + "aZ7"


def _hex(n: int) -> str:
    return "".join(secrets.choice("0123456789abcdef") for _ in range(n))


def _plant() -> tuple[dict[str, str], list[tuple[str, int, str]]]:
    """``(secrets by name, [(command, exit_code, output)])`` for one session."""
    s = {
        "mysql": "My" + _rand(12),
        "curl_user": "Cu" + _rand(12),
        "basic": base64.b64encode(f"admin:{_rand(12)}".encode()).decode(),
        "docker": "Dk" + _rand(14),
        "password_flag": "hunter" + str(secrets.randbelow(90) + 10),
        "vercel": _rand(24),
        "do_token": "dop" + "_v1_" + _hex(64),
        "pgpassword": "Pg" + _rand(12),
        "db_password": "Db" + _rand(12),
        "mailgun": "key" + "-" + _hex(32),
        "straddle_cmd": "gh" + "p_" + _rand(36),  # crosses the 300-char command clip
        "straddle_error": "gh" + "p_" + _rand(36),  # crosses the 160-char error clip
        "straddle_output": "gh" + "p_" + _rand(36),  # crosses the 200-char error-detail clip
        "test_env": "Te" + _rand(14),
        # CLI-01 review residuals: email-shaped curl user, vercel -t, attached docker -pX, multi-line commands.
        "curl_email": "Ce" + _rand(12),
        "vercel_t": _rand(24),
        "docker_attached": "Da" + _rand(12),
        "curl_multiline": "Cm" + _rand(12),
        "docker_multiline": "Dm" + _rand(12),
        "mysql_multiline": "Mm" + _rand(12),
    }
    commands = [
        (f"mysql -u root -p{s['mysql']} appdb -e 'select 1'", 1, "ERROR 2003 (HY000): Can't connect to MySQL server"),
        (f"curl -fsS -u admin:{s['curl_user']} https://api.example.com/health", 22, "curl: (22) 401"),
        (f"curl -fsS -H 'Authorization: Basic {s['basic']}' https://api.example.com/x", 22, "curl: (22) 403"),
        (f"docker login -u me -p {s['docker']} registry.example.com", 1, "Error response from daemon: unauthorized"),
        (f"./scripts/deploy.sh --env prod --password {s['password_flag']}", 2, "deploy: rejected"),
        (f"vercel deploy --prod --token {s['vercel']}", 1, "Error: token is not valid"),
        (f"doctl auth init -t {s['do_token']}", 1, "Error: unable to use supplied token"),
        (f"PGPASSWORD={s['pgpassword']} psql -h db -U app -c 'select 1'", 2, "psql: error: connection refused"),
        (f"export DB_PASSWORD={s['db_password']} && ./migrate up", 1, "migrate: failed"),
        (f"curl -s --user 'api:{s['mailgun']}' https://api.mailgun.net/v3/domains", 7, "curl: (7) failed"),
        ("echo " + "a" * 285 + " " + s["straddle_cmd"] + " | ./push.sh", 1, "push: denied"),
        ("./sync.sh " + "b" * 140 + " --token " + s["straddle_error"], 3, "sync: denied"),
        ("./report.sh", 4, "fatal: " + "c" * 180 + " token=" + s["straddle_output"] + " rejected"),
        (f"API_TOKEN={s['test_env']} pytest -q tests/test_api.py", 1, "=== 1 failed, 2 passed in 0.3s ==="),
        (f"curl -s -u dev@example.com:{s['curl_email']} https://acme.atlassian.net/rest/api/3/myself", 22, "curl: (22) 401"),
        (f"vercel deploy --prod -t {s['vercel_t']}", 1, "Error: token is not valid"),
        (f"docker login -u me -p{s['docker_attached']} ghcr.io", 1, "denied"),
        (f"curl -fsS \\\n  -X POST \\\n  -u admin:{s['curl_multiline']} \\\n  https://api.example.com/x", 22, "curl: (22) 403"),
        (f"docker login \\\n  -u me \\\n  -p {s['docker_multiline']} \\\n  registry.example.com", 1, "unauthorized"),
        (f"mysql \\\n  -h db.example.com \\\n  -u root \\\n  -p{s['mysql_multiline']} appdb", 1, "ERROR 1045"),
    ]
    return s, commands


def _transcript(tmp_path, cwd, commands, todo_secret: str):
    t = Transcript("S-cli01", cwd)
    for cmd, code, output in commands:
        t.bash(cmd, code, output)
    t.tool(
        "TodoWrite",
        {"todos": [{"content": "rotate " + "d" * 290 + " " + todo_secret, "status": "pending", "activeForm": "x"}]},
        "ok",
    )
    return t.write(tmp_path / "S-cli01.jsonl")


def _leaks(blob: str, planted: dict[str, str]) -> list[str]:
    """Names of secrets found in ``blob`` (whole value, or an 8-character piece of its random part)."""
    found = []
    for name, value in planted.items():
        body = value.split("_v1_")[-1].split("-", 1)[-1] if name in ("do_token", "mailgun") else value
        body = body[4:] if body.startswith("gh" + "p_") else body
        pieces = [value] + [body[i : i + 8] for i in range(0, max(1, len(body) - 7), 4)]
        if any(p in blob for p in pieces):
            found.append(name)
    return found


def test_parser_scrubs_before_clipping(tmp_path):
    planted, commands = _plant()
    planted["todo"] = "gh" + "p_" + _rand(36)
    repo = tmp_path / "repo"
    repo.mkdir()
    parsed = factlib.parse_claude_transcript(_transcript(tmp_path, repo, commands, planted["todo"]), root=str(repo))
    blob = json.dumps({"commands": parsed.commands, "tests": parsed.tests, "errors": parsed.errors, "todos": parsed.todos_open})
    assert _leaks(blob, planted) == []
    assert blob.count("[REDACTED:") >= len(planted)
    # The facts still say what happened: the commands, exit codes and clip marks are kept.
    assert any(c["cmd"].startswith("mysql -u root -p[REDACTED:") and c["exit_code"] == 1 for c in parsed.commands)
    assert any(c["cmd"].startswith("echo aaaa") and c["cmd"].endswith("…") for c in parsed.commands)
    assert parsed.tests[0]["passed"] is False and parsed.tests[0]["summary"] == "1 failed, 2 passed in 0.3s"


@pytest.mark.parametrize("pii", ["off", "redact"])
def test_cli_close_trail_and_brief_carry_no_command_line_credentials(wired, monkeypatch, capsys, tmp_path, pii):  # noqa: F811
    app = wired["api"]
    if pii == "redact":  # the production default
        app["app"].state.pii_detector = PIIDetector(enabled=True, mode="redact")
    _, clones = make_remote_and_clones(tmp_path, ("a", "b"))
    a, b = clones["a"], clones["b"]
    git(a, "remote", "set-url", "origin", "https://github.com/acme/cli01.git")
    git(b, "remote", "set-url", "origin", "https://github.com/acme/cli01.git")
    commit(a, "x.py", "x\n", "feat: wire the deploy")

    planted, commands = _plant()
    planted["todo"] = "gh" + "p_" + _rand(36)
    transcript = _transcript(tmp_path, a, commands, planted["todo"])
    end = json.dumps({"session_id": "S-cli01", "cwd": str(a), "transcript_path": str(transcript), "reason": "exit"})

    code, out, err = _run(monkeypatch, capsys, ["close", "--hook", "claude-code", "--agent", "claude-code", "--dry-run"], end)
    assert code == 0, err
    payload = json.loads(out[out.index("{\n") :])
    assert payload["facts"]["commands"] and payload["facts"]["errors"] and payload["facts"]["todos_open"]
    assert _leaks(json.dumps(payload), planted) == []

    code, out, err = _run(monkeypatch, capsys, ["close", "--hook", "claude-code", "--agent", "claude-code"], end)
    assert code == 0 and err == "", err

    trail = app["http"].get("/api/v1/trail", params={"project": "cli01"}).json()
    assert trail["total"] == 1
    handoff_id = trail["items"][0]["id"]
    stored = row(app, handoff_id)
    brief = app["http"].get("/api/v1/session/brief", params={"project_id": "cli01", "agent_id": "codex"}).json()
    memory = app["http"].get(f"/api/v1/memories/{handoff_id}").json()
    _, cli_trail, _ = _run(monkeypatch, capsys, ["trail", "--cwd", str(b)])
    _, cli_brief, _ = _run(monkeypatch, capsys, ["brief", "--agent", "codex", "--cwd", str(b)])

    for name, blob in (
        ("row", json.dumps(stored, default=str)),
        ("trail", json.dumps(trail)),
        ("brief", json.dumps(brief)),
        ("memory", json.dumps(memory)),
        ("cli trail", cli_trail),
        ("cli brief", cli_brief),
    ):
        assert _leaks(blob, planted) == [], name
    assert "[REDACTED:" in stored["content"] and "exited" in stored["content"]


def test_server_scrubs_before_truncating_oversized_fields(api):  # noqa: F811
    """An MCP agent types its facts: the server's own length caps (1000 per command,
    6000 for notes) must not cut a credential into a prefix the scrubber misses."""
    token = "gh" + "p_" + _rand(36)
    pw = "Pw" + _rand(14)
    facts = {
        "commands": [{"cmd": "x" * 975 + " " + token, "exit_code": 1}],
        "tests": [{"cmd": "y" * 975 + f" --password {pw}", "passed": False, "summary": "z" * 275 + " " + token}],
        "errors": ["e" * 975 + " " + token],
        "notes": "n" * 5975 + " " + token,
    }
    body = {"agent_id": "codex", "session_id": "s-trunc", "project_id": "trunc", "facts": facts}
    res = api["http"].post("/api/v1/session/close", json=body)
    assert res.status_code == 200, res.text
    stored = row(api, res.json()["handoff_id"])
    blob = json.dumps(stored, default=str) + json.dumps(api["http"].get("/api/v1/trail", params={"project": "trunc"}).json())
    assert _leaks(blob, {"token": token, "pw": pw}) == []
