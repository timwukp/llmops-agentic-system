#!/usr/bin/env python3
"""Build the console's Introduction page: one self-contained HTML file.

    python3 deploy/console/intro/build_intro.py --out /tmp/intro.html

Inputs, all committed:
    intro/page.template.html   the scenes, the player, the styling
    intro/narration.json       the script and the language/voice table
    intro/durations.json       MEASURED clip seconds, written by synth_narration.py
    intro/clauses.json         MEASURED English clause starts, written by measure_clauses.py
    docs/architecture-*.svg    the three real diagrams, inlined

Output: ONE file with no external reference except its own audio under
`/intro/audio/<lang>/<scene>.mp3`. It is NOT committed -- deploy.sh runs this into the
Lambda build directory. A generated 200 KB artifact in git is a file that can disagree
with its own inputs while looking authoritative, which is the exact failure this
project keeps writing guards against. The audio IS committed, because regenerating it
costs a Polly run; regenerating this page costs nothing.

Two things this does beyond substitution, both of which are silent bugs otherwise:

1. **SVG id namespacing.** The three diagrams each define `<marker id="ahB">`,
   `id="ahG">`, gradients, and clip paths with the SAME ids. Inlining two of them into
   one document makes every reference in the second resolve to the FIRST one's element
   -- ids are document-global, and the browser does not warn. Today the markers happen
   to be identical so nothing looks wrong, which is worse than a visible break: the day
   one diagram's arrowhead changes colour, the other diagram changes with it. So every
   id in each SVG is prefixed per file and every `url(#...)` / `href="#..."` reference
   is rewritten to match.

2. **Beat reachability.** A scene's animation beats are authored in ENGLISH seconds and
   scaled at runtime by (this language's clip length / the English clip length). A beat
   whose `data-t` is past the end of the English clip never fires in ANY language -- the
   content simply never appears, on a page nobody can diff. Every beat is checked
   against the measured English duration here, at build time, where it is cheap.

3. **English beats on the English voice.** Proportional scaling assumes a voice that
   speaks at an even rate. The English one does not, and the owner reported it as the
   one language whose pictures did not match the words (2026-10-27). So a beat names
   the English clause it illustrates, `data-at="<clause>"` or `"<clause>+<seconds>"`,
   and this writes the clause's MEASURED start as `data-en`, which the player uses for
   English only; the other four languages keep scaling `data-t`, unchanged. A beat
   after ANCHOR_FROM seconds with no anchor is refused, and so is a clauses.json
   measured from a different clip or text than the one being shipped.

4. **Clip URLs that change when the clip does.** The page asks for
   `/intro/audio/<lang>/<scene>.mp3?v=<sha256 prefix of that mp3>`. The URL used to be the
   same in every version, and the route lets a browser keep a clip for a day, so after v2
   shipped the owner's browser went on playing the v1 English it had cached while the
   deployed clips were v2 (2026-10-27). The server ignores the query; it only has to make
   a changed clip a different URL.

5. **Captions that show the same sentences in every language.** The caption used to be a
   150-character window centred on the clip's proportional position. 150 characters is
   two thirds of a Chinese scene and a fifth of an English one, so at 0:06 of scene 1 the
   Mandarin and Cantonese showed the opening sentences while the English showed a slice
   that began mid-word ("… ned on a store's"), and the owner read it as an English script
   that had not been updated (2026-10-27). Each scene is now CAPTION_PAGES pages that
   break between sentences. Mandarin chooses the breaks (the sentence start nearest each
   third of its text); a language whose sentence at the same index starts at about the
   same fraction of its text (within PAGE_MATCH) breaks at that same sentence, so English,
   Mandarin and Cantonese show the same sentences on the same page; Japanese and Korean,
   which split sentences differently, break at the sentence start nearest Mandarin's
   fraction unless narration.json's `caption_breaks` names the sentence. A page turns when the voice reaches its first sentence: measured for English
   (clauses.json), proportional for the rest.
"""
import argparse
import hashlib
import json
import pathlib
import re
import sys
import unicodedata

HERE = pathlib.Path(__file__).resolve().parent
REPO = HERE.parent.parent.parent
TEMPLATE = HERE / "page.template.html"
NARRATION = HERE / "narration.json"
DURATIONS = HERE / "durations.json"
CLAUSES = HERE / "clauses.json"
AUDIO = HERE / "audio"

