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


#: Rough advance width per character as a fraction of font-size, for the UI stack the
#: diagrams declare. Deliberately an UNDER-estimate (a real renderer is wider for most
#: strings), so this check flags only text that genuinely runs off and never blocks a
#: line that merely looks close.
_CHAR_W = 0.52
_FONT_PX = {"sub": 10.0, "bandT": 11.0, "title": 14.0}
#: Extra px per character from the class's letter-spacing. Omitting this is not a
#: rounding error: .bandT sets letter-spacing:1px, and on the ~150-character STATE
#: band that is 150px of unaccounted width -- enough that the first version of this
#: check called a visibly clipped line clean. Measured from STYLE above, so a change
#: there has to be reflected here.
_LETTER_SPACING = {"sub": 0.0, "bandT": 1.0, "title": 0.0}


def overflowing_text(raw):
    """Text that runs past the right edge of the canvas, or past its own card.

    Neither is a wire or a card, so every check above is blind to it -- and both are
    silent: the SVG stays valid, the layout suite stays green, and the sentence is
    simply cut off in a browser. Found by rendering the diagram to a PNG and looking
    at it, which is exactly the step a machine check is supposed to replace.

    Two cases, because they fail for different reasons:
      * a left-anchored line whose start x plus its own width exceeds the viewBox --
        how three GOVERNANCE-row notes ran off the 1240-wide canvas;
      * a centred card label wider than the card it names, which collides with the
        neighbouring column instead of being clipped.

    Width is estimated, so the threshold is generous (TOLERANCE_PX) and the estimate
    is an under-count. This catches the sentence-length mistakes it exists to catch,
    not kerning.
    """
    m = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', raw)
    if not m:
        return []
    W = float(m.group(1))
    TOLERANCE_PX = 12.0
    bad = []
    cards = [(float(a), float(b), float(c)) for a, b, c in re.findall(
        r'<rect class="card [^"]*" x="([\d.]+)" y="([\d.]+)" width="([\d.]+)"', raw)]
    for t in re.finditer(
            r'<text class="(sub|bandT|title)"([^>]*)>(.*?)</text>', raw, re.S):
        cls, attrs, body = t.group(1), t.group(2), re.sub(r"<[^>]+>", "", t.group(3))
        xm = re.search(r'x="([\d.]+)"', attrs)
        ym = re.search(r'y="([\d.]+)"', attrs)
        if not xm or not ym:
            continue
        x, y = float(xm.group(1)), float(ym.group(1))
        # A font-size attribute on the element overrides the class default.
        fs = re.search(r'font-size="([\d.]+)"', attrs)
        px = float(fs.group(1)) if fs else _FONT_PX[cls]
        width = len(body) * (px * _CHAR_W + _LETTER_SPACING[cls])
        centred = 'text-anchor="middle"' in attrs
        left, right = (x - width / 2, x + width / 2) if centred else (x, x + width)
        if right > W + TOLERANCE_PX or left < -TOLERANCE_PX:
            bad.append(f"TEXT-OFF-CANVAS at ({x:.0f},{y:.0f}) est. spans "
                       f"{left:.0f}..{right:.0f} beyond 0..{W:.0f}: {body[:60]!r}")
            continue
        if not centred:
            continue
        # Centred labels belong to the card they sit on: find it by centre-x and a y
        # inside the card's own band. Only card titles/subs are anchored this way.
        for cx, cy, cw in cards:
            if abs((cx + cw / 2) - x) < 1.0 and cy <= y <= cy + 60:
                if width > cw + TOLERANCE_PX:
                    bad.append(
                        f"TEXT-WIDER-THAN-CARD at ({x:.0f},{y:.0f}) est. {width:.0f}px "
                        f"in a {cw:.0f}px card: {body[:60]!r}")
                break
    return bad


#: Minimum gap demanded between a free-standing label's estimated glyph band and any
#: wire. NOT zero: with a 0 threshold the AUDIT PLANE heading cleared the resume wire
#: by 0.2 estimated px and passed, while rendering as a strikethrough. A tolerance of
#: zero on an estimated box is a tolerance on rounding error, so it certifies nothing.
TEXT_WIRE_CLEARANCE_PX = 4.0


