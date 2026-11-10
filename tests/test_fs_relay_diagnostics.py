"""F51: the relay's local diagnostics keep their history.

* ``remembra-relay status`` is a read command: its key check no longer replaces an agent's last brief or
  close result ("brief ok 5m ago" became "status ok 2m ago").
* A failed brief leaves a line in ``relay.log`` (an HTTP 521 at session start left none), and so does a
  queued handoff whose resend failed.
* ``status.json`` keeps the last result per command and the last ten failures per agent, so a later failure
  of another command no longer erases the brief's.
* An empty close logs one short line, and the log keeps three rotated generations.

The real CLI in a subprocess against local stub servers, an isolated HOME per test.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from remembra.marshal import signals
from remembra.relay import outbox
from tests.test_fs_relay_refused_outbox import scoped_server
from tests.test_relay_stop_outbox import _free_port, _payload, fixed_status_server, relay


@pytest.fixture()
def home(tmp_path) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    return h


def _log(home: Path) -> str:
    try:
        return outbox.log_path(home).read_text()
    except OSError:
        return ""


def test_status_does_not_replace_the_agents_last_result(home):
    with scoped_server() as (url, _handler):
        brief = relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
        assert brief.returncode == 0, brief.stderr
        before = outbox.read_status(home)["agents"]["claude-code"]["last_success"]
        assert before["command"] == "brief"
        status = relay(home, url, "status", "--agent", "claude-code")
        assert status.returncode == 0 and "key: accepted by the server" in status.stdout
        assert "last ok: brief" in status.stdout
        recorded = outbox.read_status(home)
        assert recorded["agents"]["claude-code"]["last_success"] == before
        assert recorded["keys"]["env"]["state"] == "accepted"  # the key check itself is still remembered


@pytest.mark.parametrize(("code", "state"), [(401, "rejected"), (403, "refused")])
def test_a_failed_key_check_by_status_leaves_the_agents_last_failure_alone(home, code, state):
    outbox.record(home, agent_id="claude-code", command="brief", ok=False, error="HTTP 521: origin down", http_status=521)
    before = outbox.read_status(home)["agents"]["claude-code"]
    with fixed_status_server(code, "no") as url:
        status = relay(home, url, "status", "--agent", "claude-code")
        assert status.returncode == 1
    recorded = outbox.read_status(home)
    assert recorded["agents"]["claude-code"] == before  # the brief's 521 is still the last failure
    assert recorded["keys"]["env"]["state"] == state and recorded["keys"]["env"]["http_status"] == code
    again = relay(home, "http://127.0.0.1:9", "status", "--no-check", "--agent", "claude-code")
    assert "last failure: brief" in again.stdout and "521" in again.stdout


def test_a_failed_brief_is_logged(home):
    with fixed_status_server(521, "origin down") as url:
        brief = relay(home, url, "brief", "--agent", "claude-code", "--session-id", "S1", "--cwd", str(home))
        assert brief.returncode == 0 and "HTTP 521" in brief.stderr
    lines = [line for line in _log(home).splitlines() if "brief: failed" in line]
    assert len(lines) == 1 and "claude-code" in lines[0] and "HTTP 521: origin down" in lines[0]


def test_a_brief_that_cannot_reach_the_server_is_logged_once(home):
    brief = relay(home, f"http://127.0.0.1:{_free_port()}", "brief", "--agent", "claude-code", "--cwd", str(home))
    assert brief.returncode == 0
    lines = [line for line in _log(home).splitlines() if "brief: failed" in line]
    assert len(lines) == 1 and "ConnectError" in lines[0]
    failure = outbox.read_status(home)["agents"]["claude-code"]["last_failure"]
    assert failure["command"] == "brief" and "ConnectError" in failure["error"]


def test_a_queued_handoff_whose_resend_fails_is_logged(home):
    with fixed_status_server(503, "busy") as url:
        assert outbox.enqueue(home, _payload("codex", "Q1"), url=url, config_source="env", error="ReadTimeout")
        relay(home, url, "brief", "--agent", "claude-code", "--cwd", str(home))
    log = _log(home)
    assert "outbox: resend failed for codex session Q1 (kept): HTTP 503: busy" in log


def test_a_later_failure_of_another_command_does_not_erase_the_briefs(home):
    outbox.record(home, agent_id="claude-code", command="brief", ok=True)
    outbox.record(home, agent_id="claude-code", command="brief", ok=False, error="HTTP 521: origin down", http_status=521)
    outbox.record(home, agent_id="claude-code", command="close", ok=False, error="ReadTimeout: timed out")
    outbox.record(home, agent_id="claude-code", command="close", ok=True)
    slot = outbox.read_status(home)["agents"]["claude-code"]
    # the two legacy slots, as every older reader expects them
    assert slot["last_failure"]["command"] == "close" and slot["last_success"]["command"] == "close"
    # per command
    assert slot["by_command"]["brief"]["last_failure"]["http_status"] == 521
    assert slot["by_command"]["brief"]["last_success"]["command"] == "brief"
    assert slot["by_command"]["close"]["last_failure"]["error"].startswith("ReadTimeout")
    # the recent failures, oldest first
    assert [(f["command"], f["http_status"]) for f in slot["recent_failures"]] == [("brief", 521), ("close", None)]
    status = relay(home, "http://127.0.0.1:9", "status", "--no-check", "--agent", "claude-code")
    assert "recent failures:" in status.stdout and "brief" in status.stdout and "HTTP 521" in status.stdout


def test_recent_failures_are_bounded_and_odd_status_files_do_not_break_record(home):
    for n in range(outbox.RECENT_FAILURES + 5):
        outbox.record(home, agent_id="a", command="close", ok=False, error=f"e{n}", http_status=500)
    recent = outbox.read_status(home)["agents"]["a"]["recent_failures"]
    assert len(recent) == outbox.RECENT_FAILURES and recent[-1]["error"] == f"e{outbox.RECENT_FAILURES + 4}"
    # a status file written by the 0.17.5 client (no by_command, no recent_failures), and odd values
    old = {"agents": {"a": {"last_success": {"command": "brief", "ts": 1.0}, "recent_failures": "x", "by_command": []}}}
    outbox.status_path(home).write_text(json.dumps(old))
    outbox.record(home, agent_id="a", command="close", ok=False, error=None)
    outbox.record(home, agent_id=None, command="brief", ok=True)
    slot = outbox.read_status(home)["agents"]["a"]
    assert slot["last_success"]["command"] == "brief" and len(slot["recent_failures"]) == 1
    assert slot["by_command"]["close"]["last_failure"]["error"] == ""
    assert "unknown-agent" in outbox.read_status(home)["agents"]


def test_concurrent_records_do_not_lose_each_other(home):
    """Several hooks end in the same second (an app quit): each one's result must survive."""
    agents = [f"agent-{n}" for n in range(8)]

    def work(agent: str) -> None:
        for n in range(12):
            outbox.record(home, agent_id=agent, command="close", ok=False, error=f"e{n}")

    threads = [threading.Thread(target=work, args=(agent,)) for agent in agents]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    recorded = outbox.read_status(home)["agents"]
    assert sorted(recorded) == agents
    assert all(len(recorded[a]["recent_failures"]) == outbox.RECENT_FAILURES for a in agents)
    assert not outbox.status_path(home).with_name("status.lock").exists()