#: Beats before this many seconds (titles, the first line) appear as the clip starts in
#: every language and need no anchor.
ANCHOR_FROM = 1.5

#: Caption pages per scene, the reference language that chooses where they break, and how
#: far (as a fraction of the text) a language's same-numbered sentence may sit from the
#: reference's before it is judged not to be the same sentence.
CAPTION_PAGES, CAPTION_REF, PAGE_MATCH = 3, "zh", 0.08
#: A page may not be wider than this many columns (CJK counts two): two caption lines at
#: the page's 1180 px width and 13.5 px type.
PAGE_COLS = 350
_SENTENCE = re.compile(r"[^.?!。？！]+[.?!。？！]+[”」\"')]*\s*")
_BEAT = re.compile(r'(?:data-at="([0-9]+)(?:\+([0-9.]+))?" )?data-t="([0-9.]+)"')

#: placeholder -> repo-relative diagram. The prefix is also the id namespace.
#:
#: architecture-console.svg is deliberately absent: scene 7 draws the console's own
#: surface rather than the dashboard's wiring diagram. It was listed here first, and the
#: exactly-one-occurrence check below caught that nothing rendered it -- the page had been
#: building "successfully" with 17 KB of SVG substituted into an HTML comment.
DIAGRAMS = {
    "__SVG_HIGH__": ("hi", "docs/architecture-high-level.svg"),
    "__SVG_LOW__": ("lo", "docs/architecture-low-level.svg"),
}

#: The template opens with an HTML comment addressed to whoever edits the template. It
#: must NOT survive into the built page, for two reasons that both bit on the first
#: build. It NAMES the placeholders, so a plain replace() substituted there as well and
#: inlined 55 KB of SVG into a comment -- and because that left a second live copy of
#: every placeholder, the "did every placeholder get filled" check could never fail. The
#: comment also documents the TEMPLATE, not the artifact; a reader of the built page
#: wants to know where it came from, which the banner below says in two lines.
_LEAD_COMMENT = re.compile(r"^(<!doctype html>\s*)<!--[\s\S]*?-->\s*", re.I)
_BANNER = ("<!-- GENERATED by deploy/console/intro/build_intro.py from page.template.html\n"
           "     + narration.json + durations.json. Do not edit; edit those. -->\n")

_ID = re.compile(r'\bid="([^"]+)"')
_REF = re.compile(r'url\(#([^)]+)\)')
_HREF = re.compile(r'\b(xlink:href|href)="#([^"]+)"')
_SCRIPT = re.compile(r"<script[\s\S]*?</script\s*>", re.I)


def namespace_svg(svg: str, prefix: str) -> str:
    """Prefix every id in one SVG and rewrite every reference to it.

    Order matters: collect the ids FIRST, then rewrite, so a reference is only renamed
    when this file actually defines the target. Renaming every `url(#x)` blindly would
    also rewrite a reference to something the page defines elsewhere.
    """
    ids = set(_ID.findall(svg))
    if not ids:
        return svg
    svg = _ID.sub(lambda m: f'id="{prefix}-{m.group(1)}"', svg)
    svg = _REF.sub(
        lambda m: f"url(#{prefix}-{m.group(1)})" if m.group(1) in ids else m.group(0), svg)
    svg = _HREF.sub(
        lambda m: (f'{m.group(1)}="#{prefix}-{m.group(2)}"'
                   if m.group(2) in ids else m.group(0)), svg)
    return svg


def load_svg(rel: str, prefix: str) -> str:
    path = REPO / rel
    svg = path.read_text(encoding="utf-8")
    # The diagrams are ours, but this page inlines them into the same document as the
    # player rather than fetching them into a shadow root the way the Architecture tab
    # does -- so strip <script> here too. The tab's inlineArchSvg() does the same.
    svg = _SCRIPT.sub("", svg)
    if "<svg" not in svg:
        raise SystemExit(f"FATAL: {rel} is not an SVG")
    return namespace_svg(svg, prefix)


def check_beats(html: str, en_durations: dict) -> list:
    """Every beat must be reachable within its own scene's English clip.

    Returns a list of complaints. A beat past the end of the clip is dead content: the
    player only ever adds `.in` when the clock passes `data-t`, and the clock stops when
    the audio does.
    """
    bad = []
    for scene_html in re.findall(r'<section class="scene" id="([^"]+)"([\s\S]*?)</section>',
                                 html):
        sid, body = scene_html
        dur = en_durations.get(sid)
        if dur is None:
            bad.append(f"scene {sid} has no measured English duration")
            continue
        for t in re.findall(r'data-t="([0-9.]+)"', body):
            if float(t) >= dur:
                bad.append(f"{sid}: beat at {t}s is past the {dur}s English clip "
                           "-- it can never fire, in any language")
    return bad


