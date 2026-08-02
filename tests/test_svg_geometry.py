"""Layout regression test for the architecture diagrams.

Flattens every flow wire in docs/architecture-*.svg into polyline segments and
asserts three properties, because a diagram whose wires cross or hide each other
misinforms faster than no diagram at all:

  (a) no two wires cross,
  (b) no wire passes through a card's interior,
  (c) no two wires run along the same corridor on top of each other.

(b) and (c) used to be claimed but not checked, and the gap was not academic --
both misses are invisible in a passing run, which is the property that lets a
layout regression ship:

  * The through-card test only looked at segment ENDPOINTS. A wire drawn straight
    across a card -- `M100,228 L700,228` through a card spanning x 300..480 --
    has both endpoints comfortably outside it, so the single worst layout defect
    the file exists to catch was the one shape it could not see. Now every
    segment is clipped against the (shrunk) card rectangle.

  * The crossing test used a determinant that is ZERO for parallel lines and
    bailed out, so two wires sharing a corridor -- `M100,300 L600,300` and
    `M200,300 L700,300` -- reported no intersection. Overlapping collinear wires
    render as one wire: a connection silently disappears from the picture rather
    than looking wrong. Handled explicitly now.

Cards are shrunk by CARD_MARGIN before the interior test because wires are
anchored ON card edges by design; the margin is what separates "arrives at this
card" from "cuts through it".

Usage: python3 tests/test_svg_geometry.py [svg ...]

The file is named test_* so pytest COLLECTS it. It was `check_svg_geometry.py`
while its own docstring claimed pytest collected it -- pytest's default
`python_files = test_*.py` meant it collected zero tests from this file, and the
repo sets no `python_files` override, so the three tests below existed and never
ran. The claim was checkable and false in the same breath, which is the shape of
every other defect this file guards against.
"""
import glob
import itertools
import os
import re
import sys

CARD_MARGIN = 6.0   # wires legitimately touch card edges; only the interior is off-limits
EPS = 1e-6
DOCS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs")


def parse_path(d):
    """Return list of points approximating the path (M/L/C only)."""
    toks = re.findall(r'[MLC]|-?\d+\.?\d*', d)
    pts, i, cur, cmd = [], 0, None, None
    while i < len(toks):
        t = toks[i]
        if t in 'MLC':
            cmd = t; i += 1; continue
        if cmd == 'M':
            cur = (float(toks[i]), float(toks[i + 1])); pts.append(cur); i += 2; cmd = 'L'
        elif cmd == 'L':
            cur = (float(toks[i]), float(toks[i + 1])); pts.append(cur); i += 2
        elif cmd == 'C':
            p1 = (float(toks[i]), float(toks[i + 1]))
            p2 = (float(toks[i + 2]), float(toks[i + 3]))
            p3 = (float(toks[i + 4]), float(toks[i + 5]))
            p0 = cur
            for k in range(1, 13):
                s = k / 12; m = 1 - s
                x = m**3 * p0[0] + 3 * m * m * s * p1[0] + 3 * m * s * s * p2[0] + s**3 * p3[0]
                y = m**3 * p0[1] + 3 * m * m * s * p1[1] + 3 * m * s * s * p2[1] + s**3 * p3[1]
                pts.append((x, y))
            cur = p3; i += 6
        else:
            i += 1
    return pts


def segs(pts):
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


def inter(a, b):
    """Proper crossing point of two segments, or None.

    Endpoint-to-endpoint contact is excluded (EPS): wires fanning out of one
    card edge share an anchor by design and that is not a crossing.
    """
    (x1, y1), (x2, y2) = a
    (x3, y3), (x4, y4) = b
    d = (x2 - x1) * (y4 - y3) - (y2 - y1) * (x4 - x3)
    if abs(d) < 1e-12:
        return None            # parallel -- overlap is handled by collinear_overlap
    t = ((x3 - x1) * (y4 - y3) - (y3 - y1) * (x4 - x3)) / d
    u = ((x3 - x1) * (y2 - y1) - (y3 - y1) * (x2 - x1)) / d
    if EPS < t < 1 - EPS and EPS < u < 1 - EPS:
        return (x1 + t * (x2 - x1), y1 + t * (y2 - y1))
    return None


