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


# ---------------------------------------------------------------------------
# Claims the launch fact-check found overstated (2026-09-26). Each test scans
# every public file, so a claim cannot come back on a page nobody re-read.
# ---------------------------------------------------------------------------


def _mkdocs_excluded() -> list[str]:
    block = re.search(r"exclude_docs: \|\n((?:  .*\n)+)", (ROOT / "mkdocs.yml").read_text())
    return block.group(1).split() if block else []


def _public_files() -> list[Path]:
    """What reaches readers: the README, changelog and release notes, package and registry metadata, the
    published docs, everything remembra.dev serves as text, the page sources kept in scripts/, and the
    dashboard's screens (its sign-in page is public)."""
    excluded = _mkdocs_excluded()

    def published(path: Path) -> bool:
        rel = path.relative_to(ROOT / "docs").as_posix()
        return not any(rel == e or (e.endswith("/") and rel.startswith(e)) for e in excluded)

    files = [
        ROOT / "README.md",
        ROOT / "CHANGELOG.md",
        ROOT / "pyproject.toml",
        ROOT / "server.json",
        ROOT / "src" / "remembra" / "api" / "well_known" / "server-card.json",
        ROOT / "packages" / "remembra-mcp" / "README.md",
        ROOT / "packages" / "remembra-mcp" / "pyproject.toml",
        ROOT / "scripts" / "site-crew-section.html",
        ROOT / "scripts" / "site-social-card.html",
    ]
    files += sorted(ROOT.glob("RELEASE-NOTES-*.md"))
    files += sorted(p for p in (ROOT / "docs").rglob("*.md") if published(p))
    files += sorted(p for p in (ROOT / "dashboard" / "src").rglob("*.tsx") if "__tests__" not in p.parts)
    suffixes = (".html", ".js", ".txt", ".md", ".webmanifest", ".xml")
    files += sorted(p for p in LANDING.rglob("*") if p.is_file() and p.suffix in suffixes)
    return files


def _hits(pattern: re.Pattern[str], files: list[Path] | None = None) -> list[str]:
    found = []
    for path in files if files is not None else _public_files():
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            for m in pattern.finditer(line):
                found.append(f"{path.relative_to(ROOT)}:{n}: ...{line[max(0, m.start() - 50) : m.end() + 50]}...")
    return found


def test_the_public_file_list_covers_the_site_and_the_registry_metadata() -> None:
    files = {p.relative_to(ROOT).as_posix() for p in _public_files()}
    for must in (
        "landing/index.html",
        "landing/pricing.html",
        "landing/security.html",
        "landing/changelog.html",
        "landing/blog/remembra-vs-mem0-vs-zep.html",
        "docs/comparisons/handoff-tools.md",
        "docs/integrations/claude-and-chatgpt-apps.md",
        "server.json",
        "pyproject.toml",
        "packages/remembra-mcp/README.md",
        "dashboard/src/brand/AuthFrame.tsx",
    ):
        assert must in files, must
    assert not any(f.startswith("docs/bugs/") for f in files)


# "sign in", "signed up", "signs you out" and a "signed data processing agreement" are about people and paper.
SIGNED = re.compile(r"\b(signs|signed)\b(?![\s-]+(?:in|out|up|you|into|data)\b)", re.I)


def test_no_public_copy_says_a_handoff_is_signed() -> None:
    """Handoffs carry no signature. One closed with an agent-scoped key is key-verified (relay.effective_agent);
    anything else is self-declared."""
    assert _hits(SIGNED) == []
    server = (ROOT / "server.json").read_text()
    assert "recorded under" in server and "signs" not in server


def test_pypi_summary_describes_relay_without_signing() -> None:
    import tomllib

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["description"].startswith("Remembra Relay: session handoffs between coding agents")
    assert "Universal memory layer" not in project["description"]


LOCKED = re.compile(r"\block(?:ed)? (?:in )?for life\b|\blifetime (?:lock|price)\b", re.I)


def test_no_public_copy_says_the_founding_price_is_locked_for_life() -> None:
    """The Terms: the Founding price holds while the subscription stays active, and 14 days after it ends."""
    from datetime import timedelta

    from remembra.cloud.metering import FOUNDING_LAPSE_GRACE

    assert FOUNDING_LAPSE_GRACE == timedelta(days=14)
    emails = [ROOT / "src" / "remembra" / "cloud" / "email_templates.py"]
    assert _hits(LOCKED) == [] and _hits(LOCKED, emails) == []
    terms = _text((LANDING / "terms.html").read_text())
    pricing = _text((LANDING / "pricing.html").read_text())
    assert "never goes up while the subscription stays active" in terms and "kept for 14 days" in terms
    assert "never goes up while the subscription stays active" in pricing and "kept for 14 days" in pricing
    plans_doc = " ".join((ROOT / "docs" / "reference" / "plans-and-credits.md").read_text().split())
    assert "never goes up while the subscription stays active" in plans_doc and "kept for 14 days" in plans_doc


