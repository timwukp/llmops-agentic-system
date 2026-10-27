# Spec: English beats on the English voice

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **Measurement** (`deploy/console/intro/measure_clauses.py`, new, offline, ffmpeg): split each
   English scene into clauses (text up to `, . : ; ?`), find the pauses with silencedetect, and
   assign each clause a start by dynamic programming over monotone assignments that keep every
   clause's length closest to its character length at the scene's rate. Write
   `deploy/console/intro/clauses.json` with each clip's and text's sha256.
2. **Anchors** (`deploy/console/intro/page.template.html`): every beat at or after 1.5 s names the
   English clause it illustrates, `data-at="<clause>"` or `"<clause>+<seconds>"`, placed before its
   `data-t`. `data-t` is unchanged.
3. **Build** (`deploy/console/intro/build_intro.py`): resolve each anchor to `data-en` (the
   clause's measured start plus the offset). Fail the build on a beat at or after 1.5 s with no
   anchor, an anchor past the scene's clause count or clip, and a `clauses.json` measured from a
   different clip or text than the one shipped.
4. **Player**: English uses `data-en`; every other language keeps `data-t` x its clip length over
   the English one.
5. **Guards:** `tests/test_intro_timing.py` (new) checks the sha256s, that the clauses are the
   narration in order, that every beat is anchored and resolved, and runs
   `tests/test_intro_player.js` under node (fails on CI if node is missing).
   `tests/test_intro_player.js` derives its durations from `durations.json` and asserts that English
   scene 6 follows the measured clauses and Japanese scene 4 the scaled `data-t`. Negative controls.
