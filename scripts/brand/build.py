# ruff: noqa: E501  (inline SVG templates read better unwrapped)
"""Build every Remembra brand asset, for the site and the dashboard, from one geometry.

    python3 scripts/brand/build.py            # everything, including the social card
    python3 scripts/brand/build.py --no-social

scripts/brand/geometry.py is the single source of the mark (the five-lobe
brain, its folds, the flat baton, the monoline wordmark and the hand-placed
16 and 32 px pixel grids). This script writes it out for both surfaces, so the
marketing site and the dashboard always show the identical mark:

Site (landing/)
  brand/mark.svg, mark-dark.svg, lockup-horizontal(-dark).svg,
  lockup-stacked(-dark).svg, app-icon.svg
  brand/partials/lockup-inline.svg, mark-inline.svg, brain-pixel-inline.svg
                                  (currentColor + var(--signal); site_partials.py inlines them)
  brand/geometry.json             paths the hero canvas rasterises, also inlined in hero.js
  favicon.svg (16 px pixel tile), favicon.ico (16 + 32 pixel, 48 vector),
  favicon-16.png, favicon-32.png, favicon-96x96.png, apple-touch-icon.png,
  web-app-manifest-192x192.png, web-app-manifest-512x512.png (maskable),
  logo.png, logo.svg, logo.jpg, logo-new.jpg, logo-icon.jpg (names older pages and emails use)
  social-preview.png              1200 x 630, scripts/site-social-card.html in headless Chrome

Dashboard (dashboard/)
  public/brand/*.svg              the same seven files as landing/brand
  public/favicon.svg, favicon.ico, favicon-16.png, favicon-32.png,
  apple-touch-icon.png, icon-192.png, icon-512.png, logo.jpg, logo-icon.jpg
  src/brand/geometry.ts           path data the React mark and the pixel canvases draw from

Needs shapely, Pillow, rsvg-convert and ImageMagick; the social card also
needs Google Chrome. Dev machine only: nothing here ships in the package.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import json
import re
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import geometry as g  # noqa: E402

ROOT = HERE.parents[1]
LANDING = ROOT / "landing"
SITE_BRAND = LANDING / "brand"
DASH = ROOT / "dashboard"
DASH_PUBLIC = DASH / "public"
DASH_BRAND = DASH_PUBLIC / "brand"
TS_OUT = DASH / "src" / "brand" / "geometry.ts"
CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

INK = "#15171A"  # graphite
STONE = "#E9E8E1"  # stone
SIGNAL = "#FF5B14"  # baton on light
SIGNAL_DARK = "#FF6B2B"  # baton on dark
TILE = INK  # favicon and app-icon tile

LIGHT = {"ink": INK, "sig": SIGNAL}
DARK = {"ink": STONE, "sig": SIGNAL_DARK}
LIVE = {"ink": "currentColor", "sig": "var(--signal)"}  # inline on the site: follows the theme

MARK_INK, MARK_BATON = g.mark_paths()
BB = g.mark_bbox()
WORD_INK, WORD_SIG, WORD_W = g.wordmark_paths()

# Lockups: the brain's baton sits on the e crossbar line (y = CY).
K_H = 1.3
K_S = 2.2
# The site's hero draws the brain larger beside the wordmark, as the approved
# hero did: at lockup size the pixel brain is only ~38 cells tall and its folds
# close up; at this size every lobe and fold stays open.
K_HERO = 2.1
HERO_GAP = 22


def f(v: float) -> str:
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


# ---------------------------------------------------------------------------
# SVG pieces
# ---------------------------------------------------------------------------
def brain_group(c: dict[str, str], tx: float = 0, ty: float = 0, k: float = 1) -> str:
    t = f' transform="translate({f(tx)} {f(ty)}) scale({f(k)})"' if (tx or ty or k != 1) else ""
    return f'<g{t}><path fill="{c["ink"]}" fill-rule="evenodd" d="{MARK_INK}"/><path fill="{c["sig"]}" d="{MARK_BATON}"/></g>'


def word_group(c: dict[str, str]) -> str:
    ink = "".join(f'<path d="{d}"/>' for d in WORD_INK)
    sig = "".join(f'<path d="{d}"/>' for d in WORD_SIG)
    return (
        f'<g fill="none" stroke="{c["ink"]}" stroke-width="{g.SW}" stroke-linecap="round" stroke-linejoin="round">{ink}</g>'
        f'<g fill="none" stroke="{c["sig"]}" stroke-width="{g.SW}" stroke-linecap="round">{sig}</g>'
    )


def lockup_h_params() -> tuple[float, float, tuple[float, float, float, float]]:
    ty = g.CY - K_H * g.AXIS_Y
    tx = -BB[2] * K_H - 16
    x0 = tx + BB[0] * K_H - 4
    x1 = WORD_W + 4
    y0 = min(22.0, ty + BB[1] * K_H) - 4
    y1 = max(100.0, ty + BB[3] * K_H) + 4
    return tx, ty, (x0, y0, x1 - x0, y1 - y0)


def lockup_s_params() -> tuple[float, float, tuple[float, float, float, float]]:
    tx = (WORD_W - K_S * 100) / 2
    ty = 22 - 14 - K_S * BB[3]
    x0, x1 = -6.0, WORD_W + 6
    y0 = ty + K_S * BB[1] - 6
    return tx, ty, (x0, y0, x1 - x0, 104 - y0)


def svg(view: tuple[float, float, float, float], body: str, w: float, h: float, label: str = "Remembra") -> str:
    vb = " ".join(f(v) for v in view)
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" width="{f(w)}" height="{f(h)}" '
        f'role="img" aria-label="{label}">{body}</svg>\n'
    )


def mark_svg(c: dict[str, str]) -> str:
    return svg((0, 0, 100, 100), brain_group(c), 512, 512)


def lockup_h_svg(c: dict[str, str]) -> str:
    tx, ty, vb = lockup_h_params()
    return svg(vb, brain_group(c, tx, ty, K_H) + word_group(c), vb[2] * 2, vb[3] * 2)


def lockup_s_svg(c: dict[str, str]) -> str:
    tx, ty, vb = lockup_s_params()
    return svg(vb, brain_group(c, tx, ty, K_S) + word_group(c), vb[2] * 1.5, vb[3] * 1.5)


def tile_svg(size: int, fill: float = 0.78, rx: float = 22, bleed: bool = False) -> str:
    """Graphite tile with the stone brain; the brain spans ``fill`` of the tile's width."""
    w = BB[2] - BB[0]
    k = 100 * fill / w
    cx, cy = (BB[0] + BB[2]) / 2, (BB[1] + BB[3]) / 2
    rect = (
        f'<rect width="100" height="100" fill="{TILE}"/>'
        if bleed
        else f'<rect width="100" height="100" rx="{f(rx)}" fill="{TILE}"/>'
    )
    body = (
        f'{rect}<g transform="translate(50 51) scale({f(k)}) translate({f(-cx)} {f(-cy)})">'
        + brain_group({"ink": STONE, "sig": SIGNAL_DARK})
        + "</g>"
    )
    return svg((0, 0, 100, 100), body, size, size, "Remembra app icon")


