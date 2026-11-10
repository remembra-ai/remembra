"""Review follow-ups for F7 and F20 (crew messages over MCP).

* ``crew_status(message=…)`` reads a message of any size the server accepts to its end, and a cut is
  said in words, never left to a trailing ellipsis.
* A long thread is read from its newest end (plus the root), not from its first 200 rows.
* The id recovery after a timed-out ``crew_say`` wait stays inside the client's 60 s, and also runs
  for sessions whose calls go through the local crew daemon (which reports "unreachable").

Every test goes through ``FastMCP.call_tool`` into the real MCP server module against a live local
API (``tests/crew/mcp_support.py``), except the two that exercise one helper directly.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

import tests.crew.test_mcp_crew as base
from remembra.crew import schemas as S
from remembra.mcp import crew as crew_mod
from tests.crew.test_mcp_crew import _two_agents, assert_clean, call, crew_id_of, use

live = base.live  # the module-scoped live API fixture and the bridge clock, shared with the WP-11 tests
clock = base.clock

MSG_RE = re.compile(r"msg_[0-9A-Za-z]+")
CLIENT_TIMEOUT_S = 60.0  # the Claude desktop MCP client's request timeout


def _sent_id(text: str) -> str:
    assert text.startswith("SENT msg_"), text
    found = MSG_RE.search(text)
    assert found, text
    return found.group(0)


def _data(text: str) -> str:
    _, blocks, errors = S.split_data_blocks(text)
    assert not errors, errors
    return "\n".join(blocks)


def _long(words: int, sentinel: str) -> str:
    return " ".join(f"w{i}" for i in range(words)) + f" {sentinel}"


# ---------------------------------------------------------------------------
# F7: the by-id read reaches the end of the largest message the server stores
# ---------------------------------------------------------------------------


def test_a_message_longer_than_4000_characters_is_read_to_its_end_by_id(live, clock):
    project, a, a_sign, b, b_sign = _two_agents(live, "f7long")
    use(b)
    body = _long(1500, "ENDMARK-LONG")
    assert 6000 < len(body) < S.MAX_MESSAGE_BYTES - 200
    msg_id = _sent_id(call("crew_say", body=body, kind="note"))
    use(a)
    out = call("crew_status", message=msg_id)
    data = _data(out)
    assert "ENDMARK-LONG" in data, out[-300:]
    assert "w1499 ENDMARK-LONG" in data and "is cut" not in out, out[-300:]
    assert_clean(out)


def test_the_full_read_limit_is_the_servers_message_limit():
    assert crew_mod.MESSAGE_FULL_CLIP == S.MAX_MESSAGE_BYTES == 8192


def test_a_body_that_is_cut_carries_how_much_was_cut():
    msg = {"id": "msg_01ABC", "kind": "note", "author_callsign": "cc-1", "body": "x" * 500, "trust_score": 1.0}
    cut = crew_mod.message_item(msg, 200)
    assert cut.cut == 301 and str(cut).endswith("…"), (cut.cut, str(cut)[-5:])  # 199 shown, 301 not
    whole = crew_mod.message_item(msg, 500)
    assert whole.cut == 0 and str(whole).endswith("x")
    assert crew_mod.message_item({**msg, "redacted": True}).cut == 0


def test_a_cut_mention_says_so_and_names_the_full_read(live, clock):
    project, a, a_sign, b, b_sign = _two_agents(live, "f7cutmention")
    use(b)
    body = _long(400, "ENDMARK-CUT")
    assert 1200 < len(body) < 4000
    msg_id = _sent_id(call("crew_say", body=body, kind="question", to=f"@{a_sign}"))
    short_id = _sent_id(call("crew_say", body="short one ENDMARK-SHORT", kind="question", to=f"@{a_sign}"))
    use(a)
    out = call("crew_status")
    outside = S.split_data_blocks(out)[0]
    assert "ENDMARK-CUT" not in out and "ENDMARK-SHORT" in _data(out), out
    line = next(ln for ln in outside.split("\n") if msg_id in ln)
    assert f'cut at 1000 characters: crew_status(message="{msg_id}") reads it in full' in line, line
    assert "cut at" not in next(ln for ln in outside.split("\n") if short_id in ln), outside
    assert_clean(out)
    assert "ENDMARK-CUT" in _data(call("crew_status", message=msg_id))


def test_a_cut_read_says_how_much_is_missing(live, clock, monkeypatch):
    """The server's limit is in bytes and the trust policy can lengthen a body (an image is replaced by
    a note), so a cut is still possible: it is said outside the data, with the count."""
    monkeypatch.setattr(crew_mod, "MESSAGE_FULL_CLIP", 300)
    project, a, a_sign, b, b_sign = _two_agents(live, "f7cutread")
    use(b)
    msg_id = _sent_id(call("crew_say", body=_long(200, "ENDMARK-GONE"), kind="note"))
    use(a)
    out = call("crew_status", message=msg_id)
    assert "ENDMARK-GONE" not in out, out
    assert re.search(r"The body is cut: the first 299 characters are shown, \d+ more are not\.", out), out


# ---------------------------------------------------------------------------
# F7: a thread is read from its newest end
# ---------------------------------------------------------------------------


def _spy_gets(monkeypatch: Any) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    real = crew_mod.CrewHttp.call

    def spy(self: Any, method: str, path: str, **kw: Any) -> Any:
        if method == "GET" and path.endswith("/messages"):
            seen.append(dict(kw.get("params") or {}))
        return real(self, method, path, **kw)

    monkeypatch.setattr(crew_mod.CrewHttp, "call", spy)
    return seen


def test_a_long_thread_is_read_from_its_newest_messages_with_the_root(live, clock, monkeypatch):
    monkeypatch.setattr(crew_mod, "THREAD_CONTEXT_MAX", 3)
    project, a, a_sign, b, b_sign = _two_agents(live, "f7thread")
    use(b)
    root_id = _sent_id(call("crew_say", body="thread root ROOTMARK", kind="question"))
    replies = [_sent_id(call("crew_say", body=f"reply number-{n}-of-7", kind="note", thread=root_id)) for n in range(1, 8)]
    use(a)
    seen = _spy_gets(monkeypatch)
    out = call("crew_status", message=replies[-1])
    data = _data(out)
    # the server is asked for the newest rows of the thread, never for its first 200
    tails = [p for p in seen if p.get("thread") == root_id and p.get("limit") != 1]
    assert tails and all(p.get("before") == S.SEQ_MAX and p["limit"] <= 5 for p in tails), seen
    # the asked reply in full, its root, and the newest other replies (3 context lines in all)
    assert "number-7-of-7" in data and "ROOTMARK" in data, out
    assert "number-6-of-7" in data and "number-5-of-7" in data, out
    assert "number-1-of-7" not in out and "number-2-of-7" not in out and "number-4-of-7" not in out, out
    assert data.index("ROOTMARK") < data.index("number-5-of-7") < data.index("number-6-of-7"), data
    assert f"THREAD {root_id}: the root and the latest 2 other messages (older ones are not shown)" in out, out
    assert_clean(out)
    # read by its root: the newest replies, and the count is not claimed past what was fetched
    by_root = call("crew_status", message=root_id)
    root_data = _data(by_root)
    assert "ROOTMARK" in root_data and "number-7-of-7" in root_data and "number-5-of-7" in root_data, by_root
    assert "number-4-of-7" not in by_root, by_root
    assert f"THREAD {root_id}: the latest 3 other messages (older ones are not shown)" in by_root, by_root


def test_a_short_thread_is_counted_and_shown_whole(live, clock, monkeypatch):
    project, a, a_sign, b, b_sign = _two_agents(live, "f7short")
    use(b)
    root_id = _sent_id(call("crew_say", body="thread root ROOTMARK", kind="question"))
    reply_id = _sent_id(call("crew_say", body="first reply ONEMARK", kind="note", thread=root_id))
    use(a)
    out = call("crew_status", message=root_id)
    assert f"THREAD {root_id}: 1 other message, oldest first, below it in the data." in out, out
    assert "ONEMARK" in _data(out)
    by_reply = call("crew_status", message=reply_id)
    assert f"THREAD {root_id}: 1 other message, oldest first, below it in the data." in by_reply, by_reply
    assert "ROOTMARK" in _data(by_reply)
    alone = _sent_id(call("crew_say", body="nobody answered", kind="note"))
    assert "No other message in its thread yet." in call("crew_status", message=alone)


# ---------------------------------------------------------------------------
# F20: the id recovery after a timed-out wait
# ---------------------------------------------------------------------------


def test_the_recovery_post_fits_under_the_client_timeout(live, clock, monkeypatch):
    """Both POSTs time out (a server that answers nothing): the second one is given a short timeout of
    its own, so the tool answers inside 60 s instead of waiting the client default of 30 s more."""
    project, a, a_sign, b, b_sign = _two_agents(live, "f20budget")
    use(b)
    real = crew_mod.CrewHttp.call
    posts: list[dict[str, Any]] = []

    def dead(self: Any, method: str, path: str, **kw: Any) -> Any:
        if method == "POST" and path.endswith("/messages"):
            posts.append({"wait_s": (kw.get("json") or {}).get("wait_s"), "timeout": kw.get("timeout")})
            raise crew_mod.CrewApiError(0, "timeout", "The Remembra server did not answer in time.")
        return real(self, method, path, **kw)

    monkeypatch.setattr(crew_mod.CrewHttp, "call", dead)
    text = call("crew_say", body="anyone?", kind="question", wait_s=120)
    assert text.startswith("CREW UNAVAILABLE:") and "may already be posted" in text, text
    assert [p["wait_s"] for p in posts] == [45, None], posts
    assert posts[1]["timeout"] is not None and 1.0 <= posts[1]["timeout"] <= crew_mod.MCP_RECOVER_S, posts
    assert posts[0]["timeout"] + posts[1]["timeout"] < CLIENT_TIMEOUT_S, posts
    assert crew_mod.MCP_HOLD_MAX_S + crew_mod.MCP_HOLD_GRACE_S + crew_mod.MCP_RECOVER_S < CLIENT_TIMEOUT_S


@pytest.mark.parametrize(
    "error, message",
    [("unreachable", "The local crew daemon did not answer."), ("timeout", "The Remembra server did not answer in time.")],
)
def test_a_waited_post_that_got_no_answer_is_recovered_whatever_the_transport_calls_it(live, clock, monkeypatch, error, message):
    """Calls of a hooked session go through the local crew daemon, which reports a silent daemon and an
    upstream timeout alike as status 0 "unreachable": the id is recovered there too."""
    project, a, a_sign, b, b_sign = _two_agents(live, "f20local")
    use(b)
    real = crew_mod.CrewHttp.call
    posts: list[dict[str, Any]] = []

    def cut(self: Any, method: str, path: str, **kw: Any) -> Any:
        body = kw.get("json")
        if method == "POST" and path.endswith("/messages") and isinstance(body, dict):
            posts.append(dict(body))
            if body.get("wait_s"):
                real(self, method, path, **{**kw, "json": {k: v for k, v in body.items() if k != "wait_s"}, "timeout": None})
                raise crew_mod.CrewApiError(0, error, message)
        return real(self, method, path, **kw)

    monkeypatch.setattr(crew_mod.CrewHttp, "call", cut)
    text = call("crew_say", body="can you release billing?", kind="request_release", to=f"@{a_sign}", wait_s=40)
    assert text.startswith("SENT msg_"), text
    assert "The wait for a reply was cut short" in text and "do not send it again" in text, text
    assert len(posts) == 2 and posts[0]["client_msg_id"] == posts[1]["client_msg_id"] and "wait_s" not in posts[1], posts
    rows = live.crew_rows(
        "SELECT id FROM crew_messages WHERE crew_id = ? AND kind = 'request_release'", (crew_id_of(live, project),)
    )
    assert len(rows) == 1 and rows[0]["id"] in text


def test_the_local_daemon_reports_no_answer_as_status_0(monkeypatch):
    """What :class:`LocalCrewHttp` raises for a daemon that did not answer, and for the daemon's own
    "unreachable" answer: the two shapes the recovery in ``crew_say`` has to accept."""
    import remembra.relay.crew.gate as gate

    http = crew_mod.LocalCrewHttp(object(), "key", object())  # type: ignore[arg-type]
    for answer in (None, {"ok": False, "status": 0, "error": "unreachable"}):
        monkeypatch.setattr(gate, "rpc", lambda *a, _answer=answer, **kw: _answer)
        with pytest.raises(crew_mod.CrewApiError) as caught:
            http.call("POST", "/crews/crew_1/messages", json={"body": "x", "wait_s": 5}, timeout=15.0)
        assert crew_mod.no_answer(caught.value), (answer, caught.value.status, caught.value.error)
    assert not crew_mod.no_answer(crew_mod.CrewApiError(409, "conflict", "no"))
    assert not crew_mod.no_answer(crew_mod.CrewApiError(0, "validation", "no"))


def test_an_unwaited_post_that_fails_is_not_posted_twice(live, clock, monkeypatch):
    project, a, a_sign, b, b_sign = _two_agents(live, "f20nowait")
    use(b)
    real = crew_mod.CrewHttp.call
    posts: list[Any] = []

    def down(self: Any, method: str, path: str, **kw: Any) -> Any:
        if method == "POST" and path.endswith("/messages"):
            posts.append(kw.get("json"))
            raise crew_mod.CrewApiError(0, "unreachable", "The Remembra server is unreachable.")
        return real(self, method, path, **kw)

    monkeypatch.setattr(crew_mod.CrewHttp, "call", down)
    text = call("crew_say", body="hello", kind="note")
    assert text.startswith("CREW UNAVAILABLE:") and len(posts) == 1, (text, posts)
