"""Marshal's voice: radio calls, no chat-assistant filler, no emoji, no "I"."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from remembra.marshal import tools
from tests.marshal_fixtures import FakeHome

BANNED = ("How can I help", "I'm here to help", "Great question", "Sure!", "AI Assistant", "✨", "I'm sorry", "Sorry,")
SOURCES = sorted((Path(__file__).resolve().parents[1] / "src" / "remembra" / "marshal").glob("*.py"))


@pytest.mark.parametrize("path", SOURCES, ids=lambda p: p.name)
def test_no_banned_phrases_in_marshal_source(path: Path) -> None:
    text = path.read_text()
    for phrase in BANNED:
        assert phrase not in text, (path.name, phrase)


def test_rendered_output_speaks_in_radio_calls(tmp_path: Path) -> None:
    fh = FakeHome(tmp_path)
    fh.hooks("codex")
    (fh.home / ".cursor").mkdir()
    outputs = [
        tools.doctor_payload(None, False, environ=fh.environ(), home=fh.home, which=fh.which)["rendered"],
        tools.setup_payload(None, environ=fh.environ(), home=fh.home, which=fh.which, os_id="macos", shell="zsh")["rendered"],
        tools.help_payload("how do I uninstall")["rendered"],
        tools.help_payload("what's the meaning of life")["rendered"],
    ]
    emoji = re.compile("[\U0001f300-\U0001faff☀-⛿✀-➿]")
    for text in outputs:
        for phrase in BANNED:
            assert phrase not in text
        assert not emoji.search(text), emoji.findall(text)
        assert "!" not in text.replace("[!!]", "").replace("!=", "")
        assert not re.search(r"(^|[\s(])I (am|will|can|think|found)\b", text)


def test_the_readme_and_troubleshooting_name_the_doctor() -> None:
    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text()
    for tool in ("remembra_doctor", "remembra_setup", "remembra_help"):
        assert f"`{tool}`" in readme, tool
    assert "`remembra-relay doctor`" in readme
    troubleshooting = (root / "docs" / "TROUBLESHOOTING.md").read_text()
    assert "remembra-relay doctor" in troubleshooting and "(guides/relay.md#doctor)" in troubleshooting
