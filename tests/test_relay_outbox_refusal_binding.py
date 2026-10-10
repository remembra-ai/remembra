"""A handoff the server refused for one key is never sent with another key.

A close the server answers with 403 (the key may not write to that project, or as that agent) stays in the
local outbox. It used to be tied only to *where* its key came from (``env``, ``claude:<path>``, ...), so a
different key put in the same place later sent it: a write the first key had been refused landed under the
second key. A refused entry now carries a one-way mark of the key that was refused, and goes only with
that same key.
"""

from __future__ import annotations

import argparse
import http.server
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from remembra.relay import cli, outbox

SCOPED = "rem_scoped_key_for_project_a_0123456789"
BROADER = "rem_broader_key_for_everything_0123456789"
REFUSAL = "HTTP 403: No access to project 'project-b'."


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    for name in ("REMEMBRA_PROJECT", "REMEMBRA_RELAY_PROJECT", "REMEMBRA_AGENT_ID", "REMEMBRA_USER_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def context(home: Path) -> cli.Context:
    return cli.Context(argparse.Namespace(agent="claude-code", cwd=str(home), hook=None), payload={})


def queue_refused_close(home: Path, monkeypatch, *, key: str, url: str, status: int | None = 403) -> outbox.Entry:
    monkeypatch.setenv("REMEMBRA_URL", url)
    monkeypatch.setenv("REMEMBRA_API_KEY", key)
    body = {"agent_id": "claude-code", "session_id": "session-1", "project_id": "project-b", "summary": "work in b"}
    cli._queue_close(context(home), body, REFUSAL if status == 403 else "ConnectError: refused", status)
    [entry] = outbox.pending(home)
    return entry


def test_refused_handoff_is_held_when_the_same_source_now_has_a_different_key(home, monkeypatch):
    entry = queue_refused_close(home, monkeypatch, key=SCOPED, url="http://127.0.0.1:9")
    monkeypatch.setenv("REMEMBRA_API_KEY", BROADER)
    config, held = cli.replay_config(entry)
    assert config is None, "a refused handoff must not be sent with a different key"
    assert held and "refused" in held and "different key" in held


def test_refused_handoff_still_goes_with_the_key_that_queued_it(home, monkeypatch):
    entry = queue_refused_close(home, monkeypatch, key=SCOPED, url="http://127.0.0.1:9")
    config, held = cli.replay_config(entry)  # the key's project access may have been fixed in the dashboard
    assert held is None and config is not None and config.api_key == SCOPED


def test_handoff_queued_for_an_unreachable_server_still_goes_after_the_key_is_replaced(home, monkeypatch):
    entry = queue_refused_close(home, monkeypatch, key=SCOPED, url="http://127.0.0.1:9", status=None)
    monkeypatch.setenv("REMEMBRA_API_KEY", BROADER)  # for example a rotated key: the server never judged the old one
    config, held = cli.replay_config(entry)
    assert held is None and config is not None and config.api_key == BROADER


def as_written_by_an_older_client(entry: outbox.Entry, home: Path) -> outbox.Entry:
    """The same queued entry as remembra 0.16.1 wrote it: no mark of the key that was refused."""
    data = dict(entry.data)
    data.pop("refused_key_fp", None)
    entry.path.write_text(json.dumps(data))
    [legacy] = outbox.pending(home)
    return legacy


@pytest.mark.parametrize("key_now", [BROADER, SCOPED])
def test_refused_entry_from_an_older_client_is_held_for_the_owner(home, monkeypatch, key_now):
    # Nothing records which key the server refused, so no key may send it: not a replacement, and not
    # "the same" key either, because the client cannot tell them apart.
    entry = queue_refused_close(home, monkeypatch, key=SCOPED, url="http://127.0.0.1:9")
    legacy = as_written_by_an_older_client(entry, home)
    monkeypatch.setenv("REMEMBRA_API_KEY", key_now)
    config, held = cli.replay_config(legacy)
    assert config is None, "a refused entry with no record of the refused key must not be sent automatically"
    assert held and "refused" in held and "delete the file" in held


def test_unrefused_entry_from_an_older_client_still_goes(home, monkeypatch):
    entry = queue_refused_close(home, monkeypatch, key=SCOPED, url="http://127.0.0.1:9", status=None)
    legacy = as_written_by_an_older_client(entry, home)
    monkeypatch.setenv("REMEMBRA_API_KEY", BROADER)
    config, held = cli.replay_config(legacy)
    assert held is None and config is not None


def retry_fails(entry: outbox.Entry, *, status: int | None, key: str) -> None:
    claimed = outbox.claim(entry)
    assert claimed is not None
    outbox.finish(
        entry, claimed, sent=False, error="ConnectError" if status is None else REFUSAL, http_status=status, api_key=key
    )


def test_a_later_network_failure_does_not_clear_the_refusal(home, monkeypatch):
    entry = queue_refused_close(home, monkeypatch, key=SCOPED, url="http://127.0.0.1:9")
    retry_fails(entry, status=None, key=SCOPED)
    [again] = outbox.pending(home)
    assert again.data.get("last_status") is None
    monkeypatch.setenv("REMEMBRA_API_KEY", BROADER)
    config, held = cli.replay_config(again)
    assert config is None and held and "different key" in held


def test_a_refusal_on_retry_binds_the_entry_to_the_key_that_was_refused(home, monkeypatch):
    entry = queue_refused_close(home, monkeypatch, key=BROADER, url="http://127.0.0.1:9", status=None)
    retry_fails(entry, status=403, key=SCOPED)  # the retry went out with the scoped key and was refused
    [again] = outbox.pending(home)
    monkeypatch.setenv("REMEMBRA_API_KEY", BROADER)
    assert cli.replay_config(again)[0] is None
    monkeypatch.setenv("REMEMBRA_API_KEY", SCOPED)
    config, held = cli.replay_config(again)
    assert held is None and config is not None and config.api_key == SCOPED


def test_the_entry_never_stores_the_key(home, monkeypatch):
    entry = queue_refused_close(home, monkeypatch, key=SCOPED, url="http://127.0.0.1:9")
    text = entry.path.read_text()
    assert SCOPED not in text and "rem_" not in text


@contextmanager
def close_endpoint() -> Iterator[tuple[str, list[str]]]:
    """A local stand-in for POST /api/v1/session/close: 403 for the scoped key, 201 for any other. Records the keys seen."""
    seen: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            key = self.headers.get("X-API-Key") or (self.headers.get("Authorization") or "").removeprefix("Bearer ")
            seen.append(key)
            refused = key == SCOPED
            body = json.dumps({"detail": "No access to project 'project-b'."} if refused else {"handoff_id": "h1"}).encode()
            self.send_response(403 if refused else 201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            return

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        thread.join(5)


def test_replay_with_a_different_key_sends_nothing_and_keeps_the_entry(home, monkeypatch):
    with close_endpoint() as (url, seen):
        queue_refused_close(home, monkeypatch, key=SCOPED, url=url)
        monkeypatch.setenv("REMEMBRA_API_KEY", BROADER)
        report = cli.replay_outbox(context(home))
        assert seen == [], f"the refused handoff was sent with another key: {len(seen)} request(s)"
        assert report.sent == []
        assert len(outbox.pending(home)) == 1


def test_replay_never_sends_an_older_clients_refused_entry(home, monkeypatch):
    # What an upgraded machine finds in its queue: an entry 0.16.1 was refused for, with no mark of the key.
    with close_endpoint() as (url, seen):
        entry = queue_refused_close(home, monkeypatch, key=SCOPED, url=url)
        as_written_by_an_older_client(entry, home)
        monkeypatch.setenv("REMEMBRA_API_KEY", BROADER)
        report = cli.replay_outbox(context(home))
        assert seen == [], f"an older client's refused handoff was sent with a replacement key: {len(seen)} request(s)"
        assert report.sent == []
        assert len(outbox.pending(home)) == 1
