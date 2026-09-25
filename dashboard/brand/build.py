"""Build the dashboard's brand assets from geometry.py.

    python3 dashboard/brand/build.py

Writes (all paths relative to dashboard/):
  public/favicon.svg            16-px pixel tile (hand-hinted grid, crisp at 1x and 2x)
  public/favicon.ico            16 (hinted) + 32 + 48
  public/favicon-16.png, favicon-32.png, apple-touch-icon.png (180), icon-192.png, icon-512.png
  public/brand/*.svg            mark, lockups and app icon, light and dark (compound paths, no masks)
  src/brand/geometry.ts         path data the React components and canvases draw from

Needs shapely, Pillow, rsvg-convert and ImageMagick (dev machine only).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import geometry as g  # noqa: E402

ROOT = os.path.dirname(HERE)
PUBLIC = os.path.join(ROOT, "public")
BRAND = os.path.join(PUBLIC, "brand")
TS_OUT = os.path.join(ROOT, "src", "brand", "geometry.ts")

LIGHT = {"ink": "#15171A", "sig": "#FF5B14"}
DARK = {"ink": "#E9E8E1", "sig": "#FF6B2B"}
TILE = "#1A1D1F"  # graphite
STONE = "#E9E8E1"
SIGNAL = "#FF6B2B"

INK, BATON = g.mark_paths()
BB = g.mark_bbox()
WORD_INK, WORD_SIG, WORD_W = g.wordmark_paths()

# Horizontal lockup: the brain's baton sits on the e crossbar line (y = CY).
K_H = 1.3
K_S = 2.2


def f(v: float) -> str:
    return f"{v:.2f}".rstrip("0").rstrip(".")


def brain_group(c: dict[str, str], tx: float = 0, ty: float = 0, k: float = 1) -> str:
    t = f' transform="translate({f(tx)} {f(ty)}) scale({f(k)})"' if (tx or ty or k != 1) else ""
    return f'<g{t}><path fill="{c["ink"]}" fill-rule="evenodd" d="{INK}"/><path fill="{c["sig"]}" d="{BATON}"/></g>'


def word_group(c: dict[str, str]) -> str:
    ink = "".join(f'<path d="{d}"/>' for d in WORD_INK)
    sig = "".join(f'<path d="{d}"/>' for d in WORD_SIG)
    return (
        f'<g fill="none" stroke="{c["ink"]}" stroke-width="{g.SW}" stroke-linecap="round" stroke-linejoin="round">{ink}</g>'
        f'<g fill="none" stroke="{c["sig"]}" stroke-width="{g.SW}" stroke-linecap="round">{sig}</g>'
    )


def lockup_h_params():
    ty = g.CY - K_H * g.AXIS_Y
    tx = -BB[2] * K_H - 16
    x0 = tx + BB[0] * K_H - 4
    x1 = WORD_W + 4
    y0 = min(22.0, ty + BB[1] * K_H) - 4
    y1 = max(100.0, ty + BB[3] * K_H) + 4
    return tx, ty, (x0, y0, x1 - x0, y1 - y0)


def lockup_s_params():
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


def mark_svg(c):
    return svg((0, 0, 100, 100), brain_group(c), 512, 512)


def lockup_h_svg(c):
    tx, ty, vb = lockup_h_params()
    return svg(vb, brain_group(c, tx, ty, K_H) + word_group(c), vb[2] * 2, vb[3] * 2)


def lockup_s_svg(c):
    tx, ty, vb = lockup_s_params()
    return svg(vb, brain_group(c, tx, ty, K_S) + word_group(c), vb[2] * 1.5, vb[3] * 1.5)


def tile_svg(size: int, rx: float = 22, bleed: bool = False) -> str:
    """Graphite tile with the stone brain; the brain fills 82% of the width."""
    w = BB[2] - BB[0]
    k = 82 / w
    cx, cy = (BB[0] + BB[2]) / 2, (BB[1] + BB[3]) / 2
    rect = f'<rect width="100" height="100" fill="{TILE}"/>' if bleed else f'<rect width="100" height="100" rx="{rx}" fill="{TILE}"/>'
    body = f'{rect}<g transform="translate(50 51) scale({f(k)}) translate({f(-cx)} {f(-cy)})">' + brain_group({"ink": STONE, "sig": SIGNAL}) + "</g>"
    return svg((0, 0, 100, 100), body, size, size, "Remembra app icon")


# Hand-hinted 16 px favicon: '.' graphite tile, '#' stone, 'o' signal, ' ' clear.
# The central sulcus, the parieto-occipital notch and the lateral fold are one
# full pixel wide and open to the outline, so they survive at 1x.
FAV16 = [
    "  ............  ",
    " .............. ",
    "................",
    ".....##.####....",
    "...####.####....",
    "..##########.#..",
    ".###########.##.",
    "..#############.",
    ".##############.",
    ".#.....oooo####.",
    "..############..",
    "...#######.###..",
    "........##.##...",
    "........##......",
    " .............. ",
    "  ............  ",
]


def fav16_svg() -> str:
    colors = {".": TILE, "#": STONE, "o": SIGNAL}
    rects = []
    for y, row in enumerate(FAV16):
        assert len(row) == 16, (y, row)
        x = 0
        while x < 16:
            ch = row[x]
            if ch == " ":
                x += 1
                continue
            x2 = x
            while x2 < 16 and row[x2] == ch:
                x2 += 1
            rects.append(f'<rect x="{x}" y="{y}" width="{x2 - x}" height="1" fill="{colors[ch]}"/>')
            x = x2
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" width="16" height="16" '
        'shape-rendering="crispEdges">' + "".join(rects) + "</svg>\n"
    )


def fav16_png(path: str) -> None:
    colors = {".": (0x1A, 0x1D, 0x1F, 255), "#": (0xE9, 0xE8, 0xE1, 255), "o": (0xFF, 0x6B, 0x2B, 255), " ": (0, 0, 0, 0)}
    im = Image.new("RGBA", (16, 16))
    for y, row in enumerate(FAV16):
        for x, ch in enumerate(row):
            im.putpixel((x, y), colors[ch])
    im.save(path)


def rsvg(svg_text: str, out: str, size: int) -> None:
    with tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False) as fh:
        fh.write(svg_text)
        tmp = fh.name
    try:
        subprocess.run(["rsvg-convert", "-w", str(size), "-h", str(size), tmp, "-o", out], check=True)
    finally:
        os.unlink(tmp)


def write(path: str, text: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


def geometry_ts() -> str:
    h_tx, h_ty, h_vb = lockup_h_params()
    s_tx, s_ty, s_vb = lockup_s_params()
    data = {
        "MARK_INK": INK,
        "MARK_BATON": BATON,
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
        "// Generated by dashboard/brand/build.py from dashboard/brand/geometry.py. Do not edit by hand.",
        "// The brain is one compound path (fill-rule evenodd); the baton is a flat capsule on the",
        "// lateral fold. In the lockups the baton line (AXIS_Y) sits on the crossbar of the e in \"mem\".",
        "",
    ]
    for key, value in data.items():
        lines.append(f"export const {key} = {json.dumps(value)} as const;")
    return "\n".join(lines) + "\n"


def main() -> None:
    for tool in ("rsvg-convert", "magick"):
        if not shutil.which(tool):
            raise SystemExit(f"{tool} is required")
    os.makedirs(BRAND, exist_ok=True)
    write(os.path.join(BRAND, "mark.svg"), mark_svg(LIGHT))
    write(os.path.join(BRAND, "mark-dark.svg"), mark_svg(DARK))
    write(os.path.join(BRAND, "lockup-horizontal.svg"), lockup_h_svg(LIGHT))
    write(os.path.join(BRAND, "lockup-horizontal-dark.svg"), lockup_h_svg(DARK))
    write(os.path.join(BRAND, "lockup-stacked.svg"), lockup_s_svg(LIGHT))
    write(os.path.join(BRAND, "lockup-stacked-dark.svg"), lockup_s_svg(DARK))
    write(os.path.join(BRAND, "app-icon.svg"), tile_svg(512))
    write(os.path.join(PUBLIC, "favicon.svg"), fav16_svg())

    fav16_png(os.path.join(PUBLIC, "favicon-16.png"))
    rsvg(tile_svg(32, rx=6), os.path.join(PUBLIC, "favicon-32.png"), 32)
    rsvg(tile_svg(180, bleed=True), os.path.join(PUBLIC, "apple-touch-icon.png"), 180)
    rsvg(tile_svg(192), os.path.join(PUBLIC, "icon-192.png"), 192)
    rsvg(tile_svg(512), os.path.join(PUBLIC, "icon-512.png"), 512)
    with tempfile.TemporaryDirectory() as tmp:
        p48 = os.path.join(tmp, "48.png")
        rsvg(tile_svg(48, rx=10), p48, 48)
        subprocess.run(
            [
                "magick",
                os.path.join(PUBLIC, "favicon-16.png"),
                os.path.join(PUBLIC, "favicon-32.png"),
                p48,
                os.path.join(PUBLIC, "favicon.ico"),
            ],
            check=True,
        )
    write(TS_OUT, geometry_ts())
    print("brand assets written; ink path", len(INK), "chars; axis", round(g.AXIS_Y, 2))


if __name__ == "__main__":
    main()
