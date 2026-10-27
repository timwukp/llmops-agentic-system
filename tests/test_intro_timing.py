"""The Introduction's pictures appear on the words that describe them, in English as well.

Beats were authored at each sentence's character offset x the clip length, which assumes a voice
that speaks at an even rate. The four other languages scale those times by their own clip length
and the owner judged them right; the English voice (generative) does not speak evenly, and on
2026-10-27 the owner reported English as the one that did not match. Measured, scene 6 drew
"deploy", "test" and "teardown" about 3 s before they were said. So each beat now names the
English clause it illustrates (data-at), measure_clauses.py records when every clause starts, and
build_intro.py writes that time as data-en, which the player uses for English only.

These tests hold the three pieces together, and run the player itself (tests/test_intro_player.js,
which nothing ran before this file: it had been failing five assertions on v1 numbers).
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import pathlib
import re
import shutil
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
INTRO = REPO / "deploy/console/intro"
NARR = json.loads((INTRO / "narration.json").read_text(encoding="utf-8"))
DUR = json.loads((INTRO / "durations.json").read_text(encoding="utf-8"))
CLAUSES = json.loads((INTRO / "clauses.json").read_text(encoding="utf-8"))["en"]
TEMPLATE = (INTRO / "page.template.html").read_text(encoding="utf-8")


def _module(name):
    spec = importlib.util.spec_from_file_location(name, INTRO / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


BUILD, MEASURE = _module("build_intro"), _module("measure_clauses")


def _scenes(html):
    return re.findall(r'<section class="scene" id="([^"]+)"([\s\S]*?)</section>', html)


@pytest.mark.parametrize("sid", NARR["scenes"])
def test_the_clauses_were_measured_from_the_clip_and_text_that_ship(sid):
    rec = CLAUSES[sid]
    clip = (INTRO / "audio/en" / f"{sid}.mp3").read_bytes()
    assert rec["mp3_sha256"] == hashlib.sha256(clip).hexdigest(), (
        f"{sid}.mp3 changed after its clauses were measured: run measure_clauses.py")
    assert rec["text_sha256"] == hashlib.sha256(NARR["text"]["en"][sid].encode()).hexdigest()


@pytest.mark.parametrize("sid", NARR["scenes"])
def test_the_clauses_are_the_narration_in_order(sid):
    rec = CLAUSES[sid]["clauses"]
    assert [[o, t] for o, _, t in rec] == [list(c) for c in MEASURE.clauses(NARR["text"]["en"][sid])]
    starts = [s for _, s, _ in rec]
    assert starts[0] == 0 and all(a < b for a, b in zip(starts, starts[1:]))
    assert starts[-1] < DUR["en"][sid]


def test_every_beat_after_the_opening_names_the_english_clause_it_shows():
    unanchored = [f"{sid}@{t}" for sid, body in _scenes(TEMPLATE)
                  for at, t in re.findall(r'(data-at="[^"]*" )?data-t="([0-9.]+)"', body)
                  if not at and float(t) >= BUILD.ANCHOR_FROM]
    assert not unanchored, f"beats with no English clause: {unanchored}"


def test_the_built_page_carries_an_english_time_for_every_anchor():
    html = BUILD.build()
    for sid, body in _scenes(html):
        anchors = re.findall(r'data-at="([0-9]+)(?:\+([0-9.]+))?" data-t="[0-9.]+" data-en="([0-9.]+)"', body)
        assert len(anchors) == body.count("data-at="), f"{sid}: an anchor was not resolved"
        for idx, plus, en in anchors:
            assert float(en) == round(CLAUSES[sid]["clauses"][int(idx)][1] + float(plus or 0), 2)


def test_the_player_uses_the_english_time_for_english_only():
    """The template line, so a refactor that drops the English branch fails here too."""
    assert re.search(r'LANG === "en" && el\.dataset\.en \? parseFloat\(el\.dataset\.en\)', TEMPLATE)


def test_the_player_script_passes(tmp_path):
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is not on the CI runner, so the player test cannot run")
        pytest.skip("node is not installed")
    page = tmp_path / "intro.html"
    page.write_text(BUILD.build(), encoding="utf-8")
    run = subprocess.run([node, str(REPO / "tests/test_intro_player.js")], capture_output=True,
                         text=True, env={**os.environ, "INTRO_HTML": str(page)}, timeout=120)
    assert run.returncode == 0 and " 0 failed" in run.stdout, run.stdout[-2000:] + run.stderr[-1000:]
