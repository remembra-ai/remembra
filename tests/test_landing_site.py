"""Checks for the marketing site in ``landing/`` (deployed by Vercel).

The site is plain HTML, so these tests guard what can silently break it:
internal links and #anchors must resolve under the ``vercel.json`` rules
(cleanUrls plus redirects), and the rebuilt home and pricing pages may only
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
from urllib.parse import unquote, urlsplit

import pytest

LANDING = Path(__file__).resolve().parent.parent / "landing"


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


def _redirect_patterns() -> list[re.Pattern[str]]:
    cfg = json.loads((LANDING / "vercel.json").read_text())
    out = []
    for r in cfg.get("redirects", []):
        src = re.sub(r":[a-z]+\*", ".*", r["source"])
        src = re.sub(r":[a-z]+", "[^/]+", src)
        out.append(re.compile("^" + src + "$"))
    return out


def _resolve(url_path: str, redirects: list[re.Pattern[str]]) -> Path | str | None:
    """Map a site path to the file Vercel serves (cleanUrls), 'redirect', or None."""
    if any(p.match(url_path) for p in redirects):
        return "redirect"
    rel = unquote(url_path).lstrip("/")
    candidates = [LANDING / "index.html"] if rel == "" else [LANDING / rel, LANDING / f"{rel}.html", LANDING / rel / "index.html"]
    return next((c for c in candidates if c.is_file()), None)


def _internal_link_problems() -> list[str]:
    redirects = _redirect_patterns()
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
                target = _resolve(path, redirects) if parts.path else page
                frag = parts.fragment
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
    """The checker itself must fail on a missing page and a missing anchor."""
    (tmp_path / "vercel.json").write_text(json.dumps({"cleanUrls": True, "redirects": []}))
    (tmp_path / "index.html").write_text('<a href="/pricing">p</a><a href="/#nope">x</a><a href="/ok#here">y</a>')
    (tmp_path / "ok.html").write_text('<h2 id="here">ok</h2>')
    monkeypatch.setattr(sys.modules[__name__], "LANDING", tmp_path)
    problems = _internal_link_problems()
    assert problems == [
        "index.html: href=/pricing does not resolve",
        "index.html: href=/#nope has no #nope target",
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

RELAY_GATE = "<!-- requires PyPI release with remembra-relay and the published relay setup guide -->"
BILLING_GATE = "<!-- requires billing: Solo, Pro, Team and Founding 100 live in Paddle and the dashboard -->"
RELAY_GUIDE = "https://docs.remembra.dev/guides/relay/"
KEY_STEP = "remembra-install --all --api-key <your-key>"


def _install_blocks(page_html: str) -> list[tuple[str, str, str]]:
    """(comment line before, the command block, the meta line after) for every install block."""
    out = []
    block = re.compile(
        r'(?P<gate>[^\n]*)\n\s*<div class="cmd steps"(?P<body>.*?)</div>\s*<p class="cmd-meta">(?P<meta>.*?)</p>', re.S
    )
    for m in block.finditer(page_html):
        out.append((m.group("gate").strip(), m.group("body"), m.group("meta")))
    return out


def test_every_install_block_has_the_key_step_the_setup_guide_and_the_release_gate() -> None:
    page_html = (LANDING / "index.html").read_text()
    blocks = _install_blocks(page_html)
    assert len(blocks) == 2  # the hero and the Start band
    assert len(re.findall(r'class="cmd[ "]', page_html)) == len(blocks)
    for gate, body, meta in blocks:
        assert gate == RELAY_GATE
        copied = html.unescape(re.search(r'data-copy="([^"]*)"', body).group(1)).split("\n")
        assert copied == ["pipx install remembra", KEY_STEP, "remembra-relay connect"]
        shown = _text(re.search(r"<code>.*?</code>", body, re.S).group(0))
        assert shown == f"$ pipx install remembra $ {KEY_STEP} $ remembra-relay connect"
        assert f'href="{RELAY_GUIDE}"' in meta
        assert 'href="https://app.remembra.dev/signup"' in meta


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
    note = _text(re.search(r'<p class="fine">.*?</p>', (LANDING / "index.html").read_text(), re.S).group(0))
    assert "Claude Code's session hooks are verified" in note
    assert "unverified" in note and "beta" not in note
    assert "through Remembra's MCP tools" in note


def test_pricing_shows_the_spec_monthly_credits_and_no_yearly_bank() -> None:
    pricing = (LANDING / "pricing.html").read_text()
    text = _text(pricing)
    for invented in ("up front", "credit bank", "26,400", "60,000"):
        assert invented not in text, invented
    assert "<b>2,200</b> credits every month" in pricing
    assert "<b>5,000</b> credits every month" in pricing
    assert "<b>2,200</b> credits per seat every month, pooled" in pricing
    # the credit line is the same for monthly and yearly billing
    assert not re.search(r'class="credits"><span class="[my]-only"', pricing)


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
    css = (LANDING / "site.css").read_text()
    light = _tokens(re.search(r"^:root \{(.*?)^\}", css, re.S | re.M).group(1))
    dark = _tokens(re.search(r'^:root\[data-theme="dark"\] \{(.*?)^\}', css, re.S | re.M).group(1))
    system_block = r'@media \(prefers-color-scheme: dark\) \{\s*:root:not\(\[data-theme="light"\]\) \{(.*?)\}'
    system_dark = _tokens(re.search(system_block, css, re.S).group(1))
    assert dark == system_dark  # the two dark-mode blocks must stay in step
    return {"light": light, "dark": {**light, **dark}}


def test_focus_rings_clear_3_to_1_in_both_themes() -> None:
    css = (LANDING / "site.css").read_text()
    home = (LANDING / "index.html").read_text()
    ring = re.search(r"^:focus-visible \{ outline: 2px solid var\((--[\w-]+)\)", css, re.M).group(1)
    cmd_ring = re.search(r"^\.cmd :focus-visible \{ outline-color: var\((--[\w-]+)\)", css, re.M).group(1)
    band_ring = re.search(r"^\.band :focus-visible \{ outline-color: var\((--[\w-]+)\)", home, re.M).group(1)
    band_cmd_ring = re.search(r"^\.band \.cmd :focus-visible \{ outline-color: (#[0-9A-Fa-f]{6})", home, re.M).group(1)
    band_cmd_bg = re.search(r"^\.band \.cmd \{[^}]*background: (#[0-9A-Fa-f]{6})", home, re.M).group(1)
    for name, t in _themes().items():
        for surface in ("--paper", "--panel", "--paper-2"):
            assert _contrast(t[ring], t[surface]) >= 3, (name, ring, surface)
        assert _contrast(t[cmd_ring], t["--ink"]) >= 3, (name, "cmd")  # .cmd is filled with --ink
        assert _contrast(t[band_ring], t["--signal"]) >= 3, (name, "band")
    assert _contrast(band_cmd_ring, band_cmd_bg) >= 3


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
    home = (LANDING / "index.html").read_text()
    assert ".relay.is-animating [data-at] { opacity: 0;" in home
    assert ".relay.is-animating [data-at].on { opacity: 1;" in home


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


def _check_agent_facts(s: dict[str, Any]) -> None:
    closer, reader = s["fromAgent"], s["toAgent"]
    tests = [f["text"] for f in s["facts"] if TEST_RESULT.search(f["text"])]
    if closer == "Claude Code":
        assert s["kind"].startswith("Handoff")
        assert "· session hook ·" in s["sig"]
        assert s["src"] == "facts from git and the session transcript"
    else:
        assert tests == [], s
        assert "via MCP" in s["sig"] and "session hook" not in s["sig"]
        assert s["src"] in ("declared by the agent, not checked", "the agent's own note, not checked")
    brief = s["toLines"][0]
    if reader == "Claude Code":
        assert brief.startswith("›brief: "), s
    else:
        assert brief.startswith("›session_brief (MCP): "), s


@needs_node
def test_only_claude_code_handoffs_carry_test_results_and_other_agents_use_mcp() -> None:
    steps: list[dict[str, Any]] = []
    for key in ORDER:
        steps += [_click(f'[data-scn="{key}"]'), _wait(ANIMATION_MS + 100), SNAP]
    for slot in ["to", "to", "from", "to", "past", "to", "to", "from", "to", "to", "to", "to"]:
        steps += [_click(f'button[data-slot="{slot}"]'), _wait(ANIMATION_MS + 100), SNAP]
    snaps = _demo(steps)
    for s in snaps:
        _check_agent_facts(s)
    closers = {s["fromAgent"] for s in snaps}
    assert "Claude Code" in closers and len(closers) >= 4  # both branches were exercised
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
        elif ev["from"]["agent"] == "Claude Code":
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
# Crew mode: one switch, off until it ships
# ---------------------------------------------------------------------------

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"


def _script(name: str) -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
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
    assert f"the project count ({free.max_projects} on Free)" in refused
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
        if src not in ("index.md", "getting-started/docker.md"):  # already live before this site
            assert f"docs.remembra.dev deployed with {src}" in gates, url


def test_predeploy_check_fails_on_a_docs_link_that_is_not_live() -> None:
    predeploy = _script("site_predeploy")
    lines, problems = predeploy.check(online=True, fetch=lambda url: 404 if "relay" in url else 200)
    assert problems == [
        "https://docs.remembra.dev/guides/relay/ is not live yet (HTTP 404): deploy the docs site first (index.html, crew.html)"
    ]
    assert any("Crew mode switch" in line for line in lines)
    _, none = predeploy.check(online=True, fetch=lambda url: 200)
    assert none == []
    _, offline = predeploy.check(online=False, fetch=lambda url: 0)
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
