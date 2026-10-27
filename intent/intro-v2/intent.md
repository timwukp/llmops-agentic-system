# Intent: the Introduction tells the platform as it is now

- **Slug:** intro-v2
- **Author:** Claude (AI agent)
- **Date:** 2026-10-27
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's chat decisions of 2026-10-27: update Get started → Introduction to the
current design, repo first and then the app; the v2 script direction ("好，照這個方向，翻譯成另外三種語言").

## Problem

The Introduction (narration v1, 2026-08-04) predates the lakehouse, the governance plane, Unified
Studio, the provenance card and identifier grounding. It speaks numbers that are no longer true
("six Lambdas", "eleven harness task states"), presents the August 0/16 as the result, shows
`claude-opus-5` as the harness model (all seven run `claude-fable-5`), and frames the agents as
replacing people ("seven agents hold the pager", "glue work, and being on call"), against the
owner's standing instruction. Its comment cites `tests/test_intro_narration.py` as re-deriving every
number; that file never existed.

## Outcome wanted

- A v2 script in five languages on data + AI governance, from raw data to an approved model; every
  number from a file, re-derived by a test that exists.
- Seven scenes redrawn to match, beats on the sentences they illustrate.
- The promise on the page matches the audio.

## Acceptance

- `tests/test_intro_narration.py` (new) and `tests/test_intro_bundle.py` pass; negative controls.
- The deployed Introduction, played by the owner.