def collinear_overlap(a, b, tol=0.75):
    """Midpoint of the shared stretch when two segments lie on the same line and
    overlap over a non-trivial length, else None.

    Two wires on one corridor draw as a single wire: the reader loses a
    connection entirely, and nothing about the output looks wrong. `tol` is in
    user units -- a shared corridor is only a problem once it is long enough to
    read as one line, and touching at a point is not an overlap.
    """
    (x1, y1), (x2, y2) = a
    (x3, y3), (x4, y4) = b
    dx, dy = x2 - x1, y2 - y1
    ex, ey = x4 - x3, y4 - y3
    if abs(dx * ey - dy * ex) > 1e-9:
        return None                                   # not parallel
    if abs(dx) < 1e-12 and abs(dy) < 1e-12:
        return None                                   # degenerate
    # b's endpoints must sit on a's infinite line
    if abs((x3 - x1) * dy - (y3 - y1) * dx) > tol:
        return None
    L = (dx * dx + dy * dy) ** 0.5
    def proj(p):
        return ((p[0] - x1) * dx + (p[1] - y1) * dy) / L
    a0, a1 = 0.0, L
    b0, b1 = sorted((proj((x3, y3)), proj((x4, y4))))
    lo, hi = max(a0, b0), min(a1, b1)
    if hi - lo <= tol:
        return None
    mid = (lo + hi) / 2
    return (x1 + dx / L * mid, y1 + dy / L * mid, hi - lo)


def seg_enters_rect(seg, rect, margin=CARD_MARGIN):
    """True when the segment passes through the rect's interior.

    Liang-Barsky clip against the rect shrunk by `margin`, so a wire ANCHORED on
    a card edge is fine while a wire crossing the card is not. Endpoint testing
    -- what this replaced -- cannot distinguish those two cases at all: a wire
    that spans the card has both endpoints outside it.
    """
    (px, py), (qx, qy) = seg
    x, y, w, h = rect
    xmin, xmax = x + margin, x + w - margin
    ymin, ymax = y + margin, y + h - margin
    if xmax <= xmin or ymax <= ymin:
        return False
    dx, dy = qx - px, qy - py
    t0, t1 = 0.0, 1.0
    for p, q_ in ((-dx, px - xmin), (dx, xmax - px), (-dy, py - ymin), (dy, ymax - py)):
        if abs(p) < 1e-12:
            if q_ < 0:
                return False          # parallel and outside this boundary
            continue
        r = q_ / p
        if p < 0:
            if r > t1: return False
            if r > t0: t0 = r
        else:
            if r < t0: return False
            if r < t1: t1 = r
    return t1 - t0 > 1e-9


def overlapping_cards(rects):
    """Pairs of cards that overlap each other.

    Not a wire property, so the wire checks cannot see it -- but it is the same
    class of defect and it MASQUERADES as one: while building the console
    diagram, a 4th card placed at x=1040 sat on top of a card spanning
    846..1102, and the only symptom was THROUGH-CARD reported against a wire that
    was drawn exactly right. Checking cards directly names the real cause.
    """
    bad = []
    for (a, b) in itertools.combinations(rects, 2):
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        if ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah:
            bad.append(f"CARD-OVERLAP rect({ax:.0f},{ay:.0f},{aw:.0f},{ah:.0f}) "
                       f"and rect({bx:.0f},{by:.0f},{bw:.0f},{bh:.0f})")
    return bad


def out_of_canvas(rects, raw):
    """Cards that fall outside the declared viewBox."""
    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', raw)
    if not m:
        return []
    W, H = float(m.group(1)), float(m.group(2))
    return [f"OFF-CANVAS rect({x:.0f},{y:.0f},{w:.0f},{h:.0f}) exceeds viewBox {W:.0f}x{H:.0f}"
            for (x, y, w, h) in rects if x < 0 or y < 0 or x + w > W or y + h > H]