# ---------------------------------------------------------------------------
# Pixel grids (hand-placed in geometry.py)
# ---------------------------------------------------------------------------
PIXEL_COLORS = {".": TILE, "#": STONE, "o": SIGNAL_DARK}


def _runs(row: str, chars: str) -> list[tuple[int, int, str]]:
    out, x = [], 0
    while x < len(row):
        ch = row[x]
        if ch not in chars:
            x += 1
            continue
        x2 = x
        while x2 < len(row) and row[x2] == ch:
            x2 += 1
        out.append((x, x2 - x, ch))
        x = x2
    return out


def pixel_svg(grid: list[str]) -> str:
    """The favicon: a pixel tile drawn from rects (crisp at 1x and 2x)."""
    n = len(grid)
    rects = [
        f'<rect x="{x}" y="{y}" width="{w}" height="1" fill="{PIXEL_COLORS[ch]}"/>'
        for y, row in enumerate(grid)
        for x, w, ch in _runs(row, ".#o")
    ]
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{n}" height="{n}" shape-rendering="crispEdges">{"".join(rects)}</svg>\n'


def pixel_png(grid: list[str], path: Path) -> None:
    rgba = {k: (*(int(v[i : i + 2], 16) for i in (1, 3, 5)), 255) for k, v in PIXEL_COLORS.items()}
    rgba[" "] = (0, 0, 0, 0)
    n = len(grid)
    im = Image.new("RGBA", (n, n))
    for y, row in enumerate(grid):
        for x, ch in enumerate(row):
            im.putpixel((x, y), rgba[ch])
    im.save(path)


def pixel_inline(grid: list[str]) -> str:
    """The 32 px brain without its tile, for the site: ink follows the text color, the baton the signal token."""
    ink: list[str] = []
    sig: list[str] = []
    xs: list[int] = []
    ys: list[int] = []
    for y, row in enumerate(grid):
        for x, w, ch in _runs(row, "#o"):
            (ink if ch == "#" else sig).append(f"M{x} {y}h{w}v1h-{w}z")
            xs += [x, x + w]
            ys += [y, y + 1]
    x0, y0, x1, y1 = min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1
    return (
        f'<svg class="pixbrain" viewBox="{x0} {y0} {x1 - x0} {y1 - y0}" shape-rendering="crispEdges" aria-hidden="true">'
        f'<path fill="currentColor" d="{"".join(ink)}"/><path fill="var(--signal)" d="{"".join(sig)}"/></svg>'
    )


