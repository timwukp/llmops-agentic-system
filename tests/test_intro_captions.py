"""The Introduction's captions show the same sentences in every language at the same moment.

The caption was a 150-character window centred on the clip's proportional position: two thirds of
a Chinese scene and a fifth of an English one. The owner's screenshots (2026-10-27) are English
scene 1 at 0:06 showing "… ned on a store's customer conversations …" while Mandarin and Cantonese
showed the scene from its first sentence, and they read it as an English script that had not been
updated. Each scene is now three caption pages that break between sentences (build_intro.py).
"""
from __future__ import annotations

import importlib.util
import json
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
INTRO = REPO / "deploy/console/intro"
NARR = json.loads((INTRO / "narration.json").read_text(encoding="utf-8"))
CLAUSES = json.loads((INTRO / "clauses.json").read_text(encoding="utf-8"))["en"]

_spec = importlib.util.spec_from_file_location("build_intro", INTRO / "build_intro.py")
BUILD = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(BUILD)
PAGES, COMPLAINTS = BUILD.caption_pages(NARR, CLAUSES)


def _sentence(lang, sid, k):
    text = NARR["text"][lang][sid]
    return BUILD.sentence_starts(text).index(PAGES[lang][sid][k][0])


def test_the_captions_build_with_no_complaint():
    assert COMPLAINTS == []


@pytest.mark.parametrize("sid", NARR["scenes"])
def test_every_language_has_the_same_pages_and_each_starts_a_sentence(sid):
    for lang in NARR["langs"]:
        pages = PAGES[lang][sid]
        assert len(pages) == BUILD.CAPTION_PAGES, f"{lang}/{sid}: {len(pages)} pages"
        assert pages[0][0] == 0 and pages[-1][1] == len(NARR["text"][lang][sid])
        for k in range(len(pages)):
            _sentence(lang, sid, k)          # raises if a page starts mid-sentence


@pytest.mark.parametrize("lang", ["en", "yue"])
@pytest.mark.parametrize("sid", NARR["scenes"])
def test_english_and_cantonese_turn_the_page_at_mandarins_sentence(sid, lang):
    """The three are written sentence for sentence, so the same page is the same sentences."""
    want = [_sentence(BUILD.CAPTION_REF, sid, k) for k in range(BUILD.CAPTION_PAGES)]
    assert [_sentence(lang, sid, k) for k in range(BUILD.CAPTION_PAGES)] == want


@pytest.mark.parametrize("sid", NARR["scenes"])
def test_no_page_is_wider_than_two_caption_lines(sid):
    for lang in NARR["langs"]:
        text = NARR["text"][lang][sid]
        for a, b, _ in PAGES[lang][sid]:
            assert BUILD.columns(text[a:b].strip()) <= BUILD.PAGE_COLS, f"{lang}/{sid}"


@pytest.mark.parametrize("sid", NARR["scenes"])
def test_english_turns_the_page_on_the_measured_clause(sid):
    starts = {o: s for o, s, _ in CLAUSES[sid]["clauses"]}
    for a, _, t in PAGES["en"][sid]:
        assert t == starts[a], f"en/{sid}: page at character {a} turns at {t}, voice at {starts[a]}"


def test_every_named_break_is_used():
    """A break in narration.json that no longer begins a sentence would be ignored silently."""
    for sid, langs in NARR.get("caption_breaks", {}).items():
        for lang, breaks in langs.items():
            text = NARR["text"][lang][sid]
            for k, prefix in enumerate(breaks, start=1):
                if prefix is not None:
                    assert text.startswith(prefix, PAGES[lang][sid][k][0]), f"{lang}/{sid} page {k + 1}"