def check(path, verbose=True):
    """Return a list of human-readable layout problems in one SVG."""
    raw = open(path).read()
    wires = [(m.group(1), m.group(2)) for m in
             re.finditer(r'<path class="(wire\w*)" d="([^"]+)"', raw)]
    rects = [(float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4)))
             for m in re.finditer(
                 r'<rect class="card [^"]*" x="([\d.]+)" y="([\d.]+)" '
                 r'width="([\d.]+)" height="([\d.]+)"', raw)]
    poly = [(f"{cls}#{i}", segs(parse_path(d))) for i, (cls, d) in enumerate(wires)]

    problems = []
    if verbose:
        print(f"\n== {os.path.basename(path)}: {len(poly)} wires, {len(rects)} cards ==")

    for (ca, sa), (cb, sb) in itertools.combinations(poly, 2):
        for x in sa:
            for y in sb:
                p = inter(x, y)
                if p:
                    problems.append(f"CROSS {ca} x {cb} at ({p[0]:.0f},{p[1]:.0f})")
                o = collinear_overlap(x, y)
                if o:
                    problems.append(f"OVERLAP {ca} x {cb} share {o[2]:.0f}px "
                                    f"of corridor near ({o[0]:.0f},{o[1]:.0f})")

    for cls, ss in poly:
        for rect in rects:
            for seg in ss:
                if seg_enters_rect(seg, rect):
                    problems.append(
                        f"THROUGH-CARD {cls} crosses rect"
                        f"({rect[0]:.0f},{rect[1]:.0f},{rect[2]:.0f},{rect[3]:.0f})")
                    break

    problems += overlapping_cards(rects)
    problems += out_of_canvas(rects, raw)

    if verbose:
        for p in dict.fromkeys(problems):
            print("  " + p)
        print("  CLEAN — no crossings, no overlaps, no wire through a card"
              if not problems else f"  {len(problems)} issue(s)")
    return problems


def default_targets():
    return sorted(glob.glob(os.path.join(DOCS, "architecture-*.svg")))


def test_diagram_layout_is_clean():
    """Every shipped diagram: no crossing, overlapping, or card-piercing wire.

    A pytest test rather than a standalone script only run by a shell line,
    because that line was `ls *.svg && python check.py || echo "no SVGs yet"` --
    `||` swallows the checker's non-zero exit, so the step reported success on a
    failing layout. A guard CI cannot fail is decoration.
    """
    targets = default_targets()
    assert targets, "no architecture SVGs found to check"
    for path in targets:
        problems = check(path, verbose=False)
        assert not problems, (
            f"{os.path.basename(path)} has {len(problems)} layout problem(s):\n  "
            + "\n  ".join(dict.fromkeys(problems)))


def test_the_svgs_match_their_generator():
    """The committed SVGs are exactly what gen_architecture_svg.py produces.

    The header of that file says "never hand-edit the SVGs", but nothing checked
    it, so a hand-edit would survive until the next regeneration silently
    reverted it. Runs the generator into a temp dir and compares bytes.
    """
    import subprocess
    import tempfile
    import shutil

    repo = os.path.dirname(DOCS)
    gen = os.path.join(DOCS, "gen_architecture_svg.py")
    assert os.path.exists(gen), gen
    with tempfile.TemporaryDirectory() as td:
        shutil.copy(gen, os.path.join(td, "gen_architecture_svg.py"))
        r = subprocess.run([sys.executable, os.path.join(td, "gen_architecture_svg.py")],
                           capture_output=True, text=True, cwd=repo)
        assert r.returncode == 0, f"generator failed: {r.stderr[-500:]}"
        for f in os.listdir(td):
            if not f.endswith(".svg"):
                continue
            shipped = os.path.join(DOCS, f)
            assert os.path.exists(shipped), f"generator emits {f} but it is not committed"
            fresh = open(os.path.join(td, f)).read()
            assert open(shipped).read() == fresh, (
                f"docs/{f} differs from generator output -- regenerate with "
                f"`python3 docs/gen_architecture_svg.py`, do not hand-edit")


def test_no_account_id_leaks_into_a_diagram():
    """This repo is public; the diagrams must not carry a 12-digit account id."""
    for p in default_targets():
        hits = re.findall(r"\b\d{12}\b", open(p).read())
        assert not hits, f"{os.path.basename(p)} leaks account id(s): {set(hits)}"


if __name__ == "__main__":
    tgts = sys.argv[1:] or default_targets()
    total = sum(len(check(p)) for p in tgts)
    print("\nAll layout checks passed!" if total == 0 else f"\n{total} layout issue(s) found.")
    sys.exit(1 if total else 0)