def anchor_english(html: str, spec: dict, durations: dict, clauses: dict) -> tuple:
    """(html with data-en on every anchored beat, complaints)."""
    bad = []

    def scene(m):
        sid, body = m.group(1), m.group(2)
        rec = clauses.get(sid)
        if rec is None:
            bad.append(f"{sid}: clauses.json has no English clauses; run measure_clauses.py")
            return m.group(0)
        clip = (AUDIO / "en" / f"{sid}.mp3").read_bytes()
        text = spec["text"]["en"][sid].encode("utf-8")
        if (rec["mp3_sha256"] != hashlib.sha256(clip).hexdigest()
                or rec["text_sha256"] != hashlib.sha256(text).hexdigest()):
            bad.append(f"{sid}: clauses.json was measured from a different clip or text "
                       "than the one shipped; run measure_clauses.py")
            return m.group(0)
        starts = rec["clauses"]

        def beat(b):
            idx, plus, t = b.group(1), b.group(2), float(b.group(3))
            if idx is None:
                if t >= ANCHOR_FROM:
                    bad.append(f"{sid}: beat at {t}s names no English clause (data-at), so "
                               "in English it would fire at a proportional estimate")
                return b.group(0)
            if int(idx) >= len(starts):
                bad.append(f"{sid}: data-at={idx} but the scene has {len(starts)} clauses")
                return b.group(0)
            en = round(starts[int(idx)][1] + float(plus or 0), 2)
            if en >= durations["en"][sid]:
                bad.append(f"{sid}: clause {idx} resolves to {en}s, past the English clip")
            return f'{b.group(0)} data-en="{en}"'

        return m.group(0).replace(body, _BEAT.sub(beat, body), 1)

    out = re.sub(r'<section class="scene" id="([^"]+)"([\s\S]*?)</section>', scene, html)
    return out, bad


def sentence_starts(text: str) -> list:
    return [m.start() for m in _SENTENCE.finditer(text)]


