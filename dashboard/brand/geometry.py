"""Remembra "pops" mark and wordmark geometry.

The brain is drawn as five unequal lobes (frontal, precentral, parietal crown,
a flatter occipital and a flat temporal underside) plus the cerebellum and the
stem, unioned into one silhouette. The folds are knocked out of it and the
result is written as a single compound path (fill-rule evenodd), so no asset
depends on an SVG mask. The orange baton is a flat horizontal capsule that
sits on the lateral fold; in the lockup it lines up with the crossbar of the
e in "mem".

Requires shapely (dev tooling only; the dashboard never imports this).
"""

from __future__ import annotations

import math

from shapely import affinity
from shapely.geometry import LineString, MultiPolygon, Point, Polygon
from shapely.ops import unary_union

# ---------------------------------------------------------------------------
# Brain (0..100 design space, facing left: frontal lobe on the left)
# ---------------------------------------------------------------------------

# (cx, cy, rx, ry, rotation°) per lobe, deliberately unequal.
LOBES = {
    "frontal": (30.5, 40.5, 19.5, 20.5, -10),
    "precentral": (47.0, 24.5, 12.5, 11.5, 0),
    "parietal": (63.5, 25.0, 17.5, 13.5, 7),
    "occipital": (79.5, 41.0, 11.0, 13.0, 14),
    "temporal": (47.5, 55.0, 24.5, 9.0, -3),
    "core": (52.0, 40.0, 28.0, 18.5, 0),
}
CEREBELLUM = (74.0, 61.0, 11.5, 7.6, -8)
STEM = ((61.5, 58.0), (63.8, 75.0))
STEM_W = 8.6

KW = 4.6  # fold (knockout) width: wide enough to survive 32px
BATON_Y = 49.0
BATON_X = (55.0, 70.0)
BATON_W = 5.0


def _bez(p0, p1, p2, p3, n=28):
    pts = []
    for i in range(n + 1):
        t = i / n
        a = (1 - t) ** 3
        b = 3 * (1 - t) ** 2 * t
        c = 3 * (1 - t) * t * t
        d = t**3
        pts.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return pts