def text_boxes(raw):
    """Estimated (x, y, w, h) box and body for every free-standing label.

    Labels that sit INSIDE a card are excluded: a card's own title and subtitle are
    drawn over its rect by design, and the wires that arrive at that card touch its
    edges. Only the free-floating labels -- band headings and the annotations beside
    a wire -- are in play here.
    """
    cards = [(float(a), float(b), float(c), float(d)) for a, b, c, d in re.findall(
        r'<rect class="card [^"]*" x="([\d.]+)" y="([\d.]+)" '
        r'width="([\d.]+)" height="([\d.]+)"', raw)]
    boxes = []
    for t in re.finditer(r'<text class="(sub|bandT|title)"([^>]*)>(.*?)</text>', raw, re.S):
        cls, attrs, body = t.group(1), t.group(2), re.sub(r"<[^>]+>", "", t.group(3))
        xm, ym = re.search(r'x="([\d.]+)"', attrs), re.search(r'y="([\d.]+)"', attrs)
        if not xm or not ym:
            continue
        x, y = float(xm.group(1)), float(ym.group(1))
        fs = re.search(r'font-size="([\d.]+)"', attrs)
        px = float(fs.group(1)) if fs else _FONT_PX[cls]
        w = len(body) * (px * _CHAR_W + _LETTER_SPACING[cls])
        if 'text-anchor="middle"' in attrs:
            x -= w / 2
        box = (x, y - px * 0.8, w, px * 1.1)          # baseline -> ascent..descent
        # Ownership, not containment: a label BELONGS to a card when it is centred on
        # that card's centre-x with a baseline inside the card's band. Testing
        # containment instead (does the estimated box fit inside the rect?) wrongly
        # freed four card subtitles into this list -- their estimated width exceeds
        # the card, so they "escaped" their own card and then registered as floating
        # labels overlapping it. Over-long card labels are already `overflowing_text`'s
        # job; conflating the two makes both reports untrustworthy.
        centred = 'text-anchor="middle"' in attrs
        if centred and any(abs((cx + cw / 2) - (x + w / 2)) < 1.0 and cy <= y <= cy + ch
                           for cx, cy, cw, ch in cards):
            continue
        if any(cx <= x <= cx + cw and cy <= y <= cy + ch for cx, cy, cw, ch in cards):
            continue                                   # left-anchored label inside a card
        boxes.append((box, body))
    return boxes


def wire_through_text(raw):
    """Wires that strike through a free-standing label.

    The requirement this file exists to enforce is that the wires, the boxes and
    their relationships are all legible. A wire drawn across a sentence defeats that
    exactly as much as a wire drawn across a card -- but every check above tests
    wires against RECTS, and a label has no rect, so a struck-through sentence was
    invisible to the whole suite. Four labels on the high-level diagram were being
    crossed when this was added, among them the AUDIT PLANE heading, and the layout
    suite was green for all of them. Found by rendering to PNG and looking.

    The box is estimated from the same width model as `overflowing_text`, so the
    margin is 0 (the estimate already under-counts width) and a label is only
    reported when a wire segment genuinely enters the estimated glyph band.
    """
    wires = [(m.group(1), segs(parse_path(m.group(2)))) for m in
             re.finditer(r'<path class="(wire\w*)" d="([^"]+)"', raw)]
    bad = []
    C = TEXT_WIRE_CLEARANCE_PX
    for (x, y, w, h), body in text_boxes(raw):
        grown = (x - C, y - C, w + 2 * C, h + 2 * C)
        for name, ss in wires:
            if any(seg_enters_rect(s, grown, margin=0.0) for s in ss):
                bad.append(f"WIRE-THROUGH-TEXT {name} crosses (or comes within "
                           f"{C:.0f}px of) the label at ({x:.0f},{y:.0f}): {body[:60]!r}")
                break
    return bad