def columns(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def caption_pages(spec: dict, clauses: dict) -> tuple:
    """({lang: {scene: [[start char, end char, English start seconds or None], ...]}}, complaints)."""
    out, bad = {lang: {} for lang in spec["langs"]}, []
    for sid in spec["scenes"]:
        ref = spec["text"][CAPTION_REF][sid]
        rs = sentence_starts(ref)
        idx = [0]
        for k in range(1, CAPTION_PAGES):
            nxt = min((i for i in range(1, len(rs)) if i > idx[-1]),
                      key=lambda i: abs(rs[i] - len(ref) * k / CAPTION_PAGES), default=None)
            if nxt is not None:
                idx.append(nxt)
        fracs = [rs[i] / len(ref) for i in idx]
        for lang in spec["langs"]:
            text = spec["text"][lang][sid]
            ss = sentence_starts(text)
            if all(i < len(ss) and abs(ss[i] / len(text) - f) <= PAGE_MATCH
                   for i, f in zip(idx, fracs)):
                starts = [ss[i] for i in idx]
            else:
                starts = [0]
                for f in fracs[1:]:
                    starts.append(min((a for a in ss if a > starts[-1]),
                                      key=lambda a: abs(a / len(text) - f)))
            for k, prefix in enumerate(
                    spec.get("caption_breaks", {}).get(sid, {}).get(lang) or [], start=1):
                if prefix is None:
                    continue
                at = [x for x in ss if text.startswith(prefix, x)]
                if not at or k >= len(starts):
                    bad.append(f"{lang}/{sid}: caption break {prefix!r} begins no sentence")
                    continue
                starts[k] = at[0]
            if starts != sorted(set(starts)):
                bad.append(f"{lang}/{sid}: caption pages out of order: {starts}")
            ends = starts[1:] + [len(text)]
            pages = []
            for a, b in zip(starts, ends):
                if columns(text[a:b].strip()) > PAGE_COLS:
                    bad.append(f"{lang}/{sid}: a caption page is {columns(text[a:b].strip())} "
                               f"columns, over the {PAGE_COLS} two lines hold")
                t = None
                if lang == "en":
                    hit = [c[1] for c in clauses[sid]["clauses"] if c[0] == a]
                    if not hit:
                        bad.append(f"en/{sid}: a caption page starts at character {a}, "
                                   "which begins no measured clause")
                    t = hit[0] if hit else None
                pages.append([a, b, t])
            out[lang][sid] = pages
    return out, bad


def clip_versions(langs, scenes) -> dict:
    """{lang: {scene: first 12 hex of the clip's sha256}}, for the clip URLs."""
    return {l: {s: hashlib.sha256((AUDIO / l / f"{s}.mp3").read_bytes()).hexdigest()[:12]
                for s in scenes if (AUDIO / l / f"{s}.mp3").exists()} for l in langs}


def build() -> str:
    spec = json.loads(NARRATION.read_text(encoding="utf-8"))
    durations = json.loads(DURATIONS.read_text(encoding="utf-8"))
    html = TEMPLATE.read_text(encoding="utf-8")

    # Strip the editor-facing comment BEFORE substituting -- see _LEAD_COMMENT.
    html, n = _LEAD_COMMENT.subn(r"\1" + _BANNER, html, count=1)
    if n != 1:
        raise SystemExit("FATAL: the template's leading <!-- ... --> comment was not "
                         "found. It must stay directly after <!doctype html>, because "
                         "it names the placeholders and would otherwise be substituted "
                         "into.")

    scenes, langs = spec["scenes"], spec["langs"]

    # The scene list in the template must BE the scene list in narration.json. If they
    # drift, the player advances to a scene with no markup (a blank stage) or a scene
    # exists that the player never reaches -- both look like the page is broken, and
    # neither raises anything.
    in_page = re.findall(r'<section class="scene" id="([^"]+)"', html)
    if in_page != scenes:
        raise SystemExit("FATAL: page scenes != narration.json scenes\n"
                         f"  page:      {in_page}\n  narration: {scenes}")

    missing = [f"{l}/{s}" for l in langs for s in scenes
               if s not in durations.get(l, {})]
    if missing:
        raise SystemExit("FATAL: durations.json is missing measured clips, so the page "
                         "would fall back to browser TTS for them:\n  "
                         + ", ".join(missing) + "\nRun synth_narration.py first.")

    bad = check_beats(html, durations["en"])
    if bad:
        raise SystemExit("FATAL: unreachable animation beats:\n  " + "\n  ".join(bad))
    clauses = json.loads(CLAUSES.read_text(encoding="utf-8"))["en"]
    html, bad = anchor_english(html, spec, durations, clauses)
    if bad:
        raise SystemExit("FATAL: English beats not on the English voice:\n  "
                         + "\n  ".join(bad))

    pages, bad = caption_pages(spec, clauses)
    if bad:
        raise SystemExit("FATAL: captions:\n  " + "\n  ".join(bad))

    subs = {
        "__SCENES__": json.dumps(scenes),
        "__LANGS__": json.dumps(
            {k: {"name": v["name"], "bcp47": v["bcp47"]} for k, v in langs.items()},
            ensure_ascii=False),
        "__TITLES__": json.dumps(spec["titles"], ensure_ascii=False),
        "__NARR__": json.dumps(spec["text"], ensure_ascii=False),
        "__DUR__": json.dumps(durations),
        "__AUDIO_V__": json.dumps(clip_versions(langs, scenes)),
        "__CAPS__": json.dumps(pages),
    }
    for ph, (prefix, rel) in DIAGRAMS.items():
        subs[ph] = load_svg(rel, prefix)

    # EXACTLY one of each. "is it present" is not the check: a template that lost a
    # placeholder builds a page with a hole in it, and a template that grew a second copy
    # inlines the same 17 KB diagram twice -- which also duplicates every id the
    # namespacing above just made unique. Both are silent in the browser.
    for ph, value in subs.items():
        n = html.count(ph)
        if n != 1:
            raise SystemExit(f"FATAL: template has {n} occurrences of {ph}, expected 1")
        html = html.replace(ph, value, 1)
    left = re.findall(r"__[A-Z_]+__", html)
    if left:
        raise SystemExit(f"FATAL: unsubstituted placeholders remain: {sorted(set(left))}")
    return html


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="path to write the built page to")
    args = ap.parse_args()
    html = build()
    out = pathlib.Path(args.out)
    out.write_text(html, encoding="utf-8")
    print(f"wrote {out} ({len(html) / 1024:.0f} KB, "
          f"{len(re.findall(r'data-t=', html))} beats)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