# Every agent and every tool: the hooks are verified for some agents, and any MCP agent can call the tools.
# "If every agent you run is Claude Code" (the comparison page's advice) is a condition, not a claim.
EVERY = re.compile(
    r"\ball (?:of )?your (?:ai )?(?:agents|tools)\b"
    r"|\bany (?:ai )?(?:coding )?(?:tool|client)\b"
    r"|\bevery agent you run\b(?! is claude code)"
    r"|\bevery agent, every machine\b"
    r"|\bevery agent leaves\b"
    r"|\bworks with every agent\b"
    r"|\bwhatever the tool\b"
    r"|\bany agent on any machine\b",
    re.I,
)


def test_no_public_copy_claims_every_agent_or_any_tool() -> None:
    assert _hits(EVERY) == []


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.;!?])\s+", " ".join(text.split())) if s]


def test_transcript_claims_name_every_agent_whose_transcript_is_read() -> None:
    """remembra-relay reads Claude Code's JSONL transcript and Codex's rollout (relay/facts.py); a sentence that
    scopes transcript facts to Claude Code alone is out of date."""
    from remembra.relay import facts
    from remembra.relay.adapters import REGISTRY

    parsed = {name: a.spec.transcript_format for name, a in REGISTRY.items() if a.spec.transcript_format}
    assert parsed == {"claude-code": facts.CLAUDE_JSONL, "codex": facts.CODEX_ROLLOUT}
    assert set(facts.TRANSCRIPT_FORMATS) == set(parsed.values())
    claude_only = re.compile(r"\(?\bfor Claude Code\b\)?(?!\s+and Codex)")
    bad = []
    for path in _public_files():
        text = _text(path.read_text(encoding="utf-8", errors="replace"))
        for sentence in _sentences(text):
            if claude_only.search(sentence) and re.search(r"transcript|test runs?\b", sentence, re.I):
                bad.append(f"{path.relative_to(ROOT)}: {sentence[:200]}")
    assert bad == []
    row = next(ln for ln in (ROOT / "docs" / "comparisons" / "handoff-tools.md").read_text().splitlines() if ln.startswith("| **Remembra Relay**"))
    assert "Claude Code transcript or Codex rollout" in row


# Names the pages use for each adapter (relay/adapters/*.py).
AGENT_NAMES = {"claude-code": "Claude Code", "codex": "Codex", "cursor": "Cursor", "gemini": "Gemini CLI", "qwen": "Qwen Code", "kimi": "Kimi"}


@pytest.mark.parametrize(
    "page", [ROOT / "docs" / "comparisons" / "handoff-tools.md", LANDING / "blog" / "remembra-vs-mem0-vs-zep.html"], ids=lambda p: p.name
)
def test_pages_name_the_verified_agents_the_registry_verifies(page: Path) -> None:
    """The comparison page and the blog list which hooks are verified: exactly the adapters marked verified,
    Codex with the prerelease version it was run on (codex.TESTED_VERSIONS)."""
    from remembra.relay.adapters import REGISTRY
    from remembra.relay.adapters.codex import TESTED_VERSIONS

    assert set(AGENT_NAMES) == set(REGISTRY)
    clauses = _sentences(_text(page.read_text()))
    verified = next(c for c in clauses if re.search(r"hooks are verified", c) and "not" not in c.split("verified")[0])
    unverified = next(c for c in clauses if re.search(r"have not been run against it yet|are not yet\b", c))
    for name, adapter in REGISTRY.items():
        shown = AGENT_NAMES[name]
        if adapter.spec.verified:
            assert shown in verified and shown not in unverified, (name, verified)
        else:
            assert shown in unverified and shown not in verified, (name, unverified)
    if REGISTRY["codex"].spec.verified:
        for version in TESTED_VERSIONS:
            assert f"codex-cli {version}" in verified and ("prerelease" in verified) == ("-" in version)
    assert "only Claude Code" not in _text(page.read_text())


