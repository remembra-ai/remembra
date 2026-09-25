# ruff: noqa: E501  (inline SVG and HTML templates read better unwrapped)
"""Build the Remembra brand assets for the marketing site (landing/).

The identity is "Remembra pops": a brain mark with a flat orange baton in its
long fold, and a drawn monoline wordmark whose second e (the e in "mem")
carries the same baton as its crossbar.

This script is the single source of truth for the geometry. It writes:

  landing/brand/mark.svg, mark-dark.svg              brain mark, flattened (no masks)
  landing/brand/lockup-horizontal(-dark).svg         mark + wordmark, side by side
  landing/brand/lockup-stacked(-dark).svg            mark above the wordmark
  landing/brand/geometry.json                        paths the hero canvas rasterises (also inlined in hero.js)
  landing/brand/partials/*.svg                       inline snippets (currentColor + var(--signal))
  landing/favicon.svg                                graphite tile, hand-placed 16 px pixels
  landing/favicon-16.png, favicon-32.png, favicon.ico, apple-touch-icon.png,
  landing/web-app-manifest-192x192.png, web-app-manifest-512x512.png
  landing/favicon-96x96.png, logo.png, logo.svg, logo.jpg, logo-new.jpg, logo-icon.jpg
                                                     (legacy names the changelog and blog use)

The 1200 x 630 social card is scripts/site-social-card.html, rendered by a
browser (see that file).

Requires shapely (geometry) and rsvg-convert + ImageMagick (PNG/ICO). It is a
maintenance tool, not part of the deployed site or the Python package:

    python scripts/site_brand.py
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
LANDING = ROOT / "landing"
BRAND = LANDING / "brand"

INK = "#15171A"  # graphite
STONE = "#E9E8E1"  # stone
SIGNAL = "#FF5B14"  # baton on light
SIGNAL_DARK = "#FF6B2B"  # baton on dark
TILE = "#15171A"  # favicon / app-icon tile

# ---------------------------------------------------------------------------
# Brain: five unequal lobes, facing left.
#   frontal (large, front) · parietal crown (top) · occipital (small, flattened
#   back) · temporal (long, flat underside) · cerebellum (tucked under the back)
# plus the stem. The folds follow the lobes (central, parieto-occipital, the long
# lateral fold and the cerebellum's), none radial, so it never reads as a flower.
# The long lateral fold is the trail; the baton rides in its straight end.
# Coordinates are in a 0..100 design box.
# ---------------------------------------------------------------------------
KNOCK = 4.4  # width of the folds cut out of the silhouette
BATON_W = 5.0  # height of the baton capsule
BATON = ((54.0, 50.0), (69.0, 50.0))  # flat, horizontal, never stepped


Pt = tuple[float, float]


def ellipse(cx: float, cy: float, rx: float, ry: float, rot: float = 0.0) -> Polygon:
    circle = Point(0, 0).buffer(1.0, quad_segs=48)
    shape = affinity.scale(circle, rx, ry, origin=(0, 0))
    shape = affinity.rotate(shape, rot, origin=(0, 0))
    return affinity.translate(shape, cx, cy)


def cubic(p0: Pt, p1: Pt, p2: Pt, p3: Pt, n: int = 40) -> list[tuple[float, float]]:
    pts = []
    for i in range(n + 1):
        t = i / n
        mt = 1 - t
        x = mt**3 * p0[0] + 3 * mt * mt * t * p1[0] + 3 * mt * t * t * p2[0] + t**3 * p3[0]
        y = mt**3 * p0[1] + 3 * mt * mt * t * p1[1] + 3 * mt * t * t * p2[1] + t**3 * p3[1]
        pts.append((x, y))
    return pts


def quad(p0: Pt, p1: Pt, p2: Pt, n: int = 30) -> list[tuple[float, float]]:
    pts = []
    for i in range(n + 1):
        t = i / n
        mt = 1 - t
        pts.append((mt * mt * p0[0] + 2 * mt * t * p1[0] + t * t * p2[0], mt * mt * p0[1] + 2 * mt * t * p1[1] + t * t * p2[1]))
    return pts


# The silhouette, drawn as one closed outline (not a ring of circles): the
# frontal pole at the left, a dip where the central sulcus starts, the
# parietal crown, a dip at the parieto-occipital notch, a flatter occipital
# back, the cerebellum tucked under it, the stem, and the temporal lobe's
# flat underside running forward to its pole.
OUTLINE = (
    "M12 52 C6 47 5 36 10 28 C14 20 24 14 34 12.5 C39 11.8 43.5 13 46 16.5 "
    "C48.5 11.5 53 8.6 60 8.8 C68 9 73.5 13 77 19.5 C79.5 19 83 20 86 24 "
    "C89.5 29 90 38 88 44.5 C87 48 85.5 50.5 83 52.2 C86 55 86.5 61 83 64.5 "
    "C79.5 68 72 69.5 67.5 68 C66.5 72 66.8 76 66 79 C65.3 81.5 60.5 81.8 59.8 79 "
    "C59 75 59.2 70 58.5 66.5 C50 66.5 36 66 27 65 C20.5 64.2 16.5 60 15.5 56 "
    "C14.5 54 13.5 53 12 52 Z"
)

# Folds, as SVG path data (the canvas uses the same strings).
FOLDS = {
    # lateral (Sylvian) fold: opens between the frontal and temporal poles,
    # runs back and ends flat; the baton rides in its straight end
    "trail": "M11 54 C22 52.5 33 55.5 44 52.4 C48.5 51 52 50 56 50 H69",
    # central sulcus: from the dip in the crown, slanting forward
    "central": "M46.2 14 Q46 22 42.5 28.5",
    # parieto-occipital sulcus: from the dip at the back of the crown
    "occipital": "M78.2 19 Q75 25 75.5 32",
    # cerebellum: separated from the occipital and temporal lobes
    "cerebellum": "M62.5 63.2 C65.5 59 73.5 57.2 84.5 53.8",
}


def fold_points(d: str) -> list[tuple[float, float]]:
    """Points along a path made of M, L, C, Q, H and Z commands."""
    tokens = re.findall(r"[MLCQHZ]|-?\d+(?:\.\d+)?", d)
    pts: list[tuple[float, float]] = []
    i = 0
    cur = (0.0, 0.0)
    cmd = "M"
    while i < len(tokens):
        if tokens[i].isalpha():
            cmd = tokens[i]
            i += 1
            if cmd == "Z":
                continue
        if cmd == "M":
            cur = (float(tokens[i]), float(tokens[i + 1]))
            i += 2
            pts.append(cur)
            cmd = "L"
        elif cmd == "L":
            cur = (float(tokens[i]), float(tokens[i + 1]))
            i += 2
            pts.append(cur)
        elif cmd == "C":
            p1 = (float(tokens[i]), float(tokens[i + 1]))
            p2 = (float(tokens[i + 2]), float(tokens[i + 3]))
            p3 = (float(tokens[i + 4]), float(tokens[i + 5]))
            i += 6
            pts += cubic(cur, p1, p2, p3)[1:]
            cur = p3
        elif cmd == "Q":
            p1 = (float(tokens[i]), float(tokens[i + 1]))
            p2 = (float(tokens[i + 2]), float(tokens[i + 3]))
            i += 4
            pts += quad(cur, p1, p2)[1:]
            cur = p2
        elif cmd == "H":
            cur = (float(tokens[i]), cur[1])
            i += 1
            pts.append(cur)
    return pts


def brain_shape() -> Polygon:
    body = Polygon(fold_points(OUTLINE)).buffer(0)
    cuts = unary_union([LineString(fold_points(d)).buffer(KNOCK / 2, quad_segs=16) for d in FOLDS.values()])
    shape = body.difference(cuts)
    # drop specks the cuts may leave behind
    if isinstance(shape, MultiPolygon):
        shape = unary_union([g for g in shape.geoms if g.area > 4])
    return shape.simplify(0.04, preserve_topology=True)


def baton_shape() -> Polygon:
    return LineString(BATON).buffer(BATON_W / 2, quad_segs=24)


def path_d(geom: Polygon | MultiPolygon, tx: float = 0.0, ty: float = 0.0, k: float = 1.0) -> str:
    polys = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    out = []
    for poly in polys:
        for ring in [poly.exterior, *poly.interiors]:
            coords = list(ring.coords)[:-1]
            pts = [f"{(x * k + tx):.2f} {(y * k + ty):.2f}".replace(".00", "") for x, y in coords]
            out.append("M" + " L".join(pts) + "Z")
    return "".join(out)


# ---------------------------------------------------------------------------
# Wordmark: one rounded stroke. The e aperture is opened wider than the first
# draft so the crossbar e still reads at 14-16 px.
# ---------------------------------------------------------------------------
SW = 11
BASE = 94.5
XT = 55.5
CT = 33.5
CY = 75
RX = 17
RY = 19.5
E_TERMINAL_DEG = 56  # where the e's bowl ends below the crossbar (was 38)


def glyph_e(x0: float) -> tuple[str, str, float]:
    cx = x0 + 5.5 + RX
    a = math.radians(E_TERMINAL_DEG)
    ex, ey = cx + RX * math.cos(a), CY + RY * math.sin(a)
    body = f"M{cx + RX} {CY} A{RX} {RY} 0 0 0 {cx - RX} {CY} A{RX} {RY} 0 0 0 {ex:.2f} {ey:.2f}"
    bar = f"M{cx - RX} {CY} H{cx + RX}"
    return body, bar, 2 * RX + SW


def glyph_r_cap(x0: float) -> tuple[str, float]:
    s = x0 + 5.5
    return f"M{s} {BASE} V{CT} H{s + 14} A15.5 15.5 0 0 1 {s + 14} 64.5 H{s} M{s + 13} 64.5 L{s + 28} {BASE}", 14 + 15.5 + SW


def glyph_m(x0: float) -> tuple[str, float]:
    s = x0 + 5.5
    a = 23
    return (
        f"M{s} {BASE} V{XT} M{s} 71 A{a / 2} 15.5 0 0 1 {s + a} 71 V{BASE} M{s + a} 71 A{a / 2} 15.5 0 0 1 {s + 2 * a} 71 V{BASE}",
        2 * a + SW,
    )


def glyph_b(x0: float) -> tuple[str, float]:
    s = x0 + 5.5
    return f"M{s} {CT - 6} V{BASE} M{s} {CY} A{RX} {RY} 0 0 1 {s + 2 * RX} {CY} A{RX} {RY} 0 0 1 {s} {CY}", 2 * RX + SW


def glyph_r(x0: float) -> tuple[str, float]:
    s = x0 + 5.5
    return f"M{s} {BASE} V{XT} M{s} 73 A15 17.5 0 0 1 {s + 15} {XT}", 15 + SW


def glyph_a(x0: float) -> tuple[str, float]:
    cx = x0 + 5.5 + RX
    return f"M{cx + RX} {CY} A{RX} {RY} 0 0 0 {cx - RX} {CY} A{RX} {RY} 0 0 0 {cx + RX} {CY} M{cx + RX} {XT} V{BASE}", 2 * RX + SW


def wordmark() -> tuple[list[str], list[str], float]:
    gap = 5.5
    x = 0.0
    ink: list[str] = []
    sig: list[str] = []
    d, w = glyph_r_cap(x)
    ink.append(d)
    x += w + gap - 2
    body, bar, w = glyph_e(x)
    ink += [body, bar]
    x += w + gap
    d, w = glyph_m(x)
    ink.append(d)
    x += w + gap
    body, bar, w = glyph_e(x)  # the e in "mem": its crossbar is the baton
    ink.append(body)
    sig.append(bar)
    x += w + gap
    for fn in (glyph_m, glyph_b, glyph_r):
        d, w = fn(x)
        ink.append(d)
        x += w + gap
    x -= 3
    d, w = glyph_a(x)
    ink.append(d)
    x += w
    return ink, sig, x


# ---------------------------------------------------------------------------
# SVG writers
# ---------------------------------------------------------------------------
def brain_group(ink: str, sig: str, tx: float = 0.0, ty: float = 0.0, k: float = 1.0) -> str:
    return (
        f'<path fill="{ink}" fill-rule="evenodd" d="{path_d(BRAIN, tx, ty, k)}"/>'
        f'<path fill="{sig}" d="{path_d(BATON_GEOM, tx, ty, k)}"/>'
    )


def word_group(ink: str, sig: str) -> str:
    ink_paths, sig_paths, _ = WORD
    return (
        f'<g fill="none" stroke="{ink}" stroke-width="{SW}" stroke-linecap="round" stroke-linejoin="round">'
        + "".join(f'<path d="{p}"/>' for p in ink_paths)
        + f'</g><g fill="none" stroke="{sig}" stroke-width="{SW}" stroke-linecap="round">'
        + "".join(f'<path d="{p}"/>' for p in sig_paths)
        + "</g>"
    )


def fmt(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def mark_svg(ink: str, sig: str, size: int = 512) -> str:
    minx, miny, maxx, maxy = BRAIN.union(BATON_GEOM).bounds
    side = max(maxx - minx, maxy - miny) + 8
    cx, cy = (minx + maxx) / 2, (miny + maxy) / 2
    vb = f"{fmt(cx - side / 2)} {fmt(cy - side / 2)} {fmt(side)} {fmt(side)}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{vb}" width="{size}" height="{size}" role="img" '
        f'aria-label="Remembra">{brain_group(ink, sig)}</svg>'
    )


# Lockup placement: the baton sits on the e's crossbar axis (y = CY).
LOCK_K = 1.32  # brain scale inside the lockup
LOCK_GAP = 20  # space between the brain and the R


def lockup_h_parts(ink: str, sig: str) -> tuple[str, tuple[float, float, float, float]]:
    minx, miny, maxx, maxy = BRAIN.bounds
    k = LOCK_K
    tx = -LOCK_GAP - 5.5 - maxx * k
    ty = CY - BATON[0][1] * k
    inner = brain_group(ink, sig, tx, ty, k) + word_group(ink, sig)
    _, _, width = WORD
    x0 = tx + minx * k
    y0 = min(ty + miny * k, CT - 6 - SW / 2)
    y1 = max(ty + maxy * k, BASE + SW / 2)
    return inner, (x0, y0, width - x0, y1 - y0)


def lockup_s_parts(ink: str, sig: str) -> tuple[str, tuple[float, float, float, float]]:
    minx, miny, maxx, maxy = BRAIN.union(BATON_GEOM).bounds
    _, _, width = WORD
    k = 2.1
    bw = (maxx - minx) * k
    tx = width / 2 - bw / 2 - minx * k
    top_word = CT - 6 - SW / 2
    ty = top_word - 22 - maxy * k
    inner = brain_group(ink, sig, tx, ty, k) + word_group(ink, sig)
    y0 = ty + miny * k
    return inner, (-SW / 2, y0, width + SW, BASE + SW / 2 - y0)


def wrap(inner: str, vb: tuple[float, float, float, float], scale: float = 2.0, label: str = "Remembra") -> str:
    x, y, w, h = vb
    pad = 6
    x, y, w, h = x - pad, y - pad, w + 2 * pad, h + 2 * pad
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{fmt(x)} {fmt(y)} {fmt(w)} {fmt(h)}" '
        f'width="{round(w * scale)}" height="{round(h * scale)}" role="img" aria-label="{label}">{inner}</svg>'
    )


# ---------------------------------------------------------------------------
# Favicon: a graphite rounded tile. At 16 and 32 px the brain is hand-placed
# on the pixel grid (notches two pixels deep so they survive), with a flat
# orange baton. Larger sizes use the vector mark on the tile.
# ---------------------------------------------------------------------------
FAV16 = [
    "................",
    "................",
    "....###.####....",
    "..#####.######..",
    ".######.####.##.",
    ".###########.##.",
    ".##############.",
    ".##############.",
    "......oooo####..",
    "..#######.###...",
    "...#######.##...",
    ".........##.....",
    ".........##.....",
    "..........#.....",
    "................",
    "................",
]

FAV32 = [
    "................................",
    "................................",
    "................................",
    "................................",
    ".................######.........",
    "..........####...########.......",
    "........######...#########......",
    "......########..##########......",
    ".....########...#########...#...",
    "....#########..##########..##...",
    "...##########..#########...###..",
    "...#####################...###..",
    "...######################.####..",
    "...###########################..",
    "...###########################..",
    "..############################..",
    "...###########################..",
    "...#############.ooooooo#####...",
    ".................ooooooo####....",
    ".................#######........",
    "......################..........",
    "......###############....####...",
    ".......#############...######...",
    "...............######.#####.....",
    "....................###.........",
    "....................###.........",
    "....................###.........",
    "....................###.........",
    ".....................#..........",
    "................................",
    "................................",
    "................................",
]


def pixel_svg(rows: list[str], tile: bool = True) -> str:
    n = len(rows)
    rects = []
    for y, row in enumerate(rows):
        x = 0
        row = row[:n].ljust(n, ".")
        while x < n:
            c = row[x]
            if c in "#o":
                x2 = x
                while x2 < n and row[x2] == c:
                    x2 += 1
                fill = STONE if c == "#" else SIGNAL_DARK
                rects.append(f'<rect x="{x}" y="{y}" width="{x2 - x}" height="1" fill="{fill}"/>')
                x = x2
            else:
                x += 1
    bg = f'<rect width="{n}" height="{n}" rx="{n * 0.22:.1f}" fill="{TILE}"/>' if tile else ""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {n} {n}" width="{n}" height="{n}" '
        f'shape-rendering="crispEdges">{bg}{"".join(rects)}</svg>'
    )


def pixel_inline(rows: list[str]) -> str:
    """Pixel brain without its tile: ink follows the text color, the baton the signal token."""
    n = len(rows)
    ink: list[str] = []
    sig: list[str] = []
    for y, row in enumerate(rows):
        row = row[:n].ljust(n, ".")
        x = 0
        while x < n:
            ch = row[x]
            if ch in "#o":
                x2 = x
                while x2 < n and row[x2] == ch:
                    x2 += 1
                (ink if ch == "#" else sig).append(f"M{x} {y}h{x2 - x}v1h-{x2 - x}z")
                x = x2
            else:
                x += 1
    return (
        f'<svg class="pixbrain" viewBox="2 3 28 27" shape-rendering="crispEdges" aria-hidden="true">'
        f'<path fill="currentColor" d="{"".join(ink)}"/><path fill="var(--signal)" d="{"".join(sig)}"/></svg>'
    )


def tile_svg(size: int, inset: float = 0.16, radius: float = 0.22) -> str:
    """Vector mark on a graphite rounded tile (apple-touch, manifest icons)."""
    minx, miny, maxx, maxy = BRAIN.union(BATON_GEOM).bounds
    span = max(maxx - minx, maxy - miny)
    k = (100 * (1 - 2 * inset)) / span
    tx = 50 - (minx + maxx) / 2 * k
    ty = 50 - (miny + maxy) / 2 * k
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="{size}" height="{size}">'
        f'<rect width="100" height="100" rx="{100 * radius:.0f}" fill="{TILE}"/>'
        f"{brain_group(STONE, SIGNAL_DARK, tx, ty, k)}</svg>"
    )


def render_png(svg: str, out: Path, size: int) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as fh:
        fh.write(svg)
        src = fh.name
    subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(size), "-o", str(out), src], check=True)
    Path(src).unlink()


def main() -> None:
    BRAND.mkdir(parents=True, exist_ok=True)
    (BRAND / "partials").mkdir(exist_ok=True)

    (BRAND / "mark.svg").write_text(mark_svg(INK, SIGNAL))
    (BRAND / "mark-dark.svg").write_text(mark_svg(STONE, SIGNAL_DARK))

    for name, ink, sig in (("", INK, SIGNAL), ("-dark", STONE, SIGNAL_DARK)):
        inner, vb = lockup_h_parts(ink, sig)
        (BRAND / f"lockup-horizontal{name}.svg").write_text(wrap(inner, vb))
        inner, vb = lockup_s_parts(ink, sig)
        (BRAND / f"lockup-stacked{name}.svg").write_text(wrap(inner, vb))

    # Inline snippets for the pages: ink follows the text color, the baton the signal token.
    inner, vb = lockup_h_parts("currentColor", "var(--signal)")
    x, y, w, h = vb
    pad = 2
    (BRAND / "partials" / "lockup-inline.svg").write_text(
        f'<svg class="lockup" viewBox="{fmt(x - pad)} {fmt(y - pad)} {fmt(w + 2 * pad)} {fmt(h + 2 * pad)}" '
        f'role="img" aria-label="Remembra">{inner}</svg>'
    )
    minx, miny, maxx, maxy = BRAIN.union(BATON_GEOM).bounds
    (BRAND / "partials" / "mark-inline.svg").write_text(
        f'<svg class="mark" viewBox="{fmt(minx - 1)} {fmt(miny - 1)} {fmt(maxx - minx + 2)} {fmt(maxy - miny + 2)}" '
        f'aria-hidden="true">{brain_group("currentColor", "var(--signal)")}</svg>'
    )

    # The 32 px pixel brain, as an inline snippet (constellation core, crew map).
    (BRAND / "partials" / "brain-pixel-inline.svg").write_text(pixel_inline(FAV32))

    # Geometry for the hero canvas: brain + baton in design units, and the
    # wordmark strokes, with the horizontal lockup placement.
    ink_paths, sig_paths, width = WORD
    hminx, hminy, hmaxx, hmaxy = BRAIN.bounds
    geo = {
        "brain": path_d(BRAIN),
        "baton": path_d(BATON_GEOM),
        "batonLine": [list(BATON[0]), list(BATON[1])],
        "brainBox": [hminx, hminy, hmaxx, hmaxy],
        "word": {"ink": ink_paths, "sig": sig_paths, "sw": SW, "width": width, "top": CT - 6 - SW / 2, "bottom": BASE + SW / 2},
        "eBar": [float(sig_paths[0].split()[0][1:]), CY, float(sig_paths[0].split("H")[1]), CY],
        "lockH": {"k": LOCK_K, "tx": -LOCK_GAP - 5.5 - hmaxx * LOCK_K, "ty": CY - BATON[0][1] * LOCK_K},
    }
    geo_json = json.dumps(geo, separators=(",", ":"))
    (BRAND / "geometry.json").write_text(geo_json)
    # The hero canvas carries the geometry inline (no extra request on first paint).
    hero = LANDING / "hero.js"
    if hero.exists():
        text = hero.read_text()
        text = re.sub(r"/\*@geometry\*/.*?/\*@end\*/", lambda _: f"/*@geometry*/{geo_json}/*@end*/", text, count=1, flags=re.S)
        hero.write_text(text)

    # Favicons
    fav16 = pixel_svg(FAV16)
    fav32 = pixel_svg([r[:32] for r in FAV32])
    (LANDING / "favicon.svg").write_text(fav32)
    render_png(fav16, LANDING / "favicon-16.png", 16)
    render_png(fav32, LANDING / "favicon-32.png", 32)
    render_png(fav32, BRAND / "favicon-48.png", 48)
    render_png(tile_svg(180, inset=0.17, radius=0.0), LANDING / "apple-touch-icon.png", 180)
    render_png(tile_svg(192, inset=0.2), LANDING / "web-app-manifest-192x192.png", 192)
    render_png(tile_svg(512, inset=0.2), LANDING / "web-app-manifest-512x512.png", 512)
    (BRAND / "app-icon.svg").write_text(tile_svg(512))
    # Older pages (changelog, blog) and outside links still ask for these names.
    render_png(tile_svg(96, inset=0.16), LANDING / "favicon-96x96.png", 96)
    render_png(tile_svg(512, inset=0.18), LANDING / "logo.png", 512)
    (LANDING / "logo.svg").write_text(tile_svg(64, inset=0.16))
    magick_bin = shutil.which("magick") or "convert"
    for name, size in (("logo.jpg", 800), ("logo-new.jpg", 512), ("logo-icon.jpg", 192)):
        tmp = BRAND / f".{name}.png"
        render_png(tile_svg(size, inset=0.18, radius=0.0), tmp, size)
        subprocess.run([magick_bin, str(tmp), "-quality", "92", str(LANDING / name)], check=True)
        tmp.unlink()
    magick = shutil.which("magick") or "convert"
    subprocess.run(
        [
            magick,
            str(LANDING / "favicon-16.png"),
            str(LANDING / "favicon-32.png"),
            str(BRAND / "favicon-48.png"),
            str(LANDING / "favicon.ico"),
        ],
        check=True,
    )
    (BRAND / "favicon-48.png").unlink()
    print("brand assets written to", BRAND)


BRAIN = brain_shape()
BATON_GEOM = baton_shape()
WORD = wordmark()

if __name__ == "__main__":
    main()