def _quad(p0, p1, p2, n=20):
    return [
        (
            (1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * p1[0] + t * t * p2[0],
            (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * p1[1] + t * t * p2[1],
        )
        for t in (i / n for i in range(n + 1))
    ]


# Folds. None of them points at a shared centre (that is what made the old
# mark read as a sunflower): each follows the anatomy of its own junction.
FOLDS = {
    # frontal / precentral: a short notch dropping into the forehead crown
    "precentral_sulcus": _quad((37.0, 15.0), (40.5, 20.5), (39.0, 27.5)),
    # central sulcus: runs down and forward across the crown
    "central_sulcus": _quad((56.0, 10.5), (55.5, 18.5), (51.0, 24.5)),
    # parieto-occipital: from the back of the crown, down and in
    "parieto_occipital": _quad((81.5, 26.0), (76.0, 29.0), (74.0, 35.5)),
    # a shallow notch on the forehead so the frontal lobe is not a plain dome
    "frontal_notch": _quad((10.5, 37.0), (15.0, 38.5), (17.5, 36.0)),
    # lateral (sylvian) fissure: the trail the baton rides on
    "lateral": _bez((18.5, 55.0), (27.0, 51.0), (37.0, 53.8), (46.0, 50.8))
    + _bez((46.0, 50.8), (49.5, 49.4), (52.5, 49.0), (56.0, 49.0))[1:]
    + [(BATON_X[1], BATON_Y)],
    # cerebellum under the occipital lobe
    "cerebellar": _bez((66.0, 60.5), (69.5, 56.0), (78.0, 54.6), (89.0, 56.0)),
}


def _ellipse(cx, cy, rx, ry, rot=0.0):
    e = Point(0, 0).buffer(1.0, resolution=64)
    e = affinity.scale(e, rx, ry, origin=(0, 0))
    e = affinity.rotate(e, rot, origin=(0, 0))
    return affinity.translate(e, cx, cy)


def _stroke(points, width):
    return LineString(points).buffer(width / 2, cap_style="round", join_style="round", resolution=24)


def brain_polygon():
    parts = [_ellipse(*v) for v in LOBES.values()]
    parts.append(_ellipse(*CEREBELLUM))
    parts.append(_stroke(STEM, STEM_W))
    body = unary_union(parts)
    # Close the sharp inner corners where lobes meet, so the joins read as
    # soft creases rather than intersecting circles.
    body = body.buffer(1.6, resolution=24).buffer(-1.6, resolution=24)
    cuts = unary_union([_stroke(p, KW) for p in FOLDS.values()])
    return body.difference(cuts)


def _bounds(geom):
    return geom.bounds  # (minx, miny, maxx, maxy)


# Fit the mark into the 100 box with an even margin.
_RAW = brain_polygon()
_B = _bounds(_RAW)
MARGIN = 7.0
SCALE = (100 - 2 * MARGIN) / max(_B[2] - _B[0], _B[3] - _B[1])
OFF = (
    50 - SCALE * (_B[0] + _B[2]) / 2,
    50 - SCALE * (_B[1] + _B[3]) / 2,
)


def to_mark(x, y):
    return (OFF[0] + SCALE * x, OFF[1] + SCALE * y)


def mark_polygon():
    return affinity.translate(affinity.scale(_RAW, SCALE, SCALE, origin=(0, 0)), OFF[0], OFF[1])


AXIS_Y = to_mark(0, BATON_Y)[1]


def _ring_d(coords, nd=2):
    pts = list(coords)
    if pts[0] == pts[-1]:
        pts = pts[:-1]
    fmt = f"{{:.{nd}f}}"

    def f(v):
        s = fmt.format(v).rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s

    out = [f"M{f(pts[0][0])} {f(pts[0][1])}"]
    for x, y in pts[1:]:
        out.append(f"L{f(x)} {f(y)}")
    return "".join(out) + "Z"


def poly_d(geom, tol=0.04, nd=2):
    geom = geom.simplify(tol, preserve_topology=True)
    polys = list(geom.geoms) if isinstance(geom, MultiPolygon) else [geom]
    d = []
    for p in polys:
        assert isinstance(p, Polygon)
        d.append(_ring_d(p.exterior.coords, nd))
        for hole in p.interiors:
            d.append(_ring_d(hole.coords, nd))
    return "".join(d)


def capsule_d(x1, x2, y, w, nd=2):
    """A flat horizontal capsule (the baton) as a filled path."""
    r = w / 2

    def f(v):
        return f"{v:.{nd}f}".rstrip("0").rstrip(".")

    return f"M{f(x1)} {f(y - r)}H{f(x2)}A{f(r)} {f(r)} 0 0 1 {f(x2)} {f(y + r)}H{f(x1)}A{f(r)} {f(r)} 0 0 1 {f(x1)} {f(y - r)}Z"


def mark_paths():
    """(ink compound path, baton path) in the 0..100 mark box."""
    ink = poly_d(mark_polygon())
    x1, y = to_mark(BATON_X[0], BATON_Y)
    x2, _ = to_mark(BATON_X[1], BATON_Y)
    return ink, capsule_d(x1, x2, y, BATON_W * SCALE)


def mark_bbox():
    return mark_polygon().bounds


# ---------------------------------------------------------------------------
# Wordmark: monoline strokes, round caps (x-height 55.5..94.5, cap 33.5)
# ---------------------------------------------------------------------------

SW = 11
BASE = 94.5
XT = 55.5
CT = 33.5
CY = 75
RX = 17
RY = 19.5
# Angle (below the horizontal, measured at the bowl centre) where the lower
# terminal of the e stops. Wider than the first draft (38°) so the aperture
# stays open and the crossbar still reads at 14-16px.
E_TERMINAL_DEG = 56


def e(x0):
    cx = x0 + 5.5 + RX
    a = math.radians(E_TERMINAL_DEG)
    ex = cx + RX * math.cos(a)
    ey = CY + RY * math.sin(a)
    body = f"M{cx + RX} {CY}A{RX} {RY} 0 0 0 {cx - RX} {CY}A{RX} {RY} 0 0 0 {ex:.2f} {ey:.2f}"
    bar = f"M{cx - RX} {CY}H{cx + RX}"
    return body, bar, 2 * RX + SW


def R(x0):
    s = x0 + 5.5
    return f"M{s} {BASE}V{CT}H{s + 14}A15.5 15.5 0 0 1 {s + 14} 64.5H{s}M{s + 13} 64.5L{s + 28} {BASE}", 14 + 15.5 + SW


def m(x0):
    s = x0 + 5.5
    a = 23
    d = f"M{s} {BASE}V{XT}M{s} 71A{a / 2} 15.5 0 0 1 {s + a} 71V{BASE}M{s + a} 71A{a / 2} 15.5 0 0 1 {s + 2 * a} 71V{BASE}"
    return d, 2 * a + SW


def b(x0):
    s = x0 + 5.5
    d = f"M{s} {CT - 6}V{BASE}M{s} {CY}A{RX} {RY} 0 0 1 {s + 2 * RX} {CY}A{RX} {RY} 0 0 1 {s} {CY}"
    return d, 2 * RX + SW


def r(x0):
    s = x0 + 5.5
    return f"M{s} {BASE}V{XT}M{s} 73A15 17.5 0 0 1 {s + 15} {XT}", 15 + SW


def a_(x0):
    cx = x0 + 5.5 + RX
    d = f"M{cx + RX} {CY}A{RX} {RY} 0 0 0 {cx - RX} {CY}A{RX} {RY} 0 0 0 {cx + RX} {CY}M{cx + RX} {XT}V{BASE}"
    return d, 2 * RX + SW


def wordmark_paths(gap=5.5):
    """(ink stroke paths, signal stroke paths, total width). Signal = the e crossbar in "mem"."""
    x = 0.0
    ink: list[str] = []
    sig: list[str] = []
    d, w = R(x)
    ink.append(d)
    x += w + gap - 2
    body, bar, w = e(x)
    ink += [body, bar]
    x += w + gap
    d, w = m(x)
    ink.append(d)
    x += w + gap
    body, bar, w = e(x)
    ink.append(body)
    sig.append(bar)
    x += w + gap
    d, w = m(x)
    ink.append(d)
    x += w + gap
    d, w = b(x)
    ink.append(d)
    x += w + gap
    d, w = r(x)
    ink.append(d)
    x += w + gap - 3
    d, w = a_(x)
    ink.append(d)
    x += w
    return ink, sig, x
