#!/usr/bin/env python3
"""The intro walkthrough is presented on the repo page in the one form that plays.

The film used to be committed here as a 10 MB mp4, and most of this module was a container
reader that checked the bytes: moov before mdat, an audio track as long as the video, the
authored frame size, yuv420p, length against the summed narration clips. That artifact is
gone -- the walkthrough is served by the `user-attachments` upload that GitHub renders as a
real `<video>` element, and a second committed copy of the same five minutes was weight in
every clone with nothing pointing at it once the poster link went.

So what is left to guard is not the bytes, it is the PRESENTATION, and that is now the whole
of the promise:

  - a bare `user-attachments` URL alone in its paragraph, in both languages, same upload.
    This is the ONLY form GitHub promotes to a player -- measured through POST /markdown
    mode=gfm, not assumed -- and it is now the only way to watch the film from the repo page.
    There is no second path behind it.
  - no hand-written `<video>` tag. GitHub's sanitizer deletes it, so it renders as an empty
    gap; six embed forms were measured and all six are erased.
  - no size or encoder setting typed into the section, and no budget amount.
  - no video file committed anywhere in the tree. The film was committed once, and a guard
    is what stops the next 10 MB from arriving the same way.
  - the recorder still records at the size the page is authored at. This is the one property
    of the RECORDING that survives the file's deletion, and it kept its guard deliberately:
    record_video.py holds its own STAGE_W/STAGE_H copy, so nothing but a comparison stops
    the two drifting and the next recording shipping every diagram resampled.

What was genuinely lost, said plainly rather than left for someone to discover: with no
committed mp4 there is nothing to read a moov atom out of, so faststart, the audio mux, the
pixel format and the recording's length against its narration are no longer checked by
anything. They were properties of an artifact this repo no longer has. The narration side is
untouched -- tests/test_intro_bundle.py still re-measures all 35 clips from their own bytes.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
INTRO = REPO / "deploy" / "console" / "intro"

#: Language-independent, and the one string in the section whose presence is separately
#: guaranteed -- by test_both_readmes_carry_the_inline_player_url below. The section used to be
#: located by the committed mp4's path, which is exactly what broke when that path was deleted:
#: three guards that had nothing to do with the file failed because their ANCHOR was the file.
#: A heading would work too and was rejected -- it differs per language and gets reworded by
#: translation polish, which would fail these guards for a reason that is not their subject.
SECTION_ANCHOR = "user-attachments/assets/"


def _stage_size() -> tuple[int, int]:
    """The authored stage size, read from the page's own CSS.

    `.stage` is the box every scene is positioned inside; fit() scales it, so this is the size
    at which scale == 1 and nothing is resampled. Read from the page rather than from
    record_video.py on purpose: the recorder keeps its own copy, and a check against the
    RECORDER's number would stay green while both drifted away from what the page lays out.
    """
    css = (INTRO / "page.template.html").read_text()
    m = re.search(r"\.stage\s*\{[^}]*?width:\s*(\d+)px;\s*height:\s*(\d+)px", css, re.S)
    assert m, ("could not find the .stage width/height in page.template.html — the authored "
               "size this guard compares the recorder against is derived from there")
    return int(m.group(1)), int(m.group(2))


def test_the_recorder_records_at_the_size_the_page_is_authored_at():
    """record_video.py's stage constants must equal the page's `.stage` box.

    This is what is left of the frame-size guarantee. It used to be asserted against the
    committed recording's coded frame size, read from the mp4's sample entry; with no committed
    recording, the comparison moves one step upstream to the two places the number is written
    down. That is a weaker guard and it is worth being explicit about why it is not nothing: a
    recorder pointed at a stage it does not match produces a video where fit() scales the stage,
    and every diagram in it ships resampled. Nobody notices that by watching once, which is why
    it was measured rather than eyeballed in the first place.

    Both numbers are DERIVED, neither is retyped here -- a guard that hardcoded 1180x664 would
    catch a typo in one file and miss the two of them being changed together, which is the
    likelier direction (someone re-authors the stage and updates the recorder to match, without
    knowing a recording exists that no longer fits either).
    """
    src = (INTRO / "record_video.py").read_text()
    m = re.search(r"^STAGE_W,\s*STAGE_H\s*=\s*(\d+),\s*(\d+)$", src, re.M)
    assert m, ("record_video.py no longer defines STAGE_W, STAGE_H on one line — this guard "
               "reads the recorder's copy of the stage size from there")
    recorder = (int(m.group(1)), int(m.group(2)))
    assert recorder == _stage_size(), (
        f"record_video.py records at {recorder[0]}x{recorder[1]} but page.template.html "
        f"authors the stage at {_stage_size()[0]}x{_stage_size()[1]} — fit() would scale the "
        "stage and every diagram in the recording would ship resampled")


def test_no_video_file_is_committed_to_this_repo():
    """The film is hosted, not committed, and this is what keeps it that way.

    Not a size rule for its own sake. A five-minute h264 recording of this page is ~10 MB, it
    was committed once (and then had to be deleted), and the reason it looked reasonable at the
    time is that a README link made it feel necessary. It is not: a bare user-attachments URL
    renders a player, GitHub hosts the bytes, and the repo stops paying for them in every clone.

    Derived from `git ls-files` rather than from a directory walk, so an untracked local
    recording -- which is exactly what running record_video.py produces -- does not fail this.
    The rule is about what is COMMITTED.

    Said plainly because a guard cannot: deleting the mp4 from the tree did not remove it from
    history. It entered at cdeea2d and every clone still fetches that blob. What this prevents
    is the next one.
    """
    tracked = subprocess.run(["git", "ls-files"], capture_output=True, text=True,
                             cwd=REPO, check=True).stdout.split()
    assert tracked, "git reported no tracked files — this guard checked nothing"
    videos = sorted(p for p in tracked
                    if p.lower().endswith((".mp4", ".mov", ".webm", ".mkv", ".avi")))
    assert not videos, (
        f"video file(s) committed to the repo: {videos}. The walkthrough is served by the "
        "user-attachments upload the READMEs carry, which GitHub hosts and renders as a "
        "player; a committed copy is megabytes in every clone, forever, for a second path to "
        "the same five minutes. Upload it as a release or an attachment instead.")


@pytest.mark.parametrize("readme", ["README.md", "README.zh-TW.md"])
def test_neither_readme_hand_writes_a_video_tag(readme):
    """A `<video>` tag renders as an empty gap, and it is the obvious thing to reach for.

    Measured against GitHub's own POST /markdown mode=gfm rather than assumed: the sanitizer
    deletes the element for the repo-relative path, a raw.githubusercontent.com URL, a
    `<source>` child and a release asset alike, and the rendered homepage of this repo confirmed
    it with zero `<video>` elements. The form that DOES play is a bare user-attachments URL,
    which is not a tag at all -- GitHub's renderer recognises the link and builds the element
    itself. Both facts are compatible, and confusing them is what put a stripped tag in this
    README once already.

    Split out of the guard that also asserted the README reached the committed mp4. That half
    died with the file; this half is about what renders and is unaffected by where the bytes
    live, so it is a test of its own now rather than an assertion riding on a deleted premise.

    Code spans are stripped before matching, because the naive version of this assertion failed
    on the prose that documented the defect.
    """
    prose = re.sub(r"`[^`]*`", "", (REPO / readme).read_text())
    assert not re.search(r"<video\b", prose), (
        f"{readme} has a <video> tag — GitHub's sanitizer deletes it, so it renders as an empty "
        "gap. A bare https://github.com/user-attachments/assets/<uuid> URL alone in its "
        "paragraph is the form that plays")


ASSET_URL = re.compile(
    r"^https://github\.com/user-attachments/assets/"
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.M)


def test_both_readmes_carry_the_inline_player_url():
    """The one form of link GitHub turns into a player, in both languages, identical.

    This guard now carries the whole promise on its own. It used to sit beside one asserting the
    README reached a committed copy of the same film, so a rewrite that broke the player still
    left a path to the bytes; that copy is deleted, so if this URL goes, the walkthrough is
    unreachable from the repo page and nothing else notices.

    Measured, not assumed, via POST /markdown mode=gfm: a bare user-attachments URL ALONE IN
    ITS PARAGRAPH renders <details open> + <video controls>. Hence `^...$` with re.M and the
    blank-line checks -- the same URL wrapped in `[text](url)`, or with prose trailing it in the
    same paragraph, is a different input and this repo does not get to guess which ones GitHub
    promotes. It cannot be verified offline either (the asset sits behind a signed JWT), so the
    check is on the FORM that was verified online, held to the letter.

    The single-uuid assertion is what would have caught the two single-language pull requests
    that deleted the poster link from one README each: the pair is only equal while something
    compares them, and neither copy can be diffed because both URLs are opaque uploads.
    """
    urls = set()
    for readme in ("README.md", "README.zh-TW.md"):
        text = (REPO / readme).read_text()
        found = ASSET_URL.findall(text)
        assert found, (
            f"{readme} has no bare https://github.com/user-attachments/assets/<uuid> line — "
            "that URL alone in a paragraph is the ONLY form GitHub renders as a player, and "
            "since the committed copy of the film was removed it is the only way to watch the "
            "walkthrough from the repo page at all")
        for url in found:
            i = text.index(url)
            before, after = text[:i], text[i + len(url):]
            assert before.endswith("\n\n"), (
                f"{readme}: the player URL is not alone in its paragraph (text precedes it on "
                "the same block) — GitHub only promotes a URL that stands by itself")
            assert after.startswith("\n\n") or after.strip() == "", (
                f"{readme}: prose follows the player URL inside the same paragraph — GitHub "
                "only promotes a URL that stands by itself")
        urls.update(found)
    assert len(urls) == 1, (
        f"the two READMEs point at different uploads ({sorted(urls)}) — a reader of one would "
        "watch a different film from a reader of the other")


def _section_containing(text: str, needle: str) -> str:
    """The `## …` section the needle sits in, from its heading to the next heading.

    Sliced by heading rather than by a character window on purpose. A window wide enough to
    cover this section also reaches into the next one -- and the next one is `## What it is`,
    which states the platform's own configured reporting reference in prose that predates this
    change and is deliberately out of scope. A guard that fails on text it was never meant to
    police is a guard someone deletes.
    """
    # Asserted, not `.index`. A README with the anchor deleted made this raise
    # `ValueError: substring not found`, which reports as a broken test rather than as a README
    # whose walkthrough section cannot be found.
    assert needle in text, (
        f"{needle!r} is not in this README, so the walkthrough section cannot be located")
    i = text.index(needle)
    start = text.rfind("\n## ", 0, i)
    assert start != -1, "the walkthrough anchor is not inside a ## section"
    end = text.find("\n## ", i)
    return text[start:end if end != -1 else len(text)]


def test_the_walkthrough_section_states_no_number_it_does_not_derive():
    """No retyped size or encoder setting anywhere in the walkthrough section, either language.

    The two numbers this replaced -- "10.7 MB" and "CRF 26" -- described a file this repo no
    longer contains, which makes the rule stronger rather than weaker: a size written next to
    the player now cannot be right even on the day it is typed, because the bytes a reader
    downloads are GitHub's re-encode and nothing here can measure them. "How big is the
    download" stays the obvious thing for the next person to add, and this is where they find
    out why not.

    Scoped to the walkthrough section, not the whole README: docs/COST.md and the cost prose
    elsewhere legitimately carry figures.
    """
    # Both patterns are about a claim ABOUT THE FILE, so both are anchored on units, not on bare
    # digits -- "5:04" and "0/16" are facts about the film and the gate, not retyped measurements.
    bad = re.compile(r"\b\d+(?:\.\d+)?\s*[MK]B\b|\bCRF[\s-]*\d+", re.I)
    for readme in ("README.md", "README.zh-TW.md"):
        section = _section_containing((REPO / readme).read_text(), SECTION_ANCHOR)
        hit = bad.search(section)
        assert not hit, (
            f"{readme}: the walkthrough section states {hit.group(0)!r} — a file size or encoder "
            "setting typed into prose is measured once and reads as measured forever (the last "
            "one said 10.7 MB and stayed while the file was re-encoded). The film is no longer "
            "committed here, so there is nothing in this repo to derive it from; leave it out")


def test_the_video_section_names_no_budget_amount():
    """The no-amount rule covers the walkthrough's framing too, in both languages.

    The narration itself contains no figure; this catches a caption or a "what you'll see"
    summary reintroducing one right next to the player, where a reader would read it as the
    product's number rather than as one team's setting.
    """
    bad = re.compile(r"\$\s?20[,.]?000|20[,.]?000\s*(?:USD|dollars|美元)|兩萬|二萬|2\s*萬")
    for readme in ("README.md", "README.zh-TW.md"):
        section = _section_containing((REPO / readme).read_text(), SECTION_ANCHOR)
        hit = bad.search(section)
        assert not hit, (
            f"{readme}: the walkthrough section names a budget amount ({hit.group(0)!r}); "
            "the reporting reference is set by each team and no figure belongs here")