# ---------------------------------------------------------------------------
# Geometry for code: the site's hero canvas and the dashboard's React mark
# ---------------------------------------------------------------------------
def e_bar() -> list[float]:
    x1, y, x2 = (float(v) for v in re.findall(r"-?\d+(?:\.\d+)?", WORD_SIG[0]))
    return [x1, y, x2, y]


def site_geometry() -> dict[str, object]:
    tx, ty, _ = lockup_h_params()
    (bx1, by), (bx2, _) = g.baton_line()
    return {
        "brain": MARK_INK,
        "baton": MARK_BATON,
        "batonLine": [[round(bx1, 3), round(by, 3)], [round(bx2, 3), round(by, 3)]],
        "batonW": round(g.BATON_W * g.SCALE, 3),
        "brainBox": [round(v, 3) for v in BB],
        "word": {
            "ink": WORD_INK,
            "sig": WORD_SIG,
            "sw": g.SW,
            "width": round(WORD_W, 3),
            "top": g.CT - 6 - g.SW / 2,
            "bottom": g.BASE + g.SW / 2,
        },
        "eBar": e_bar(),
        "lockH": {"k": K_H, "tx": round(tx, 3), "ty": round(ty, 3)},
        "lockHero": {"k": K_HERO, "tx": round(-BB[2] * K_HERO - HERO_GAP, 3), "ty": round(g.CY - K_HERO * g.AXIS_Y, 3)},
        # fold centre lines and their width: at hero scale the canvas cuts
        # them wider so each lobe stays at least two cells apart
        "folds": g.fold_paths(),
        "knock": round(g.KNOCK, 3),
    }


def geometry_ts() -> str:
    h_tx, h_ty, h_vb = lockup_h_params()
    s_tx, s_ty, s_vb = lockup_s_params()
    data = {
        "MARK_INK": MARK_INK,
        "MARK_BATON": MARK_BATON,
        "MARK_BOX": [round(v, 2) for v in BB],
        "AXIS_Y": round(g.AXIS_Y, 3),
        "BATON_X": [round(g.to_mark(g.BATON_X[0], 0)[0], 3), round(g.to_mark(g.BATON_X[1], 0)[0], 3)],
        "BATON_W": round(g.BATON_W * g.SCALE, 3),
        "WORD_INK": WORD_INK,
        "WORD_SIG": WORD_SIG,
        "WORD_W": round(WORD_W, 3),
        "WORD_SW": g.SW,
        "WORD_CY": g.CY,
        "LOCKUP_H": {"tx": round(h_tx, 3), "ty": round(h_ty, 3), "k": K_H, "viewBox": [round(v, 3) for v in h_vb]},
        "LOCKUP_S": {"tx": round(s_tx, 3), "ty": round(s_ty, 3), "k": K_S, "viewBox": [round(v, 3) for v in s_vb]},
    }
    lines = [
        "// Generated by scripts/brand/build.py from scripts/brand/geometry.py. Do not edit by hand.",
        "// The site (landing/) is built from the same geometry, so the two marks are identical.",
        "// The brain is one compound path (fill-rule evenodd); the baton is a flat capsule on the",
        '// lateral fold. In the lockups the baton line (AXIS_Y) sits on the crossbar of the e in "mem".',
        "",
    ]
    lines += [f"export const {key} = {json.dumps(value)} as const;" for key, value in data.items()]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------
def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def rsvg(svg_text: str, out: Path, size: int) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as fh:
        fh.write(svg_text)
        tmp = Path(fh.name)
    try:
        subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(size), str(tmp), "-o", str(out)], check=True)
    finally:
        tmp.unlink()


def jpg(svg_text: str, out: Path, size: int) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        png = Path(tmp) / "x.png"
        rsvg(svg_text, png, size)
        subprocess.run(["magick", str(png), "-background", TILE, "-flatten", "-quality", "92", str(out)], check=True)


def favicon_set(public: Path) -> None:
    """favicon.svg (16 grid), favicon-16/32.png (hand-placed), favicon.ico (16 + 32 + 48)."""
    write(public / "favicon.svg", pixel_svg(g.PIXEL_16))
    pixel_png(g.PIXEL_16, public / "favicon-16.png")
    pixel_png(g.PIXEL_32, public / "favicon-32.png")
    with tempfile.TemporaryDirectory() as tmp:
        p48 = Path(tmp) / "48.png"
        rsvg(tile_svg(48, fill=0.84, rx=16), p48, 48)
        subprocess.run(
            ["magick", str(public / "favicon-16.png"), str(public / "favicon-32.png"), str(p48), str(public / "favicon.ico")],
            check=True,
        )


