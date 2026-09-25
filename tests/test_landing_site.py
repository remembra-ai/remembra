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


def test_no_page_calls_handoffs_signed() -> None:
    for name in ("index.html", "pricing.html", "relay-demo.js"):
        assert not re.search(r"\bsigned\b", (LANDING / name).read_text(), re.I), name


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
