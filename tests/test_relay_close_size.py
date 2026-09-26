"""Review regression (0.16.1 whole-release review): a large repository's close is stored, not refused.

DEP-1 capped every request body at 1 MiB unless its route is listed in
``remembra.core.body_limit.ROUTE_LIMITS``. ``POST /session/close`` was not
listed, although it is built to take oversized fact lists and clip them after
parsing. Relay clients send every changed path, so a repository with 12,000
modified files closed with a ~2 MB body: 413, "not queued", and the handoff was
lost. Now:

* the close route takes up to 8 MiB (it still keeps 500 paths per list);
* the relay client cuts every list to what the server keeps before sending.

These run the production routes behind the same ``BodySizeLimitMiddleware``
that ``create_app()`` adds (``tests/agent_api_harness.py``).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from remembra.api.v1 import relay as relay_api
from remembra.core.body_limit import MiB
from remembra.relay import facts as factlib
from tests.agent_api_harness import row
from tests.relay_fixtures import git, make_remote_and_clones
from tests.test_relay_cli_inprocess import _run, api, wired  # noqa: F401 - fixtures

MANY = 12_000


def _paths(n: int) -> list[str]:
    return [f"services/monorepo/packages/component_{i:05d}/src/generated_module_{i:05d}.py" for i in range(n)]


def _close_016_style(files: list[str], session: str = "s-mono") -> dict:
    """What a 0.16.0 (or unfixed 0.16.1) relay client sends: every changed path, twice."""
    return {
        "agent_id": "claude-code",
        "session_id": session,
        "project_id": "mono",
        "facts": {"branch": "main", "head_commit": "a" * 40, "files_changed": files, "uncommitted_files": files},
    }


def test_a_close_from_a_repository_with_12000_changed_files_is_stored(api):  # noqa: F811
    body = _close_016_style(_paths(MANY))
    raw = json.dumps(body).encode()
    assert len(raw) > 1 * MiB  # the review's repro: ~2 MB, refused with 413 before the fix
    res = api["http"].post("/api/v1/session/close", content=raw, headers={"Content-Type": "application/json"})
    assert res.status_code == 200, res.text[:300]
    relay_meta = json.loads(row(api, res.json()["handoff_id"])["metadata"])["relay"]
    assert (relay_meta["files_changed_count"], relay_meta["uncommitted_count"]) == (500, 500)  # clipped after parsing


def test_the_close_route_still_refuses_a_body_over_8_mib(api):  # noqa: F811
    raw = json.dumps(_close_016_style(_paths(70_000))).encode()
    assert len(raw) > 8 * MiB
    res = api["http"].post("/api/v1/session/close", content=raw, headers={"Content-Type": "application/json"})
    assert res.status_code == 413
    assert res.json()["detail"] == "Request body too large. The limit for this request is 8 MiB."


def test_the_client_caps_equal_the_servers():
    assert factlib.CLOSE_LIST_CAPS == relay_api._LIST_CAPS
    assert {"commands", "tests", "errors"} == factlib.CLOSE_KEEP_NEWEST


def test_merge_facts_cuts_every_list_to_what_the_server_keeps():
    transcript = factlib.TranscriptFacts(
        commands=[{"cmd": f"make step{i}", "exit_code": 0} for i in range(250)],
        tests=[{"cmd": f"pytest tests/test_{i}.py", "passed": True, "summary": None} for i in range(130)],
        errors=[f"error {i}" for i in range(60)],
        files=[f"/repo/edited_{i}.py" for i in range(10)],
        todos_open=[f"todo {i}" for i in range(120)],
    )
    long_path = "deep/" * 300 + "file.py"
    git_facts = {
        "branch": "main",
        "files_changed": [long_path, *_paths(MANY)],
        "uncommitted_files": _paths(MANY),
        "commits": [{"sha": f"{i:040x}", "subject": "s"} for i in range(30)],
    }
    facts = factlib.merge_facts(git_facts, transcript, "/repo")
    assert len(facts["files_changed"]) == 500 and len(facts["uncommitted_files"]) == 500
    assert (
        facts["files_changed"][0]
        == sorted(dict.fromkeys([long_path, *_paths(MANY), *[f"edited_{i}.py" for i in range(10)]]))[0][:1000]
    )
    assert max(len(p) for p in facts["files_changed"]) <= 1000
    # Event lists keep their newest entries, the others their first (as the server does).
    assert [c["cmd"] for c in facts["commands"]][-1] == "make step249" and len(facts["commands"]) == 200
    assert facts["commands"][0]["cmd"] == "make step50"
    assert len(facts["tests"]) == 100 and facts["tests"][-1]["cmd"] == "pytest tests/test_129.py"
    assert facts["errors"] == [f"error {i}" for i in range(10, 60)]
    assert facts["todos_open"] == [f"todo {i}" for i in range(100)]
    assert len(facts["commits"]) == 30
    # A path with spaces is kept as it is (only its length is capped).
    spaced = factlib.merge_facts({"uncommitted_files": ["a  b/c d.txt"]}, None, None)
    assert spaced["uncommitted_files"] == ["a  b/c d.txt"]


def _big_repo(tmp_path: Path, n: int) -> Path:
    _, clones = make_remote_and_clones(tmp_path)
    repo = clones["laptop"]
    git(repo, "remote", "set-url", "origin", "https://github.com/acme/mono.git")
    for i in range(n):
        (repo / f"generated_{i:04d}.txt").write_text(f"{i}\n")
    return repo


def test_the_relay_client_sends_a_close_the_server_accepts(wired, monkeypatch, capsys, tmp_path):  # noqa: F811
    repo = _big_repo(tmp_path, 650)
    status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=True)
    assert len(status.stdout.splitlines()) == 650

    code, out, _ = _run(monkeypatch, capsys, ["close", "--agent", "claude-code", "--cwd", str(repo), "--dry-run"])
    assert code == 0
    payload = json.loads(out[out.index("{\n") :])
    assert len(payload["facts"]["uncommitted_files"]) == 500 and len(payload["facts"]["files_changed"]) == 500
    assert len(json.dumps(payload).encode()) < 1 * MiB  # fits even a server without the larger close cap

    code, out, err = _run(monkeypatch, capsys, ["close", "--agent", "claude-code", "--cwd", str(repo)])
    assert code == 0 and "Remembra handoff" in out and "close failed" not in err, err
    outbox_dir = wired["home"] / ".remembra" / "relay" / "outbox"
    assert not outbox_dir.exists() or not list(outbox_dir.glob("*.json"))