def test_the_phone_connector_is_marked_coming_wherever_it_appears() -> None:
    """The remote connector is off unless REMEMBRA_CONNECTOR_ENABLED is set, and api.remembra.dev/mcp answers 405:
    no page may present it as live. The home section's gate comment stays (tests/test_landing_site.py)."""
    from remembra.config import Settings

    assert Settings.model_fields["connector_enabled"].default is False
    home = (LANDING / "index.html").read_text()
    assert "<!-- beta: the connector is built and tested locally; not yet verified inside the live Claude and ChatGPT apps -->" in home
    marker = re.compile(r"not live|not switched on|coming", re.I)
    bad = []
    for path in sorted(LANDING.rglob("*.html")):
        page = path.read_text()
        if path.name == "index.html":
            page = re.sub(r'<section class="sec" id="anywhere".*?</section>', "", page, flags=re.S)  # labelled as a whole
        for fragment in re.findall(r"<(li|tr|p)\b[^>]*>(.*?)</\1>", page, re.S):
            text = _text(fragment[1])
            if re.search(r"Claude and ChatGPT (apps|connector)|Remembra connector", text) and not marker.search(text):
                bad.append(f"{path.relative_to(ROOT)}: {text[:160]}")
    assert bad == []
    doc = (ROOT / "docs" / "integrations" / "claude-and-chatgpt-apps.md").read_text()
    head = doc.split("## ", 1)[0]
    assert "(coming, not live)" in head.splitlines()[0]
    assert '!!! warning "Coming: not live on Remembra Cloud yet"' in head and "answers 405" in head
    assert "Claude & ChatGPT Apps (coming): integrations/claude-and-chatgpt-apps.md" in (ROOT / "mkdocs.yml").read_text()
    connect = (ROOT / "docs" / "connect.md").read_text()
    assert "(coming, not live)" in connect and "mcp.remembra.dev` is not running" in connect
    assert "Remote (recommended)" not in connect


def test_team_card_and_plan_features_claim_only_what_the_teams_api_enforces() -> None:
    from remembra.api.v1.billing import PLAN_FEATURES
    from remembra.cloud.plans import PlanTier
    from remembra.teams.manager import VALID_ROLES

    assert VALID_ROLES == {"owner", "admin", "member", "viewer"}
    card = _text(re.search(r'<article class="plan" aria-labelledby="p-team">(.*?)</article>', (LANDING / "pricing.html").read_text(), re.S).group(1))
    assert "The owner and admins invite and remove teammates and set their roles" in card
    assert "through shared spaces, read or write per person" in card
    # A viewer has the same team access as a member (see the API test below) and team roles do not gate notes.
    assert "viewer" not in card.lower() and "read-only" not in card.lower()
    team = " ".join(PLAN_FEATURES[PlanTier.TEAM])
    assert "viewer" not in team.lower() and "team inbox" not in team.lower() and "shared projects" not in team.lower()


async def test_team_viewer_has_the_same_access_as_a_member_and_admins_manage(tmp_path) -> None:
    """What the pricing card may promise, checked through the teams API: owner and admins invite, remove and
    set roles; a member and a viewer can each read the team and nothing more; only the owner deletes it."""
    from remembra.api.v1 import teams
    from remembra.teams.manager import TeamManager
    from tests.security_harness import secure_app

    async with secure_app(tmp_path, [teams.router]) as h:
        tm = TeamManager(h.db)
        await tm.init_schema()
        h.app.state.team_manager = tm
        users = {role: await h.create_user(f"{role}@example.com") for role in ("owner", "admin", "member", "viewer", "extra")}
        team = await tm.create_team(name="Acme", owner_id=users["owner"], max_seats=10)  # room for the invites below
        tid = team["id"]
        for role in ("admin", "member", "viewer"):
            await tm.add_member(tid, users[role], role=role, invited_by=users["owner"])
        await tm.add_member(tid, users["extra"], role="member", invited_by=users["owner"])

        def auth(role: str) -> dict[str, str]:
            return h.jwt(users[role], f"{role}@example.com")

        async def attempts(role: str) -> dict[str, int]:
            c = h.client
            return {
                "read": (await c.get(f"/api/v1/teams/{tid}", headers=auth(role))).status_code,
                "members": (await c.get(f"/api/v1/teams/{tid}/members", headers=auth(role))).status_code,
                "invite": (await c.post(f"/api/v1/teams/{tid}/invites", json={"email": f"new-{role}@example.com", "role": "member"}, headers=auth(role))).status_code,
                "set_role": (await c.patch(f"/api/v1/teams/{tid}/members/{users['extra']}/role", json={"role": "viewer"}, headers=auth(role))).status_code,
                "rename": (await c.patch(f"/api/v1/teams/{tid}", json={"name": "Renamed"}, headers=auth(role))).status_code,
                "delete": (await c.delete(f"/api/v1/teams/{tid}", headers=auth(role))).status_code,
            }

        member, viewer = await attempts("member"), await attempts("viewer")
        assert member == viewer == {"read": 200, "members": 200, "invite": 403, "set_role": 403, "rename": 403, "delete": 403}
        admin = await attempts("admin")
        assert admin["invite"] == 201 and admin["set_role"] == 200 and admin["rename"] == 200 and admin["delete"] == 403
        r = await h.client.delete(f"/api/v1/teams/{tid}/members/{users['extra']}", headers=auth("admin"))
        assert r.status_code == 204
        assert (await h.client.delete(f"/api/v1/teams/{tid}", headers=auth("owner"))).status_code == 204
