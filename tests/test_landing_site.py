"""Checks for the marketing site in ``landing/`` (served by nginx, landing/nginx.conf).

The site is plain HTML, so these tests guard what can silently break it:
internal links and #anchors must resolve under the rules in
``landing/nginx.conf`` (clean URLs plus redirects, applied by
``scripts/site_nginx.py``), and the rebuilt home and pricing pages may only
load external assets from Google Fonts.

They also hold the pages to what the product does today: the claims in the
copy, the facts the trail demo shows for each agent, the focus-ring contrast,
and the demo's behavior, which runs the real ``relay-demo.js`` against the
real markup in Node (``tests/js/relay_demo_harness.js``).
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest

ROOT_DIR = Path(__file__).resolve().parent.parent
LANDING = ROOT_DIR / "landing"


class _Collector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.refs: list[tuple[str, str, str]] = []  # (tag, attr, url)
        self.ids: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v for k, v in attrs if v is not None}
        if "id" in a:
            self.ids.add(a["id"])
        if tag == "a" and "name" in a:
            self.ids.add(a["name"])
        for key in ("href", "src"):
            if key in a and not (tag == "link" and a.get("rel") in ("preconnect", "canonical")):
                self.refs.append((tag, key, a[key]))


def _parse(path: Path) -> _Collector:
    c = _Collector()
    c.feed(path.read_text(encoding="utf-8", errors="replace"))
    return c


def _nginx() -> Any:
    return _script("site_nginx")


def _resolve(url: str, nginx: Any, locations: Any) -> tuple[Path | str | None, str]:
    """(what nginx ends up sending for a site URL, the #anchor a redirect added).

    The first item is the file served, 'redirect' when a redirect leaves the
    site, or None on a 404. Redirects that stay on the site are followed.
    """
    added = ""
    res = nginx.resolve(url, locations, LANDING)
    for _ in range(5):
        if res.status not in (301, 302, 307, 308) or not res.location:
            break
        if not res.location.startswith("/"):
            return "redirect", added
        added = urlsplit(res.location).fragment or added
        res = nginx.resolve(res.location, locations, LANDING)
    if res.status == 200 and res.file is not None:
        return Path(res.file), added
    return None, added


def _internal_link_problems() -> list[str]:
    nginx = _nginx()
    locations = nginx.load(LANDING / "nginx.conf")
    ids_cache: dict[Path, set[str]] = {}
    problems: list[str] = []
    for page in sorted(LANDING.rglob("*.html")):
        if "node_modules" in page.parts:
            continue
        rel_page = page.relative_to(LANDING).as_posix()
        for _tag, attr, url in _parse(page).refs:
            parts = urlsplit(url)
            if parts.scheme or url.startswith(("//", "mailto:", "tel:", "javascript:", "data:")):
                continue
            target: Path | str | None
            if url.startswith("#"):
                target, frag = page, url[1:]
            else:
                if parts.path.startswith("/"):
                    path = parts.path
                else:
                    base = "/" if page.parent == LANDING else "/" + page.parent.relative_to(LANDING).as_posix() + "/"
                    path = base + parts.path
                added = ""
                if not parts.path:
                    target = page
                else:
                    query = f"?{parts.query}" if parts.query else ""
                    # A redirect may add its own #anchor (/cookies -> /privacy#cookies); check it too.
                    target, added = _resolve(path + query, nginx, locations)
                frag = parts.fragment or added
                if target is None:
                    problems.append(f"{rel_page}: {attr}={url} does not resolve")
                    continue
            if frag and isinstance(target, Path) and target.suffix == ".html":
                if target not in ids_cache:
                    ids_cache[target] = _parse(target).ids
                if frag not in ids_cache[target]:
                    problems.append(f"{rel_page}: {attr}={url} has no #{frag} target")
    return problems


def test_every_internal_link_resolves() -> None:
    assert _internal_link_problems() == []


def test_link_checker_catches_a_broken_link(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The checker itself must fail on a missing page and a missing anchor, and follow redirects."""
    (tmp_path / "nginx.conf").write_text(
        "server {\n"
        "  location = /old { return 301 /ok; }\n"
        "  location = /gone { return 301 /missing; }\n"
        "  location = /away { return 301 https://docs.remembra.dev/; }\n"
        "  location / { try_files $uri $uri.html $uri/index.html =404; }\n"
        "}\n"
    )
    (tmp_path / "index.html").write_text(
        '<a href="/pricing">p</a><a href="/#nope">x</a><a href="/ok#here">y</a>'
        '<a href="/old#here">r</a><a href="/gone">g</a><a href="/away">a</a>'
    )
    (tmp_path / "ok.html").write_text('<h2 id="here">ok</h2>')
    monkeypatch.setattr(sys.modules[__name__], "LANDING", tmp_path)
    problems = _internal_link_problems()
    assert problems == [
        "index.html: href=/pricing does not resolve",
        "index.html: href=/#nope has no #nope target",
        "index.html: href=/gone does not resolve",
    ]


@pytest.mark.parametrize("page", ["index.html", "pricing.html"])
def test_new_pages_load_external_assets_only_from_google_fonts(page: str) -> None:
    allowed = ("https://fonts.googleapis.com/", "https://fonts.gstatic.com/")
    external_assets = [
        url
        for tag, attr, url in _parse(LANDING / page).refs
        if (tag in ("script", "img", "link", "iframe") and url.startswith(("http:", "https:", "//")))
        and not (tag == "link" and attr == "href" and url.startswith(allowed))
    ]
    assert external_assets == []


def test_signup_links_point_at_the_dashboard_signup_route() -> None:
    for page in ("index.html", "pricing.html"):
        hrefs = [url for tag, _a, url in _parse(LANDING / page).refs if tag == "a"]
        assert "https://app.remembra.dev/signup" in hrefs, page


# ---------------------------------------------------------------------------
# Deploy gates: what the pages depend on that does not exist yet
# ---------------------------------------------------------------------------

RELAY_GATE = (
    "<!-- requires the PyPI release with remembra-relay (remembra>=0.16) and docs.remembra.dev deployed with guides/relay.md -->"
)
BILLING_GATE = "<!-- requires billing: Solo, Pro, Team and Founding 100 live in Paddle and the dashboard -->"
RELAY_GUIDE = "https://docs.remembra.dev/guides/relay/"
# [mcp]: remembra-install points every agent at remembra-mcp, which needs the mcp package.
INSTALL_STEP = "pipx install --force 'remembra[mcp]>=0.16'"
# No key on the command line (shell history keeps it): remembra-install asks for it at a hidden prompt.
KEY_STEP = "remembra-install --all"  # Remembra Cloud by default; a re-run keeps a saved self-hosted server
# The last step writes the hooks: a bare `connect` is a dry run and leaves a new user unconnected.
CONNECT_STEP = "remembra-relay connect --apply"


def _install_blocks(page_html: str) -> list[tuple[str, str, str, str]]:
    """(comment line before, the key-first lead line, the command block, the meta line after) per install block."""
    out = []
    block = re.compile(
        r'(?P<gate>[^\n]*)\n\s*<p class="cmd-meta cmd-lead">(?P<lead>.*?)</p>\s*'
        r'<div class="cmd"(?P<body>.*?)</div>\s*<p class="cmd-meta">(?P<meta>.*?)</p>',
        re.S,
    )
    for m in block.finditer(page_html):
        out.append((m.group("gate").strip(), m.group("lead"), m.group("body"), m.group("meta")))
    return out


def test_every_install_block_has_the_key_step_the_setup_guide_and_the_release_gate() -> None:
    page_html = (LANDING / "index.html").read_text()
    blocks = _install_blocks(page_html)
    assert len(blocks) == 2  # the hero and the Start band
    assert len(re.findall(r'class="cmd[ "]', page_html)) == len(blocks)
    for gate, lead, body, meta in blocks:
        assert gate == RELAY_GATE
        # The key comes first, above the commands, then the commands save it and write the hooks.
        assert 'href="https://app.remembra.dev/signup"' in lead
        copied = html.unescape(re.search(r'data-copy="([^"]*)"', body).group(1)).split("\n")
        assert copied == [INSTALL_STEP, KEY_STEP, CONNECT_STEP]
        shown = _text(re.search(r"<code>.*?</code>", body, re.S).group(0))
        assert shown == f"$ {INSTALL_STEP} $ {KEY_STEP} $ {CONNECT_STEP}"
        assert f'href="{RELAY_GUIDE}"' in meta
        assert "hidden prompt" in meta


def test_paid_prices_sit_behind_the_billing_gate() -> None:
    home = (LANDING / "index.html").read_text()
    assert f'{BILLING_GATE}\n        <div class="prices">' in home
    pricing = (LANDING / "pricing.html").read_text()
    assert f'{BILLING_GATE}\n    <div class="founding">' in pricing
    assert pricing.index(BILLING_GATE) < pricing.index("Claim a founding seat") < pricing.index("Start with Solo")


# ---------------------------------------------------------------------------
# Copy claims
# ---------------------------------------------------------------------------


def _text(fragment: str) -> str:
    return " ".join(html.unescape(re.sub(r"<[^>]+>", " ", fragment)).split())


SIGNED_FREE = ["index.html", "pricing.html", "crew.html", "security.html", "relay-demo.js", "hero.js", "constellation.js"]


def test_no_page_calls_handoffs_signed() -> None:
    """A handoff is attributed, not signed: nothing on the site may call it signed."""
    for name in SIGNED_FREE:
        text = (LANDING / name).read_text()
        assert not re.search(r"\bsigned\b|\bsigns\b", text, re.I), name
    crew_section = (Path(__file__).resolve().parent.parent / "scripts" / "site-crew-section.html").read_text()
    assert not re.search(r"\bsigned\b|\bsigns\b", crew_section, re.I)


def test_trust_row_claims_only_what_a_handoff_records() -> None:
    home = (LANDING / "index.html").read_text()
    trust = _text(re.search(r'<dl class="trust".*?</dl>', home, re.S).group(0))
    assert "machine" not in trust
    assert "Give each agent its own scoped key and it can't write as another." in trust
    assert "records the agent, session and time" in trust


def test_lede_does_not_promise_a_handoff_after_a_closed_lid() -> None:
    lede = _text(re.search(r'<p class="lede">.*?</p>', (LANDING / "index.html").read_text(), re.S).group(0))
    assert "picks up from its handoff" in lede
    assert "lid" not in lede


def test_agents_note_calls_the_unrun_hooks_unverified() -> None:
    note = _text(re.search(r'<p class="fine" id="agents-note">.*?</p>', (LANDING / "index.html").read_text(), re.S).group(0))
    assert "Claude Code's and Codex's session hooks are verified" in note
    assert "run /hooks once" in note  # Codex skips untrusted hooks silently
    assert "unverified" in note and "beta" not in note.lower()
    assert "through Remembra's MCP tools" in note
    # what remembra-relay connect really does (relay/cli.py): a dry run until --apply, unverified adapters opt-in
    assert "dry run until you add --apply" in note and "--include-unverified" in note
    from remembra.relay.adapters import REGISTRY

    specs = [adapter.spec for adapter in REGISTRY.values()]
    unverified = [spec for spec in specs if not spec.verified]
    assert [spec.name for spec in specs if spec.verified] == ["claude-code", "codex"]
    assert "codex-cli 0.155" in note and REGISTRY["codex"].spec.notes.startswith("Verified with codex-cli 0.155.")
    # only a prerelease was run, through codex exec against a local model stand-in (codex.TESTED_VERSIONS)
    assert "Codex with a prerelease of codex-cli 0.155 (" in note and "codex exec" in note
    assert "with a local stand-in for the model)" in note
    assert "other Codex versions have not been run" in note and "live round trip" not in note
    assert len(unverified) == 4  # Cursor, Gemini CLI, Qwen Code and Kimi, as the note names them
    assert "hooks for Cursor, Gemini CLI, Qwen Code and Kimi are unverified" in note
    # which agents the second install line really writes MCP config for (tools/agents.py)
    from remembra.tools.agents import AGENT_CONFIGS

    assert set(AGENT_CONFIGS) == {"claude-desktop", "claude-code", "codex", "cursor", "gemini", "windsurf"}
    assert "remembra-install sets up those tools for Claude Code, Claude Desktop, Codex, Cursor and Gemini CLI." in note
    from remembra.tools.agents import UNVERIFIED_AGENTS

    assert set(UNVERIFIED_AGENTS) == {"windsurf"}  # --all leaves it out: the note must not claim it
    assert "Windsurf, Qwen Code and Kimi you add by hand" in note
    assert "guides/relay/#mcp-by-hand" in (LANDING / "index.html").read_text()
    assert "{#mcp-by-hand}" in (Path(__file__).resolve().parent.parent / "docs" / "guides" / "relay.md").read_text()


def _plan_card(page: str, plan_id: str) -> str:
    return re.search(rf'<article class="plan[^"]*" aria-labelledby="{plan_id}">(.*?)</article>', page, re.S).group(1)


def test_pricing_shows_the_credits_the_code_grants_monthly_and_yearly() -> None:
    """Monthly plans refill every month; yearly plans bank the whole year, unlocked in full after 14 days."""
    from remembra.cloud.plans import PLANS, BillingInterval, PlanTier
    from remembra.config import Settings

    assert Settings.model_fields["annual_credit_upfront_months"].default == 12  # the whole bank once unlocked
    assert Settings.model_fields["annual_credit_unlock_days"].default == 14  # R-27
    assert Settings.model_fields["annual_credit_initial_months"].default == 1
    pricing = (LANDING / "pricing.html").read_text()
    free = PLANS[PlanTier.FREE]
    assert f"<b>{free.max_smart_credits_per_month:,}</b> credits every month" in _plan_card(pricing, "p-free")
    for tier, plan_id, per_seat in (
        (PlanTier.SOLO, "p-solo", ""),
        (PlanTier.PRO, "p-pro", ""),
        (PlanTier.TEAM, "p-team", " per seat"),
    ):
        plan = PLANS[tier]
        card = _plan_card(pricing, plan_id)
        month = plan.credit_allowance(BillingInterval.MONTH)
        year = plan.credit_allowance(BillingInterval.YEAR)
        assert year == 12 * month
        pooled = ", pooled" if plan.per_seat else ""
        up_front = ", pooled" if plan.per_seat else ", in one bank"
        assert f'<span class="m-only"><b>{month:,}</b> credits{per_seat} every month{pooled}</span>' in card, tier
        assert f'<span class="y-only"><b>{year:,}</b> credits{per_seat} for the year{up_front}</span>' in card, tier
    text = _text(pricing)
    assert "Twelve months of credits (26,400 on Solo) go into one bank" in text
    assert "day one" not in text and "up front" not in text
    assert PLANS[PlanTier.SOLO].credit_allowance(BillingInterval.YEAR) == 26_400
    assert "credit bank" in text and "refill" not in text.split("A yearly credit bank")[1][:40]


def test_pricing_numbers_are_the_plans_in_plans_py() -> None:
    from remembra.cloud.plans import FOUNDING_ANNUAL_PRICE_CENTS, PLANS, PlanTier

    pricing = (LANDING / "pricing.html").read_text()

    def usd(cents: int) -> str:
        return f"${cents // 100:,}" if cents % 100 == 0 else f"${cents / 100:,.2f}"

    free = _plan_card(pricing, "p-free")
    assert '<p class="price">$0</p>' in free and PLANS[PlanTier.FREE].price_monthly_cents == 0
    for tier, plan_id, unit in ((PlanTier.SOLO, "p-solo", ""), (PlanTier.PRO, "p-pro", ""), (PlanTier.TEAM, "p-team", "/seat")):
        plan = PLANS[tier]
        card = _plan_card(pricing, plan_id)
        assert plan.price_monthly_cents is not None and plan.price_annual_cents is not None
        assert f'<span class="m-only">{usd(plan.price_monthly_cents)}<small>{unit}/mo</small></span>' in card, tier
        assert f'<span class="y-only">{usd(plan.price_annual_cents)}<small>{unit}/yr</small></span>' in card, tier
        assert plan.price_annual_cents == 10 * plan.price_monthly_cents  # "2 months free"
        assert "2 months free" in card or plan.per_seat  # the Team card spends that line on the seat minimum
        assert f"{plan.max_memories:,} notes" in card, tier
        assert f"{plan.max_recalls_per_month:,} searches" in card, tier
        assert f"{plan.max_relay_events_per_month:,}" in card, tier
    team = PLANS[PlanTier.TEAM]
    team_card = _text(_plan_card(pricing, "p-team"))
    assert f"{team.min_seats}-seat minimum ({usd(team.min_seats * team.price_annual_cents)}/yr)" in team_card
    assert f"{team.min_seats}-seat minimum ({usd(team.min_seats * team.price_monthly_cents)}/mo)" in team_card
    free_plan = PLANS[PlanTier.FREE]
    free_text = _text(free)
    assert f"{free_plan.max_projects} projects" in free_text and f"{free_plan.max_memories:,} notes kept" in free_text
    assert f"{free_plan.max_recalls_per_month:,} searches a month" in free_text
    assert f"fair use {free_plan.max_relay_events_per_month:,} a month" in free_text
    # Founding 100: Solo, yearly only, locked price
    founding = _text(re.search(r'<div class="founding">.*?</div>\s*<a', pricing, re.S).group(0))
    assert f"billed {usd(FOUNDING_ANNUAL_PRICE_CENTS)} yearly" in founding
    assert f"Solo for {usd(FOUNDING_ANNUAL_PRICE_CENTS // 12)}/mo" in founding
    # the home page's price strip says the same
    home = html.unescape(re.search(r'<div class="prices">.*?</div>', (LANDING / "index.html").read_text(), re.S).group(0))
    for tier in (PlanTier.SOLO, PlanTier.PRO):
        assert f"{usd(PLANS[tier].price_monthly_cents or 0)} a month" in home
    assert f"Team {usd(team.price_monthly_cents or 0)} a seat a month" in home
    assert f"{usd(FOUNDING_ANNUAL_PRICE_CENTS)} yearly" in home


# ---------------------------------------------------------------------------
# Focus ring contrast (WCAG 1.4.11: 3:1 against the adjacent color)
# ---------------------------------------------------------------------------


def _luminance(hex_color: str) -> float:
    h = hex_color.lstrip("#")
    channels = [int(h[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"(--[\w-]+):\s*(#[0-9A-Fa-f]{6})\b", block))


def _themes() -> dict[str, dict[str, str]]:
    """Dark is the default for every visitor (the :root block); light is the toggle's data-theme="light"."""
    css = (LANDING / "site.css").read_text()
    dark = _tokens(re.search(r'^:root,\n:root\[data-theme="dark"\] \{(.*?)^\}', css, re.S | re.M).group(1))
    light = _tokens(re.search(r'^:root\[data-theme="light"\] \{(.*?)^\}', css, re.S | re.M).group(1))
    assert "prefers-color-scheme" not in css  # no system block that could override the tokens
    assert set(light) == set(dark)  # each theme sets every color token
    return {"light": light, "dark": dark}


def test_focus_rings_clear_3_to_1_in_both_themes() -> None:
    css = (LANDING / "site.css").read_text()
    home = (LANDING / "home.css").read_text()
    ring = re.search(r"^:focus-visible \{ outline: 2px solid var\((--[\w-]+)\)", css, re.M).group(1)
    copy_ring = re.search(r"^\.copy:focus-visible \{[^}]*outline-color: (#[0-9A-Fa-f]{6})", css, re.M).group(1)
    cmd_bgs = re.findall(r"^(?::root\[data-theme=\"light\"\] )?\.cmd \{[^}]*background: (#[0-9A-Fa-f]{6})", css, re.M | re.S)
    band_ring = re.search(r"^\.band :focus-visible \{ outline-color: var\((--[\w-]+)\)", home, re.M).group(1)
    band_cmd_ring = re.search(r"^\.band \.cmd :focus-visible \{ outline-color: (#[0-9A-Fa-f]{6})", home, re.M).group(1)
    band_cmd_bg = re.search(r"^\.band \.cmd \{[^}]*background: (#[0-9A-Fa-f]{6})", home, re.M).group(1)
    assert len(cmd_bgs) == 2  # the command block on dark paper and on light paper
    for name, t in _themes().items():
        for surface in ("--paper", "--panel", "--paper-2"):
            assert _contrast(t[ring], t[surface]) >= 3, (name, ring, surface)
        assert _contrast(t[band_ring], t["--signal"]) >= 3, (name, "band")
    for bg in cmd_bgs:
        assert _contrast(copy_ring, bg) >= 3, ("copy", bg)  # the copy button sits inside .cmd in both themes
    assert _contrast(band_cmd_ring, band_cmd_bg) >= 3


def test_install_block_never_hides_a_command_under_the_copy_button() -> None:
    """Measured in a browser at 320, 375 and 414 px: the first line ran under the copy button and '$' wrapped
    onto its own row, and JetBrains Mono drew >= as one glyph. These rules are what fixed it."""
    css = (LANDING / "site.css").read_text()
    code = re.search(r"^\.cmd code \{([^}]*)\}", css, re.M).group(1)
    assert "overflow-x: auto" in code  # a long line scrolls inside the box, never under the button
    assert re.search(r"^\.cmd code \.ln \{[^}]*flex-wrap: nowrap", css, re.M)  # '$' stays with its command
    phone = css.split("@media (max-width: 520px)", 1)[1].split("@media", 1)[0]
    assert ".cmd { flex-direction: column; }" in phone  # the copy button goes under the lines on a phone
    mono = re.search(r"^code, kbd, pre, samp \{([^}]*)\}", css, re.M).group(1)
    assert "font-variant-ligatures: none" in mono and '"calt" 0' in mono
    dashboard = (ROOT_DIR / "dashboard" / "src" / "index.css").read_text()
    rule = re.search(r"code,\s*kbd,\s*pre,\s*samp,\s*\.font-mono \{([^}]*)\}", dashboard).group(1)
    assert "font-variant-ligatures: none" in rule and '"calt" 0' in rule


def test_contrast_check_rejects_the_old_orange_ring_on_light_paper() -> None:
    light = _themes()["light"]
    assert _contrast(light["--signal"], light["--paper"]) < 3
    assert _contrast(light["--signal"], light["--panel"]) < 3


# ---------------------------------------------------------------------------
# The trail demo, run for real in Node
# ---------------------------------------------------------------------------

NODE = shutil.which("node")
HARNESS = Path(__file__).resolve().parent / "js" / "relay_demo_harness.js"
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
HOLD_MS = 6200  # relay-demo.js HOLD
ANIMATION_MS = 7400  # a scenario with a meter, start to finished frame
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
ORDER = ["credits", "usage", "lid", "day"]


class _RelayTree(HTMLParser):
    """The #relay figure as a {"t", "a", "c"} tree for the harness."""

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[dict[str, Any]] = []
        self.root: dict[str, Any] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node: dict[str, Any] = {"t": tag, "a": {k: v or "" for k, v in attrs}, "c": []}
        if self.stack:
            self.stack[-1]["c"].append(node)
        elif node["a"].get("id") == "relay":
            self.root = node
        else:
            return
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if self.stack and self.stack[-1]["t"] == tag:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        if self.stack:
            self.stack[-1]["c"].append(data)


def _relay_tree() -> dict[str, Any]:
    parser = _RelayTree()
    parser.feed((LANDING / "index.html").read_text())
    assert parser.root is not None
    return parser.root


def _demo(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    assert NODE is not None
    payload = {"script": str(LANDING / "relay-demo.js"), "dom": _relay_tree(), "steps": steps}
    run = subprocess.run([NODE, str(HARNESS)], input=json.dumps(payload), capture_output=True, text=True, timeout=60)
    assert run.returncode == 0, run.stderr
    snaps: list[dict[str, Any]] = json.loads(run.stdout)
    return snaps


def _click(sel: str) -> dict[str, Any]:
    return {"do": "click", "sel": sel}


def _wait(ms: int) -> dict[str, Any]:
    return {"do": "advance", "ms": ms}


SNAP = {"do": "snap"}
PAUSE = _click("button.pause")


def test_harness_visibility_model_is_the_page_css() -> None:
    css = (LANDING / "home.css").read_text()
    assert '<link rel="stylesheet" href="home.css">' in (LANDING / "index.html").read_text()
    assert ".relay.is-animating [data-at] { opacity: 0;" in css
    assert ".relay.is-animating [data-at].on { opacity: 1;" in css
    # no other rule may hide a [data-at] element: the harness would not see it
    assert len(re.findall(r"\[data-at\][^{]*\{[^}]*(?:opacity: 0|display: none|visibility: hidden)", css)) == 1
    harness = HARNESS.read_text()
    assert 'animating && !e.classList.contains("on")' in harness


@needs_node
def test_pause_mid_animation_shows_the_finished_frame_and_play_resumes_the_same_tab() -> None:
    mid, paused, played, held = _demo(
        [_click('[data-scn="lid"]'), _wait(1500), SNAP, PAUSE, _wait(10_000), SNAP, PAUSE, _wait(100), SNAP, _wait(HOLD_MS), SNAP]
    )
    # mid-animation, most of the frame is still hidden: the check below can fail
    assert "is-animating" in mid["classes"] and mid["hidden"]

    assert "is-animating" not in paused["classes"]
    assert paused["stage"] == "6"
    assert paused["hidden"] == []
    assert paused["pressed"] == ["lid"] and paused["pause"] == "Play"
    assert paused["meter"] == "offline"  # the meter shows its end state, not a frozen drain
    assert paused["pending"] == 0  # nothing runs while paused

    assert played["pressed"] == ["lid"] and played["pause"] == "Pause"
    assert "is-animating" not in played["classes"] and played["hidden"] == []
    assert held["pressed"] == ["day"]  # only after the hold does it move on


@needs_node
def test_play_after_a_tap_finishes_that_trail_then_returns_to_the_first_tab() -> None:
    tapped, playing, finished, first, second = _demo(
        [
            _click('button[data-slot="to"]'),
            _wait(1000),
            SNAP,
            PAUSE,
            _wait(100),
            SNAP,
            _wait(6000),
            SNAP,
            _wait(5000),
            SNAP,
            _wait(ANIMATION_MS + HOLD_MS),
            SNAP,
        ]
    )
    assert tapped["pause"] == "Play" and tapped["pressed"] == []
    assert tapped["fromAgent"] == "OpenAI Codex"  # the holder closes, a different agent picks up
    assert tapped["toAgent"] != "OpenAI Codex"
    assert playing["pause"] == "Pause" and "is-animating" in playing["classes"]
    assert "is-animating" not in finished["classes"] and finished["hidden"] == []
    assert finished["pressed"] == []
    assert first["pressed"] == ["credits"]
    assert second["pressed"] == ["usage"]  # one step per hold, never two


TEST_RESULT = re.compile(r"\btests? passed\b|\bfailing\b")  # a verdict, not a to-do about tests


HOOKED = {"Claude Code", "OpenAI Codex"}  # the verified adapters (relay/adapters: verified=True)


def _check_agent_facts(s: dict[str, Any]) -> None:
    closer, reader = s["fromAgent"], s["toAgent"]
    tests = [f["text"] for f in s["facts"] if TEST_RESULT.search(f["text"])]
    if closer in HOOKED:
        assert s["kind"].startswith("Handoff")
        assert "· session hook ·" in s["sig"]
        assert s["src"] == "facts from git and the session transcript"
    else:
        assert tests == [], s
        assert "via MCP" in s["sig"] and "session hook" not in s["sig"]
        assert s["src"] in ("declared by the agent, not checked", "the agent's own note, not checked")
    brief = s["toLines"][0]
    if reader in HOOKED:
        assert brief.startswith("›brief: "), s
    else:
        assert brief.startswith("›session_brief (MCP): "), s


@needs_node
def test_only_hooked_agents_handoffs_carry_test_results_and_other_agents_use_mcp() -> None:
    steps: list[dict[str, Any]] = []
    for key in ORDER:
        steps += [_click(f'[data-scn="{key}"]'), _wait(ANIMATION_MS + 100), SNAP]
    for slot in ["to", "to", "from", "to", "past", "to", "to", "from", "to", "to", "to", "to"]:
        steps += [_click(f'button[data-slot="{slot}"]'), _wait(ANIMATION_MS + 100), SNAP]
    snaps = _demo(steps)
    for s in snaps:
        _check_agent_facts(s)
    closers = {s["fromAgent"] for s in snaps}
    assert closers & HOOKED and len(closers - HOOKED) >= 2  # both branches were exercised
    assert any(re.search(r"\btests passed\b", f["text"]) for s in snaps[len(ORDER) :] for f in s["facts"])


@needs_node
def test_lid_tab_shows_the_brief_as_the_relay_renders_it() -> None:
    (s,) = _demo([_click('[data-scn="lid"]'), _wait(ANIMATION_MS + 100), SNAP])
    assert s["kind"] == "Checkpoint 5c21"
    assert s["sig"] == "saved by cursor via MCP · 17:46"
    assert s["stop"] == "lid closed · no handoff written"
    assert s["toLines"][:2] == [
        "›brief: last handoff 44a2 from qwen-code, 11h10m ago",
        "›recent: checkpoint 5c21 from cursor, 1h54m ago",
    ]
    assert [f["cls"] for f in s["facts"]] == ["note", "todo"]


def _static_text(node: Any) -> str:
    if isinstance(node, str):
        return node
    return "".join(_static_text(c) for c in node["c"])


def _find(node: Any, pred: Any) -> list[dict[str, Any]]:
    if isinstance(node, str):
        return []
    found = [node] if pred(node) else []
    for c in node["c"]:
        found += _find(c, pred)
    return found


@needs_node
def test_still_frame_markup_matches_what_the_script_renders() -> None:
    """Without JavaScript (or before it runs) the page shows the markup; it must say what the demo says."""
    tree = _relay_tree()
    (s,) = _demo([SNAP])

    def norm(t: str) -> str:
        return " ".join(t.split())

    def by_key(key: str) -> str:
        return norm(_static_text(_find(tree, lambda n: n["a"].get("data-k") == key)[0]))

    def children_of(node_id: str) -> list[str]:
        (parent,) = _find(tree, lambda n: n["a"].get("id") == node_id)
        return [norm(_static_text(c)) for c in parent["c"] if not isinstance(c, str)]

    for key in ("kind", "sig", "src", "fromAgent", "toAgent", "stop"):
        assert by_key(key) == s[key], key
    assert children_of("facts") == [f["text"] for f in s["facts"]]
    assert children_of("toLines") == s["toLines"]
    assert norm(_static_text(_find(tree, lambda n: n["a"].get("id") == "relay-cap")[0])) == s["caption"]


# ---------------------------------------------------------------------------
# One wording for a handoff, on the trail bus
# ---------------------------------------------------------------------------

TAP_SLOTS = ["to", "to", "from", "to", "past", "to", "to", "from", "to", "to", "to", "to"]


def _every_story() -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for key in ORDER:
        steps += [_click(f'[data-scn="{key}"]'), _wait(ANIMATION_MS + 100), SNAP]
    for slot in TAP_SLOTS:
        steps += [_click(f'button[data-slot="{slot}"]'), _wait(ANIMATION_MS + 100), SNAP]
    return _demo(steps)


@needs_node
def test_trail_bus_marks_every_mcp_handoff_self_declared_and_never_signed() -> None:
    """The hero window, its status strip and the constellation all print ev.title, ev.source and ev.line."""
    snaps = _every_story()
    for s in snaps:
        ev = s["trail"]
        assert ev["stage"] == 6 and ev["still"] is False
        assert not re.search(r"\bsign", json.dumps(ev), re.I), ev
        if ev["kind"] == "Checkpoint":
            assert ev["title"] == "checkpoint.saved"
            assert ev["declared"] is True and ev["source"] == "self-declared"
            assert f"checkpoint {ev['id']} (self-declared)" in ev["line"]
        elif ev["from"]["agent"] in HOOKED:
            assert ev["title"] == "handoff.saved" and ev["via"] == "session hook"
            assert ev["declared"] is False and ev["source"] == "from git"
            assert f"handoff {ev['id']} (facts from git)" in ev["line"]
        else:
            assert ev["title"] == "handoff.saved" and ev["via"] == "MCP"
            assert ev["declared"] is True and ev["source"] == "self-declared"
            assert f"handoff {ev['id']} (self-declared)" in ev["line"]
            assert s["src"] == "declared by the agent, not checked"  # the trail says the same
    kinds = {(s["trail"]["kind"], s["trail"]["declared"]) for s in snaps}
    assert kinds == {("Handoff", False), ("Handoff", True), ("Checkpoint", True)}  # every branch ran


def test_hero_and_constellation_print_the_bus_wording_not_their_own() -> None:
    for name in ("hero.js", "constellation.js"):
        text = (LANDING / name).read_text()
        assert "ev.line" in text, name
        assert "stopped · handoff" not in text and "went offline · checkpoint" not in text, name
    hero = (LANDING / "hero.js").read_text()
    assert "set(win.title, ev.title)" in hero
    assert 'set(win.facts, ev.facts + " · " + ev.source)' in hero


def _by_id(page_html: str, el_id: str) -> str:
    m = re.search(rf'<(\w+)[^>]*\bid="{el_id}"[^>]*>(.*?)</\1>', page_html, re.S)
    assert m, el_id
    return _text(m.group(2))


@needs_node
def test_hero_window_and_strips_ship_the_first_story_still_frame() -> None:
    """Before (or without) JavaScript the window and both strips say what the bus says for the first story."""
    (s,) = _demo([SNAP])
    ev = s["trail"]
    home = (LANDING / "index.html").read_text()
    assert _by_id(home, "hwTitle") == ev["title"]
    assert _by_id(home, "hwVia") == ev["via"]
    assert _by_id(home, "hwFacts") == f"{ev['facts']} · {ev['source']}"
    assert _by_id(home, "heroStatus") == ev["line"]
    assert _by_id(home, "constStatus") == ev["line"]


# ---------------------------------------------------------------------------
# Layout stability: the trail holds its height; the strip clears the window
# ---------------------------------------------------------------------------


@needs_node
def test_trail_reserves_the_height_of_its_tallest_story() -> None:
    """Switching stories (4, 3 or 2 facts, one brief line or two) must not change the trail's height."""
    steps: list[dict[str, Any]] = [SNAP]
    for key in ORDER:
        steps += [_click(f'[data-scn="{key}"]'), _wait(ANIMATION_MS + 100), SNAP]
    first, *stories = _demo(steps)
    heights = [s["contentHeight"] for s in stories]
    assert len(set(heights)) > 1  # the stories really differ, so the reservation matters
    reserved = {s["minHeight"] for s in [first, *stories]}
    assert reserved == {f"{max(heights)}px"}
    # the reservation re-renders every story; the frame on screen is still the current one
    assert [s["pressed"] for s in stories] == [[k] for k in ORDER]
    assert all(s["hidden"] == [] for s in stories)


def _css_block(css: str, selector: str, within: str | None = None) -> str:
    if within is not None:
        start = css.index(within)
        css = css[start : css.index("\n}\n", start)]
    m = re.search(rf"(?m)^\s*{re.escape(selector)} \{{(.*?)\}}", css, re.S)
    assert m, selector
    return m.group(1)


def test_hero_status_strip_sits_in_the_room_right_of_the_handoff_window() -> None:
    css = (LANDING / "home.css").read_text()
    win = _css_block(css, ".hero-win")
    strip = _css_block(css, ".hero-status")
    assert "position: absolute" in win and "width: var(--win-w)" in win and "left: var(--gutter)" in win
    assert "left: calc(var(--gutter) + var(--win-w) + var(--win-gap))" in strip
    assert "right: var(--gutter)" in strip and "margin-inline: auto" in strip
    assert "transform" not in strip  # no centring on the page, which ran under the window
    stacked = "@media (max-width: 960px) {"
    assert "position: relative" in _css_block(css, ".hero-win", within=stacked)
    stacked_strip = _css_block(css, ".hero-status", within=stacked)
    assert "position: relative" in stacked_strip and "min-height" in stacked_strip
    hero = (LANDING / "hero.js").read_text()
    assert "keep clear of two lines" in hero


# ---------------------------------------------------------------------------
# The constellation: one story with the trail, the hero and the status strips
# ---------------------------------------------------------------------------


def _demo_agents() -> list[str]:
    js = (LANDING / "relay-demo.js").read_text()
    return json.loads(re.search(r"var AGENTS = (\[.*?\]);", js).group(1))


def _constellation_nodes() -> list[str]:
    home = (LANDING / "index.html").read_text()
    block = re.search(r'<div class="const" id="constellation">(.*?)<div class="const-foot">', home, re.S).group(1)
    return re.findall(r'<button type="button" class="cnode[^"]*" data-agent="([^"]+)"', block)


def test_constellation_listens_to_the_trail_bus_and_taps_hand_off_through_it() -> None:
    js = (LANDING / "constellation.js").read_text()
    assert "window.RemembraTrail.on(onTrail)" in js  # the same events the trail and the hero draw
    assert 'window.RemembraTrail.handOff(n.getAttribute("data-agent"))' in js  # a tap is a real handoff in the story
    assert "statusEl.textContent = ev.line" in js
    nodes = _constellation_nodes()
    assert len(nodes) == 6 and set(nodes) == set(_demo_agents())  # every tappable agent is one the demo can hand to


@needs_node
def test_a_constellation_tap_moves_the_trail_to_that_agent() -> None:
    first, *after = _demo(
        [SNAP]
        + [
            step
            for agent in ("Cursor", "Gemini CLI", "Claude Code")
            for step in ({"do": "handoff", "agent": agent}, _wait(ANIMATION_MS + 100), SNAP)
        ]
    )
    holder = first["toAgent"]
    for agent, s in zip(("Cursor", "Gemini CLI", "Claude Code"), after, strict=True):
        assert s["toAgent"] == agent and s["trail"]["to"]["agent"] == agent
        assert s["fromAgent"] == holder and s["trail"]["from"]["agent"] == holder  # whoever held the work hands it on
        assert s["trail"]["stage"] == 6 and s["hidden"] == []
        _check_agent_facts(s)
        holder = agent


# ---------------------------------------------------------------------------
# Labels for what is not generally available: crew mode and the connector
# ---------------------------------------------------------------------------


def test_connector_section_is_labelled_beta_and_says_what_is_unverified() -> None:
    home = (LANDING / "index.html").read_text()
    section = re.search(r'<section class="sec" id="anywhere".*?</section>', home, re.S).group(0)
    assert re.search(r'<p class="eyebrow">.*?<span class="tag signal">Beta</span></p>', section)
    note = _text(re.search(r'<p class="fine" id="connector-note">.*?</p>', section, re.S).group(0))
    assert note.startswith("Beta:") and "still verifying it inside the live Claude and ChatGPT apps" in note
    assert "Nothing over the connector edits or deletes memories." in _text(section)
    gate = home[: home.index('id="anywhere"')].rsplit("<section", 1)[0]
    assert (
        "<!-- beta: the connector is built and tested locally; not yet verified inside the live Claude and ChatGPT apps -->"
        in gate
    )


def test_crew_page_is_labelled_part_of_launch_only_by_the_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    partials = _script("site_partials")
    crew = (LANDING / "crew.html").read_text()
    eyebrow = re.compile(r'<p class="eyebrow">Crew mode <!-- @crew-tag -->(.*?)<!-- /@crew-tag --></p>')
    assert eyebrow.search(crew).group(1) == '<span class="tag">Not available yet</span>'  # off: crew is not merged yet
    assert "<!-- requires Crew mode (feat/crew) merged and live before launch -->" in crew
    monkeypatch.setattr(partials, "CREW_LIVE", True)
    live = partials.render(crew)
    assert eyebrow.search(live).group(1) == '<span class="tag signal">Part of launch</span>'
    section = (SCRIPTS / "site-crew-section.html").read_text()
    assert '<span class="tag signal">Part of launch</span>' in section


def _crew_preview_and_plan(page: str) -> str:
    """The strings a shared /crew link and the plan block show: meta, og and the plan section."""
    metas = re.findall(r'<meta (?:name|property)="(?:description|og:[a-z]+|twitter:[a-z]+)" content="([^"]*)">', page)
    plan = re.search(r'<section class="sec" aria-labelledby="plan-title">.*?</section>', page, re.S).group(0)
    return " ".join(metas) + " " + _text(plan)


def test_crew_link_previews_and_plan_do_not_say_launch_while_off(monkeypatch: pytest.MonkeyPatch) -> None:
    partials = _script("site_partials")
    crew = (LANDING / "crew.html").read_text()
    off = _crew_preview_and_plan(crew)
    assert "launch" not in off.lower()
    assert "not available yet" in off and "What ships first" in off and "First release" in off
    assert crew.count('<meta name="description"') == 1 and crew.count('<meta property="og:description"') == 1
    monkeypatch.setattr(partials, "CREW_LIVE", True)
    live = partials.render(crew)
    on = _crew_preview_and_plan(live)
    assert "Part of launch" in on and "What ships at launch" in on and "At launch" in on
    assert "not available" not in on.lower()


# ---------------------------------------------------------------------------
# Crew mode: one switch, off until it ships
# ---------------------------------------------------------------------------

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _script(name: str) -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module  # dataclasses in the script look their module up here
    sys.path.insert(0, str(SCRIPTS))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


def test_pages_are_up_to_date_with_the_partials() -> None:
    partials = _script("site_partials")
    for name in [*partials.PAGES, partials.SITEMAP]:
        text = (LANDING / name).read_text()
        rendered = partials.render_sitemap(text) if name == partials.SITEMAP else partials.render(text)
        assert rendered == text, f"{name} is stale: run python scripts/site_partials.py"


def test_crew_mode_is_off_everywhere_until_it_ships() -> None:
    partials = _script("site_partials")
    assert partials.CREW_LIVE is False
    for name in partials.PAGES:
        text = (LANDING / name).read_text()
        assert 'href="/crew"' not in text, name
        assert 'remembra.dev/crew"' not in text or name == "crew.html", name
    assert "/crew" not in (LANDING / "sitemap.xml").read_text()
    home = (LANDING / "index.html").read_text()
    assert 'id="crew"' not in home and "Part of launch" not in home
    assert [int(n) for n in re.findall(r'<p class="eyebrow"><span class="n">(\d\d)</span>', home)] == [1, 2, 3, 4]
    crew = (LANDING / "crew.html").read_text()
    assert '<!-- @crew-robots --><meta name="robots" content="noindex"><!-- /@crew-robots -->' in crew
    assert "Crew mode is still being built and is not available yet." in _text(crew)
    assert "Part of launch" not in _text(crew)


def test_one_switch_brings_every_crew_link_back_and_off_again(monkeypatch: pytest.MonkeyPatch) -> None:
    partials = _script("site_partials")
    committed = {name: (LANDING / name).read_text() for name in [*partials.PAGES, partials.SITEMAP]}

    def render_all() -> dict[str, str]:
        return {n: (partials.render_sitemap(t) if n == partials.SITEMAP else partials.render(t)) for n, t in committed.items()}

    monkeypatch.setattr(partials, "CREW_LIVE", True)
    live = render_all()
    for name in partials.PAGES:
        assert live[name].count('href="/crew"') >= 2, name  # header, menu and footer
    assert "<loc>https://remembra.dev/crew</loc>" in live[partials.SITEMAP]
    home = live["index.html"]
    assert 'id="crew"' in home and 'href="/crew">How crew mode works' in home
    assert [int(n) for n in re.findall(r'<p class="eyebrow"><span class="n">(\d\d)</span>', home)] == [1, 2, 3, 4, 5]
    assert re.search(r'<span class="n">03</span> Crew mode', home)
    assert "noindex" not in live["crew.html"] and "Part of launch" in live["crew.html"]
    assert "not available yet" not in live["crew.html"]

    monkeypatch.setattr(partials, "CREW_LIVE", False)
    again = {n: (partials.render_sitemap(t) if n == partials.SITEMAP else partials.render(t)) for n, t in live.items()}
    assert again == committed  # off and on again is a no-op


# ---------------------------------------------------------------------------
# Pricing: every way a save is refused, from the plan limits the code enforces
# ---------------------------------------------------------------------------


def test_pricing_names_every_limit_that_refuses_a_save() -> None:
    from remembra.cloud.plans import PLANS, PlanTier

    free, solo, pro, team = (PLANS[t] for t in (PlanTier.FREE, PlanTier.SOLO, PlanTier.PRO, PlanTier.TEAM))
    page = (LANDING / "pricing.html").read_text()
    faq = _text(re.search(r'<dl class="faq">.*?</dl>', page, re.S).group(0))
    refused = faq[faq.index("A save is refused only at your plan's limits") :]
    assert "Running out of credits never blocks a save." in faq
    assert f"({free.max_content_chars:,} characters on Free, {solo.max_content_chars:,} on paid plans)" in refused
    assert pro.max_content_chars == team.max_content_chars == solo.max_content_chars
    assert f"the project count ({free.max_projects} on Free; projects with only handoffs are not counted)" in refused
    assert f"the notes per request ({free.max_batch_items} on Free, {solo.max_batch_items} on paid plans)" in refused
    assert f"{free.max_unenriched_writes_per_day} unenriched saves a day" in refused
    assert "notes-kept cap" in refused
    bursts = (free.relay_burst_per_min, solo.relay_burst_per_min, pro.relay_burst_per_min, team.relay_burst_per_min)
    assert "({} a minute on Free, {} on Solo, {} on Pro and Team)".format(*bursts[:3]) in faq and bursts[2] == bursts[3]
    free_card = _text(re.search(r'<article class="plan" aria-labelledby="p-free">.*?</article>', page, re.S).group(0))
    assert f"Notes up to {free.max_content_chars:,} characters" in free_card
    plain = _text(re.search(r'<div class="plain">.*?</div>', page, re.S).group(0))
    assert "8,000 characters is the longest note Free takes" in plain
    assert f"On paid plans, where notes run to {solo.max_content_chars:,} characters" in plain


# ---------------------------------------------------------------------------
# Docs links: every one has a source page, and its deploy is an owner action
# ---------------------------------------------------------------------------


def test_every_docs_link_is_built_from_a_docs_page_and_gated_until_deployed() -> None:
    predeploy = _script("site_predeploy")
    links = predeploy.docs_links()
    assert "https://docs.remembra.dev/guides/relay/" in links
    assert "https://docs.remembra.dev/integrations/claude-and-chatgpt-apps/" in links
    gates = " ".join(predeploy.gates())
    for url in links:
        src = predeploy.source_for(url)
        assert src is not None, f"{url} has no page in docs/ that mkdocs.yml builds"
        # Pages the docs site had long before this relaunch; the online predeploy run still fetches them.
        if src not in ("index.md", "getting-started/docker.md", "getting-started/agent-setup.md", "integrations/mcp-server.md"):
            assert f"docs.remembra.dev deployed with {src}" in gates, url


def test_predeploy_check_fails_on_a_docs_link_that_is_not_live() -> None:
    predeploy = _script("site_predeploy")
    lines, problems = predeploy.check(online=True, fetch=lambda url: 404 if "relay" in url else 200, latest=lambda: "0.16.0")
    assert problems == [
        "https://docs.remembra.dev/guides/relay/ is not live yet (HTTP 404): "
        "deploy the docs site first (index.html, crew.html, changelog.html)"
    ]
    assert any("Crew mode switch" in line for line in lines)
    _, none = predeploy.check(online=True, fetch=lambda url: 200, latest=lambda: "0.16.1")
    assert none == []

    def no_network() -> str:
        raise AssertionError("offline must not ask PyPI")

    _, offline = predeploy.check(online=False, fetch=lambda url: 0, latest=no_network)
    assert offline == []
    assert predeploy.source_for("https://docs.remembra.dev/guides/no-such-page/") is None


# ---------------------------------------------------------------------------
# One mark: the site and the dashboard are built from one geometry
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
DASH_PUBLIC = ROOT / "dashboard" / "public"
BRAND_FILES = [
    "mark.svg",
    "mark-dark.svg",
    "lockup-horizontal.svg",
    "lockup-horizontal-dark.svg",
    "lockup-stacked.svg",
    "lockup-stacked-dark.svg",
    "app-icon.svg",
]


def test_hero_carries_the_brand_geometry_with_its_folds() -> None:
    geo = json.loads((LANDING / "brand" / "geometry.json").read_text())
    hero = (LANDING / "hero.js").read_text()
    inline = re.search(r"/\*@geometry\*/(.*?)/\*@end\*/", hero, re.S)
    assert inline and json.loads(inline.group(1)) == geo  # scripts/brand/build.py keeps the two in step
    assert len(geo["folds"]) == 6 and 4.5 < geo["knock"] < 5.5  # five lobes: six folds, one of them the trail
    assert all(d.startswith("M") for d in geo["folds"])
    (x1, y1), (x2, y2) = geo["batonLine"]
    assert y1 == y2 and x2 > x1  # the baton is flat, never stepped
    fold_cells = float(re.search(r"var FOLD_CELLS = ([\d.]+);", hero).group(1))
    assert fold_cells >= 2.5  # at least two cells open once rasterised at >= 50% coverage
    assert 'cx.globalCompositeOperation = "destination-out"' in hero
    assert "GEO.batonW" in hero and "15 * GEO.lockH.k" not in hero  # the flying baton is sized from the geometry
    # as in the approved hero, the brain is drawn larger beside the wordmark than in the nav lockup
    assert geo["lockHero"]["k"] > geo["lockH"]["k"] and "GEO.lockH." not in hero


def test_site_and_dashboard_ship_the_identical_mark() -> None:
    for name in BRAND_FILES:
        assert (LANDING / "brand" / name).read_bytes() == (DASH_PUBLIC / "brand" / name).read_bytes(), name
    for name in ("favicon.svg", "favicon.ico", "favicon-16.png", "favicon-32.png", "apple-touch-icon.png"):
        assert (LANDING / name).read_bytes() == (DASH_PUBLIC / name).read_bytes(), name
    geo = json.loads((LANDING / "brand" / "geometry.json").read_text())
    ts = (ROOT / "dashboard" / "src" / "brand" / "geometry.ts").read_text()
    assert json.loads(re.search(r"export const MARK_INK = (.*?) as const;", ts).group(1)) == geo["brain"]
    assert json.loads(re.search(r"export const MARK_BATON = (.*?) as const;", ts).group(1)) == geo["baton"]
    assert json.loads(re.search(r"export const WORD_SIG = (.*?) as const;", ts).group(1)) == geo["word"]["sig"]
    partial = (LANDING / "brand" / "partials" / "mark-inline.svg").read_text()
    assert geo["brain"] in partial and geo["baton"] in partial


def test_one_brand_generator() -> None:
    assert (ROOT / "scripts" / "brand" / "geometry.py").is_file() and (ROOT / "scripts" / "brand" / "build.py").is_file()
    assert not (ROOT / "scripts" / "site_brand.py").exists()
    assert not (ROOT / "dashboard" / "brand").exists() or not any((ROOT / "dashboard" / "brand").glob("*.py"))


def test_pixel_grids_keep_the_hinting_rules() -> None:
    grids = _pixel_grids()
    assert set(grids) == {"PIXEL_16", "PIXEL_32"}
    for grid in grids.values():
        n = len(grid)
        assert all(len(row) == n for row in grid)
        assert set("".join(grid)) <= set(" .#o")
        baton_rows = [y for y, row in enumerate(grid) if "o" in row]
        spans = {(row.index("o"), row.rindex("o")) for row in (grid[y] for y in baton_rows)}
        assert len(spans) == 1  # one flat capsule: every baton row starts and ends in the same column
        assert all(set(grid[y][slice(*next(iter(spans)))]) == {"o"} for y in baton_rows)
        start = next(iter(spans))[0]
        assert grid[baton_rows[0]][start - 1] == "."  # the baton sits at the end of an open fold
    svg = (LANDING / "favicon.svg").read_text()
    assert 'viewBox="0 0 16 16"' in svg and 'shape-rendering="crispEdges"' in svg


def _pixel_grids() -> dict[str, list[str]]:
    """The hand-placed grids, read from geometry.py without importing it (it needs shapely)."""
    import ast

    tree = ast.parse((ROOT / "scripts" / "brand" / "geometry.py").read_text())
    return {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id.startswith("PIXEL_")
    }


def test_legal_pages_name_the_billing_provider_the_code_uses() -> None:
    # api/v1/billing.py: Paddle is the sole billing provider, and the Merchant of Record.
    billing = (Path(__file__).resolve().parent.parent / "src" / "remembra" / "api" / "v1" / "billing.py").read_text()
    assert "Paddle is the sole billing provider" in billing
    for name in ("privacy.html", "terms.html"):
        page = _text((LANDING / name).read_text())
        assert "Stripe" not in page, name
        assert "Paddle" in page and "Merchant of Record" in page, name
        assert "Last updated: March 2, 2026" not in page, name


def test_predeploy_check_fails_while_pypi_is_behind_the_install_gate() -> None:
    predeploy = _script("site_predeploy")
    assert predeploy.required_release() == "0.16"  # from the <!-- requires ... remembra>=0.16 --> gates
    lines, problems = predeploy.check(online=True, fetch=lambda url: 200, latest=lambda: "0.13.2")
    assert problems == ["PyPI has remembra 0.13.2; the install lines need remembra>=0.16: release it first"]
    assert "PyPI: remembra 0.13.2 (the install lines need remembra>=0.16)" in lines
    _, unreachable = predeploy.check(online=True, fetch=lambda url: 200, latest=lambda: None)
    assert unreachable == ["PyPI could not be reached to confirm remembra>=0.16 is released"]
    _, ok = predeploy.check(online=True, fetch=lambda url: 200, latest=lambda: "0.16.0")
    assert ok == []


def test_codex_verification_copy_says_what_was_run() -> None:
    """Changelog and guide: the Claude Code half was replayed, the model was a stand-in, the build a prerelease."""
    root = LANDING.parent
    changelog = " ".join((root / "CHANGELOG.md").read_text().split())
    guide = " ".join((root / "docs" / "guides" / "relay.md").read_text().split())
    for text in (changelog, guide):
        assert "0.155.0-alpha.16.4" in text and "prerelease" in text
        assert "replayed through its verified hook path (not a live Claude Code session)" in text
        assert "local stand-in for the model" in text
        assert "live round trip" not in text
    assert "No stable Codex release has been run" in changelog
    assert "no stable Codex release has been run yet" in guide
    assert "| verified (codex-cli 0.155.0-alpha.16.4, a prerelease) |" in guide


# ---------------------------------------------------------------------------
# Prices, refunds and the legal pages
# ---------------------------------------------------------------------------


def _site_text_files() -> list[Path]:
    return [p for p in LANDING.rglob("*") if p.is_file() and p.suffix in (".html", ".js", ".css", ".txt", ".xml", ".json")]


def test_no_retired_price_anywhere_on_the_site() -> None:
    # $49 Pro and $199 Team were the old plans; they must not surface next to the new ones.
    hits = [
        f"{p.relative_to(LANDING)}: {m.group(0)}"
        for p in _site_text_files()
        for m in re.finditer(r"\$(49|199)\b", p.read_text(errors="replace"))
    ]
    assert hits == []


def test_retired_price_check_catches_an_old_price(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "old.html").write_text("<td>$49/mo flat</td>")
    monkeypatch.setattr(sys.modules[__name__], "LANDING", tmp_path)
    with pytest.raises(AssertionError):
        test_no_retired_price_anywhere_on_the_site()


def test_terms_pricing_and_refunds_state_the_same_refund_policy() -> None:
    refunds = _text((LANDING / "refunds.html").read_text())
    terms = _text(re.search(r'<h2 id="payment">.*?<h2', (LANDING / "terms.html").read_text(), re.S).group(0))
    faq = _text(re.search(r"<dt>Can I get a refund\?</dt>.*?</dd>", (LANDING / "pricing.html").read_text(), re.S).group(0))
    for text in (refunds, terms, faq):
        assert "14-day money-back guarantee" in text
        assert "first payment" in text
        assert re.search(r"renewals", text, re.I)
    assert "generally don't offer refunds" not in terms
    assert 'href="/refunds"' in (LANDING / "terms.html").read_text()
    assert 'href="/refunds"' in (LANDING / "pricing.html").read_text()


def test_refund_policy_says_a_refund_cancels_the_subscription_and_the_webhook_does() -> None:
    # "A refund ends the plan" alone let the Paddle subscription renew and charge again.
    refunds = _text((LANDING / "refunds.html").read_text())
    assert "We cancel the subscription with Paddle at the same time, so it does not renew" in refunds
    assert "A chargeback ends the paid plan and cancels the subscription" in refunds
    billing = (Path(__file__).resolve().parent.parent / "src" / "remembra" / "api" / "v1" / "billing.py").read_text()
    refund_handler = billing[billing.index("async def _apply_paddle_refund") :]
    assert "_cancel_refunded_subscription(" in refund_handler
    assert 'effective_from="immediately"' in billing


# Who receives the mail sent to each domain the site gives an address for (MX records, 2026-09-26),
# and the name the subprocessors page must use for that provider.
MAIL_PROVIDERS = {"dolphytech.com": "Google Workspace", "remembra.dev": "Namecheap"}


def test_every_email_address_on_the_site_has_its_mail_provider_named() -> None:
    address = re.compile(r"(?:mailto:|formsubmit\.co/(?:ajax/)?)[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+)")
    domains = {m.group(1).lower() for page in _site_text_files() for m in address.finditer(page.read_text(errors="replace"))}
    assert domains, "no addresses found: the scan is broken"
    # A new domain needs its mail provider added here and on the subprocessors page.
    assert domains <= set(MAIL_PROVIDERS), domains - set(MAIL_PROVIDERS)
    subs = _text((LANDING / "subprocessors.html").read_text())
    privacy = _text((LANDING / "privacy.html").read_text())
    for domain in domains:
        assert MAIL_PROVIDERS[domain] in subs, domain
        assert MAIL_PROVIDERS[domain] in privacy, domain
    # The security page's promise is only true while the list is complete.
    assert "Everyone who handles your data is on the subprocessors page" in _text((LANDING / "security.html").read_text())


def _md_inline(text: str) -> str:
    """Markdown inline text as plain words: drop **, `code` ticks, and [label](url) -> label."""
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    return " ".join(text.replace("**", "").replace("`", "").split())


def test_security_md_on_github_says_what_the_security_page_says() -> None:
    # Buyers read the GitHub Security tab and remembra.dev/security side by side.
    md = (Path(__file__).resolve().parent.parent / "SECURITY.md").read_text()
    page = (LANDING / "security.html").read_text()
    assert "https://remembra.dev/security" in md

    page_rows = [
        tuple(_text(c) for c in re.findall(r"<td>(.*?)</td>", row))
        for row in re.findall(r"<tr>(.*?)</tr>", re.search(r'<h2 id="roadmap">.*?</table>', page, re.S).group(0))
    ]
    md_table = md[md.index("## Roadmap") :].split("\n\n")[1]
    md_rows = [tuple(c.strip() for c in line.strip("|").split("|")) for line in md_table.splitlines()[2:]]
    assert md_rows == [r for r in page_rows if r], md_rows

    def tight(text: str) -> str:  # "(see retention )." from stripped tags reads as "(see retention)."
        return re.sub(r"\s+([).,;])", r"\1", text)

    not_yet = re.search(r'<h2 id="not-yet">.*?</ul>', page, re.S).group(0)
    page_not_yet = [tight(_text(li)) for li in re.findall(r"<li>(.*?)</li>", not_yet, re.S)]
    md_section = md[md.index("## What We Don't Do Yet") : md.index("## Roadmap")]
    md_not_yet = [tight(_md_inline(line[2:])) for line in md_section.splitlines() if line.startswith("- ")]
    assert md_not_yet == page_not_yet

    page_harbor = _text(re.search(r"<h3>Safe harbor</h3>\s*<p>(.*?)</p>", page, re.S).group(1))
    md_harbor = _md_inline(re.search(r"\*\*Safe harbor\.\*\* (.*)", md).group(1))
    assert md_harbor == page_harbor.replace("follow this page", "follow this policy")
    for scope in ("remembra.dev, app.remembra.dev, api.remembra.dev and docs.remembra.dev", "Denial of service, load testing"):
        assert scope in md and scope in _text(page)

    # The withdrawn certification dates must not come back.
    for stale in ("Q3 2026", "SOC 2 Type I |", "HIPAA BAA |", "ISO 27001 | 2027", "Penetration testing (annual)"):
        assert stale not in md, stale


def _sentences(page: str) -> list[str]:
    return re.split(r"(?<=[.!?])\s+", _text((LANDING / page).read_text()))


def test_legal_and_security_pages_claim_no_audit_or_protocol_we_do_not_have() -> None:
    for page in ("privacy.html", "security.html", "terms.html", "subprocessors.html", "dpa.html"):
        text = _text((LANDING / page).read_text())
        assert "TLS 1.3" not in text, page
        assert "Stripe" not in text, page
        for sentence in _sentences(page):
            if re.search(r"SOC 2|penetration test", sentence):
                # Only ever said to say we don't have it, or when we plan it.
                assert re.search(r"\b(no|not|has not|hasn't|don't)\b|Q[1-4] 20\d\d|no date", sentence), (page, sentence)


def test_privacy_names_where_memory_goes_and_links_the_lists() -> None:
    privacy = (LANDING / "privacy.html").read_text()
    text = _text(privacy)
    for must in ("OpenAI", "United States", "Hetzner", "Paddle", "not use API data to train"):
        assert must in text, must
    assert 'href="/subprocessors"' in privacy and 'href="/dpa"' in privacy
    assert "Analytics cookies" not in text


def test_subprocessors_page_names_every_service_the_code_sends_data_to() -> None:
    subs = _text((LANDING / "subprocessors.html").read_text())
    config = (Path(__file__).resolve().parent.parent / "src" / "remembra" / "config.py").read_text()
    # Each service the cloud config can send data to, and the name the page must use for it.
    wired = {
        "openai_api_key": "OpenAI",
        "resend_api_key": "Resend",
        "turnstile": "Cloudflare",
        "typesafe_api_key": "TypeSafe",
        "anthropic_api_key": "Anthropic",
    }
    for setting, name in wired.items():
        assert setting in config, setting  # the check still describes the code
        assert name in subs, name
    for name in ("Hetzner", "Paddle", "Formsubmit", "Google Fonts", "Google Workspace", "Namecheap"):
        assert name in subs, name


def test_every_page_offers_sign_in_next_to_start_free() -> None:
    """Returning users need a way back in: the header has Sign in beside Start free, and the phone menu (where
    header links are hidden) lists it first. The dashboard opens on its sign-in screen for anyone signed out."""
    pages = [p for p in LANDING.rglob("*.html") if "<!-- @header -->" in p.read_text()]
    assert len(pages) >= 13
    for page in pages:
        head = re.search(r"<!-- @header -->(.*?)<!-- /@header -->", page.read_text(), re.S).group(1)
        bar = re.search(r'<nav class="nav-links".*?</nav>', head, re.S).group(0)
        signin = '<a class="nav-link nav-signin" href="https://app.remembra.dev/">Sign in</a>'
        assert f'{signin}\n      <a class="btn-nav" href="https://app.remembra.dev/signup">Start free</a>' in bar, page.name
        menu = re.search(r'<nav class="menu-panel".*?</nav>', head, re.S).group(0)
        assert re.findall(r"<a [^>]*>([^<]+)</a>", menu)[0] == "Sign in", page.name
