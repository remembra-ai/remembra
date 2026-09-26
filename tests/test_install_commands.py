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


def test_no_install_command_puts_the_key_on_the_command_line() -> None:
    dashboard = AGENTS_TS.read_text()
    assert "--api-key" not in dashboard
    for page in ("index.html", "crew.html"):
        assert "--api-key" not in (ROOT / "landing" / page).read_text(), page
    guide = (ROOT / "docs" / "guides" / "relay.md").read_text()
    assert "remembra-install --all --api-key" not in guide