def brand_svgs(folder: Path) -> None:
    write(folder / "mark.svg", mark_svg(LIGHT))
    write(folder / "mark-dark.svg", mark_svg(DARK))
    write(folder / "lockup-horizontal.svg", lockup_h_svg(LIGHT))
    write(folder / "lockup-horizontal-dark.svg", lockup_h_svg(DARK))
    write(folder / "lockup-stacked.svg", lockup_s_svg(LIGHT))
    write(folder / "lockup-stacked-dark.svg", lockup_s_svg(DARK))
    write(folder / "app-icon.svg", tile_svg(512))


def build_site() -> None:
    brand_svgs(SITE_BRAND)

    tx, ty, vb = lockup_h_params()
    write(
        SITE_BRAND / "partials" / "lockup-inline.svg",
        f'<svg class="lockup" viewBox="{" ".join(f(v) for v in vb)}" role="img" aria-label="Remembra">'
        f"{brain_group(LIVE, tx, ty, K_H)}{word_group(LIVE)}</svg>\n",
    )
    x0, y0, x1, y1 = BB
    write(
        SITE_BRAND / "partials" / "mark-inline.svg",
        f'<svg class="mark" viewBox="{f(x0 - 1)} {f(y0 - 1)} {f(x1 - x0 + 2)} {f(y1 - y0 + 2)}" aria-hidden="true">{brain_group(LIVE)}</svg>\n',
    )
    write(SITE_BRAND / "partials" / "brain-pixel-inline.svg", pixel_inline(g.PIXEL_32) + "\n")

    geo_json = json.dumps(site_geometry(), separators=(",", ":"))
    write(SITE_BRAND / "geometry.json", geo_json)
    hero = LANDING / "hero.js"
    text = hero.read_text()
    text, n = re.subn(r"/\*@geometry\*/.*?/\*@end\*/", lambda _: f"/*@geometry*/{geo_json}/*@end*/", text, count=1, flags=re.S)
    if n != 1:
        raise SystemExit("hero.js has no /*@geometry*/ ... /*@end*/ block")
    hero.write_text(text)

    favicon_set(LANDING)
    rsvg(tile_svg(180, fill=0.74, bleed=True), LANDING / "apple-touch-icon.png", 180)
    # maskable: the brain stays inside the 80% safe circle
    rsvg(tile_svg(192, fill=0.62, bleed=True), LANDING / "web-app-manifest-192x192.png", 192)
    rsvg(tile_svg(512, fill=0.62, bleed=True), LANDING / "web-app-manifest-512x512.png", 512)
    # names older pages, the changelog and the emails still ask for
    rsvg(tile_svg(96), LANDING / "favicon-96x96.png", 96)
    rsvg(tile_svg(512), LANDING / "logo.png", 512)
    write(LANDING / "logo.svg", tile_svg(64))
    jpg(tile_svg(800, bleed=True), LANDING / "logo.jpg", 800)
    jpg(tile_svg(512, bleed=True), LANDING / "logo-new.jpg", 512)
    jpg(tile_svg(192, bleed=True), LANDING / "logo-icon.jpg", 192)


def build_dashboard() -> None:
    brand_svgs(DASH_BRAND)
    favicon_set(DASH_PUBLIC)
    rsvg(tile_svg(180, fill=0.74, bleed=True), DASH_PUBLIC / "apple-touch-icon.png", 180)
    rsvg(tile_svg(192), DASH_PUBLIC / "icon-192.png", 192)
    rsvg(tile_svg(512), DASH_PUBLIC / "icon-512.png", 512)
    jpg(tile_svg(512, bleed=True), DASH_PUBLIC / "logo.jpg", 512)
    jpg(tile_svg(192, bleed=True), DASH_PUBLIC / "logo-icon.jpg", 192)
    write(TS_OUT, geometry_ts())


def build_social_card() -> None:
    """Screenshot scripts/site-social-card.html (the hero canvas, reduced motion) into landing/social-preview.png."""
    if not Path(CHROME).exists():
        print("skipped social-preview.png: Google Chrome not found")
        return

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(ROOT)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/scripts/site-social-card.html"
        subprocess.run(
            [
                CHROME,
                "--headless=new",
                "--force-prefers-reduced-motion",
                "--hide-scrollbars",
                "--window-size=1200,630",
                "--virtual-time-budget=5000",
                f"--screenshot={LANDING / 'social-preview.png'}",
                url,
            ],
            check=True,
            capture_output=True,
        )
    finally:
        server.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--no-social", action="store_true", help="skip the social card (needs Chrome)")
    args = parser.parse_args()
    for tool in ("rsvg-convert", "magick"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} is required")
    build_site()
    build_dashboard()
    if not args.no_social:
        build_social_card()
    print(f"brand assets written for landing/ and dashboard/ (ink path {len(MARK_INK)} chars, baton axis y={g.AXIS_Y:.2f})")


if __name__ == "__main__":
    main()
