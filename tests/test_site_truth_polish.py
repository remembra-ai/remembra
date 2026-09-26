"""What the site and docs say about releases, third-party loads, the bridge and Windsurf matches the code.

- security.html's ASI04 row says what .github/workflows/release.yml produces;
- privacy.html and subprocessors.html name every third party the dashboard
  (dashboard/index.html) loads on every page;
- no page teaches `remembra-bridge --url` with a key on the command line or the
  wrong port;
- no page claims `remembra-install --all` sets up Windsurf, which is unverified.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from remembra.tools.agents import AGENT_CONFIGS, UNVERIFIED_AGENTS
from remembra.tools.bridge import DEFAULT_BRIDGE_PORT

ROOT = Path(__file__).resolve().parents[1]
LANDING = ROOT / "landing"
DOC_FILES = [ROOT / "README.md", *sorted((ROOT / "docs").rglob("*.md")), *sorted(LANDING.glob("*.html"))]

yaml = pytest.importorskip("yaml")


def _text(html: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", html).split())


def test_security_page_says_what_a_release_carries() -> None:
    release = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text())
    steps = [s for job in release["jobs"].values() for s in job.get("steps", [])]
    pypi = next(s for s in steps if "gh-action-pypi-publish" in str(s.get("uses")))
    docker = next(s for s in steps if "docker/build-push-action" in str(s.get("uses")))
    assert pypi["with"]["attestations"] is True and docker["with"]["sbom"] is True and docker["with"]["provenance"]

    row = next(r for r in re.findall(r"<tr>(.*?)</tr>", (LANDING / "security.html").read_text(), re.S) if "ASI04" in r)
    text = _text(row)
    assert "no signature yet" not in text and "there is no SBOM" not in text
    assert "PEP 740 attestations" in text and "trusted publishing" in text
    assert "Docker image carries build provenance and an SBOM" in text
    assert "The Python packages have no SBOM" in text  # the wheel and sdist ship none
    assert "0.13.2 and earlier have no attestations" in text
    # SECURITY.md says the same.
    security_md = (ROOT / "SECURITY.md").read_text()
    assert "PEP 740 attestations" in security_md and "provenance and an SBOM" in security_md


def test_privacy_pages_name_what_every_dashboard_page_loads() -> None:
    index = (ROOT / "dashboard" / "index.html").read_text()
    assert "fonts.googleapis.com" in index and "https://cdn.paddle.com/paddle/v2/paddle.js" in index
    privacy = _text((LANDING / "privacy.html").read_text())
    assert "remembra.dev and the dashboard (app.remembra.dev) load their typefaces from Google Fonts" in privacy
    assert "Every page of the dashboard (app.remembra.dev) loads Paddle's checkout script, Paddle.js" in privacy
    assert "remembra.dev itself does not load Paddle.js" in privacy
    loads_paddle = re.compile(r"<script[^>]+cdn\.paddle\.com|['\"]https://cdn\.paddle\.com")
    landing_files = [*LANDING.glob("*.html"), *LANDING.glob("*.js"), *LANDING.glob("blog/**/*.html")]
    assert not [p.name for p in landing_files if loads_paddle.search(p.read_text())]
    subprocessors = _text((LANDING / "subprocessors.html").read_text())
    assert "Serves the typefaces on remembra.dev and the dashboard (app.remembra.dev)" in subprocessors
    assert "Paddle.js, loads from cdn.paddle.com on every page of the dashboard" in subprocessors


def test_no_page_teaches_a_bridge_flag_or_port_that_does_not_exist() -> None:
    bad = []
    for path in DOC_FILES:
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"remembra-bridge\b[^\n`]*--url\b", line) or "localhost:8766" in line:
                bad.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()}")
            if re.search(r"remembra-bridge\b[^\n`]*--api-key\s+\S", line):
                bad.append(f"{path.relative_to(ROOT)}:{n}: key on the command line: {line.strip()}")
    assert bad == []
    setup = (ROOT / "docs" / "getting-started" / "agent-setup.md").read_text()
    assert f"--url http://127.0.0.1:{DEFAULT_BRIDGE_PORT}" in setup and "remembra-bridge --upstream" in setup


def test_no_page_says_install_all_sets_up_windsurf() -> None:
    assert "windsurf" in AGENT_CONFIGS and "windsurf" in UNVERIFIED_AGENTS
    assert AGENT_CONFIGS["windsurf"].parts[-3:] == (".codeium", "windsurf", "mcp_config.json")
    claims = re.compile(r"(auto-detects|configures|Auto-configured|sets up)[^.\n]*Windsurf|Windsurf[^.\n|]*Auto-configured", re.I)
    bad = []
    for path in DOC_FILES:
        if path.name in ("changelog.md", "changelog.html", "competitive-analysis-2026.md", "connect.md"):
            continue  # past releases, and a page about adding the server by hand
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if claims.search(line) and "unverified" not in line.lower() and "does not" not in line:
                bad.append(f"{path.relative_to(ROOT)}:{n}: {line.strip()[:160]}")
    assert bad == []
    assert "~/.windsurf/mcp_config.json" not in (ROOT / "docs" / "getting-started" / "agent-setup.md").read_text()