def test_an_empty_close_logs_one_short_line(home):
    close = relay(home, "http://127.0.0.1:9", "close", "--agent", "codex", "--session-id", "idle", "--cwd", str(home))
    assert close.returncode == 0 and "nothing to hand off" in close.stderr and "Add --summary" in close.stderr
    [line] = [line for line in _log(home).splitlines() if "nothing to hand off" in line]
    assert "close: nothing to hand off for codex session idle; not sent" in line
    assert len(line) < 120  # was about 250 bytes: 58% of the log on the surveyed machine


def test_the_log_keeps_three_rotated_generations(home, monkeypatch):
    monkeypatch.setattr(outbox, "LOG_MAX_BYTES", 300)
    for n in range(60):
        outbox.log(home, f"line {n:03d} " + "x" * 40)
    names = sorted(p.name for p in outbox.relay_dir(home).glob("relay.log*"))
    assert names == ["relay.log", "relay.log.1", "relay.log.2", "relay.log.3"]
    text = "".join((outbox.relay_dir(home) / name).read_text() for name in names)
    assert "line 059" in text and "line 000" not in text  # the newest are kept, the oldest generation is gone
    newest_first = [outbox.relay_dir(home) / name for name in names]
    assert "line 059" in newest_first[0].read_text()
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in newest_first)


def test_doctor_counts_skipped_sessions_in_every_generation(home):
    line = "skipped close: codex automation session s1"
    outbox.log(home, line)
    for generation in (1, 2, 3):
        (outbox.relay_dir(home) / f"relay.log.{generation}").write_text(f"2026-01-01T00:00:00Z pid=1 {line}\n")
    assert signals._skipped_logged(home) == 4