def label_over_card(raw):
    """Free-standing labels that overlap a card they do not belong to.

    A third thing the wire checks cannot see, and the third one found by rendering to
    PNG rather than by any assertion: "gate fail → remediate (≤3)" was centred such
    that its last ~23px lay on top of the SageMaker Training card, so the annotation
    and the card's title were drawn over each other. Cards-vs-cards is checked
    (`overlapping_cards`) and wires-vs-cards is checked (`seg_enters_rect`), but a
    label is neither, and it was the one combination with no test at all.

    Labels that sit inside a card are excluded upstream by `text_boxes`, so anything
    reaching here is a floating annotation and any card overlap is unintended.
    """
    cards = [(float(a), float(b), float(c), float(d)) for a, b, c, d in re.findall(
        r'<rect class="card [^"]*" x="([\d.]+)" y="([\d.]+)" '
        r'width="([\d.]+)" height="([\d.]+)"', raw)]
    bad = []
    for (x, y, w, h), body in text_boxes(raw):
        for cx, cy, cw, ch in cards:
            ox = min(x + w, cx + cw) - max(x, cx)
            oy = min(y + h, cy + ch) - max(y, cy)
            if ox > 1.0 and oy > 1.0:
                bad.append(f"LABEL-OVER-CARD label at ({x:.0f},{y:.0f}) overlaps "
                           f"rect({cx:.0f},{cy:.0f},{cw:.0f},{ch:.0f}) by "
                           f"{ox:.0f}x{oy:.0f}px: {body[:60]!r}")
                break
    return bad


def overlapping_labels(raw):
    """Free-standing labels drawn on top of each other.

    The fourth and last pairing: by the time this was added, cards-vs-cards,
    wires-vs-cards, wires-vs-text and labels-vs-cards were all checked, and two
    labels overlapping was the combination nothing looked at. It bit immediately --
    moving the escalation annotation up by 4px to clear a wire dropped it onto the
    header subtitle, trading a checked defect for an unchecked one. Two sentences
    printed over each other are less readable than either failure this file started
    out guarding against.

    The threshold on both axes is 1px, and that is load-bearing. The first version
    demanded half a line of vertical overlap on the theory that it would otherwise
    flag adjacent rows of the same block -- and then its own negative control passed:
    restoring the overlapping label produced 3px of overlap, under the 5.5px bar, so
    the check called the defect clean. It never needed the allowance: rows in a block
    are 18-20px apart with ~11-12px boxes, so they do not overlap at all, and a
    threshold set to head off a collision that cannot happen only masked one that did.
    """
    boxes = text_boxes(raw)
    bad = []
    for (a, ta), (b, tb) in itertools.combinations(boxes, 2):
        ox = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
        oy = min(a[1] + a[3], b[1] + b[3]) - max(a[1], b[1])
        if ox > 1.0 and oy > 1.0:
            bad.append(f"LABEL-OVER-LABEL {ta[:34]!r} at ({a[0]:.0f},{a[1]:.0f}) "
                       f"overlaps {tb[:34]!r} at ({b[0]:.0f},{b[1]:.0f}) "
                       f"by {ox:.0f}x{oy:.0f}px")
    return bad


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
    problems += overflowing_text(raw)
    problems += wire_through_text(raw)
    problems += label_over_card(raw)
    problems += overlapping_labels(raw)

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


def test_the_training_card_links_the_trainer_the_deploy_actually_mirrors():
    """A diagram may not point at a trainer no run can reach.

    It did, for months: the SageMaker Training card deep-linked
    pipeline/training/train_qlora.py while deploy/03_storage.py mirrored a different copy
    under distill/, which is what every run downloaded. A reader auditing the trainer read
    the wrong file and found three deliverability rules that were not in the deployed one.
    Derived from ensure_code()'s own literal path parts rather than written down here, for
    the same reason the deliverability tests are: a hardcoded expectation is how the drift
    survived a full suite in the first place.
    """
    repo = os.path.dirname(DOCS)
    body = (open(os.path.join(repo, "deploy/03_storage.py")).read()
            .split("def ensure_code(")[1].split("\ndef ")[0])
    parts = re.findall(r'"([a-z_0-9]+)"', body.split("src_dir =")[1].split("\n")[0])
    assert parts, "ensure_code no longer builds its source dir from literal path parts"
    mirrored = "/".join(parts) + "/train_qlora.py"
    svg = open(os.path.join(DOCS, "architecture-high-level.svg")).read()
    assert f"blob/main/{mirrored}" in svg, (
        f"the training card must link {mirrored} -- the file ensure_code() uploads and every "
        "run downloads. Fix docs/gen_architecture_svg.py and regenerate")
    assert "blob/main/pipeline/training/train_qlora.py" not in svg, (
        "the diagram still links the non-mirrored copy of the trainer")


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
