"""The install line is the same everywhere a new user copies it, and it installs remembra-mcp's extra.

`remembra-install` wires every agent to launch `remembra-mcp`, which imports
the `mcp` package: an install line without the ``[mcp]`` extra leaves every
agent with an MCP server that crashes at start. The dashboard's one-liner,
the landing page, the relay guide and the launcher's own hint must agree.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from remembra._launch import MCP_INSTALL

ROOT = Path(__file__).resolve().parent.parent
AGENTS_TS = ROOT / "dashboard" / "src" / "lib" / "agents.ts"
EXPECTED = "pipx install --force 'remembra[mcp]>=0.16'"


def _dashboard_pipx() -> str:
    match = re.search(r'export const PIPX_INSTALL = "([^"]+)";', AGENTS_TS.read_text())
    assert match, "PIPX_INSTALL not found in dashboard/src/lib/agents.ts"
    return match.group(1)


def _spec(command: str) -> str:
    match = re.search(r"'(remembra[^']*)'", command)
    assert match, command
    return match.group(1)


def test_dashboard_install_has_the_mcp_extra_and_the_release_pin() -> None:
    pipx = _dashboard_pipx()
    assert pipx == EXPECTED
    assert _spec(pipx) == "remembra[mcp]>=0.16"


def test_landing_and_relay_guide_use_the_dashboard_package_spec() -> None:
    pipx = _dashboard_pipx()
    for page in ("index.html", "crew.html"):
        text = (ROOT / "landing" / page).read_text()
        copies = [html.unescape(m) for m in re.findall(r'data-copy="([^"]*)"', text) if "pipx install" in html.unescape(m)]
        assert copies, page
        for copied in copies:
            assert copied.split("\n")[0] == pipx, page
    guide = (ROOT / "docs" / "guides" / "relay.md").read_text()
    installs = re.findall(r"^pipx install [^#\n]*?(?=\s+#|$)", guide, re.M)
    assert installs and all(line.strip() == pipx for line in installs), installs
    assert pipx == MCP_INSTALL  # the hint remembra-mcp prints when the extra is missing


def _pages() -> list[Path]:
    """Every page a user copies an install line from: the landing site, the docs, README and CHANGELOG."""
    return [
        *sorted((ROOT / "landing").glob("*.html")),
        *sorted((ROOT / "docs").rglob("*.md")),
        ROOT / "README.md",
        ROOT / "CHANGELOG.md",
    ]


def _lines(page: Path) -> list[str]:
    text = page.read_text()
    if page.suffix == ".html":
        # One line per rendered command line: tags stripped, entities decoded.
        text = re.sub(r"</span>\s*<span class=\"ln\">", "\n", text)
        text = html.unescape(re.sub(r"<[^>]+>", " ", text))
    return [" ".join(line.split()) for line in text.splitlines()]


def test_every_pipx_install_of_remembra_uses_the_dashboard_line() -> None:
    """A pipx line without --force leaves an old install in place, and without >=0.16 it may install a release
    that has no remembra-relay. Every copy on every page is the dashboard's line (a local --editable install aside)."""
    pipx = _dashboard_pipx()
    seen = 0
    for page in _pages():
        for line in _lines(page):
            command = line.lstrip("$ ").split(" #")[0].strip()
            if not command.startswith("pipx install") or "remembra" not in command or "--editable" in command:
                continue
            seen += 1
            assert command == pipx, f"{page.relative_to(ROOT)}: {command}"
    assert seen >= 8  # landing (index, crew, changelog), README, docs home, docs changelog, CHANGELOG, relay guide


def test_no_install_command_puts_the_key_on_the_command_line() -> None:
    dashboard = AGENTS_TS.read_text()
    assert "--api-key" not in dashboard
    for page in _pages():
        for line in _lines(page):
            where = f"{page.relative_to(ROOT)}: {line}"
            assert "--api-key <" not in line and "--api-key &lt;" not in line, where
            # remembra-install asks at a hidden prompt; only remembra-bridge still takes the key as an argument.
            if re.search(r"\bremembra-install(?![-\w]).*--api-key(?![-\w])", line):
                raise AssertionError(where)


def test_every_page_names_the_same_verified_agents() -> None:
    """Claude Code and Codex are the verified adapters (relay/adapters); no page may still call Codex unverified."""
    from remembra.relay.adapters import REGISTRY

    assert sorted(a.spec.name for a in REGISTRY.values() if a.spec.verified) == ["claude-code", "codex"]
    dashboard = AGENTS_TS.read_text()
    verified_ids = re.findall(r"'?([\w-]+)'?: \{[^}]*verified: true", dashboard)
    assert sorted(verified_ids) == ["claude-code", "codex"]
    stale = [
        "hooks for Codex, Cursor",
        "Codex, Cursor, Gemini CLI, Qwen Code, Kimi | unverified",
        "Codex, Cursor, Gemini CLI, Qwen Code and Kimi hooks ship unverified",
        "Codex, Cursor, Gemini CLI, Qwen Code and Kimi shipped unverified",
        "Claude Code's session hooks are verified. The hooks for Codex",
    ]
    for page in _pages():
        text = " ".join(html.unescape(page.read_text()).split())
        for phrase in stale:
            assert phrase not in text, f"{page.relative_to(ROOT)}: {phrase}"
    for page in ("README.md", "docs/index.md", "docs/reference/changelog.md", "CHANGELOG.md", "landing/changelog.html"):
        text = " ".join(html.unescape((ROOT / page).read_text()).split())
        assert "Codex" in text and "codex-cli 0.155.0-alpha.16.4" in text, page
        assert "Cursor, Gemini CLI, Qwen Code" in text and "unverified" in text, page
