# Intent: the English Introduction shows each picture on its own words

- **Slug:** intro-en-timing
- **Author:** Claude (AI agent)
- **Date:** 2026-10-27
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's chat report of 2026-10-27, after playing the deployed v2 Introduction:
Mandarin and Cantonese match; English does not; use the Mandarin and Cantonese versions as the
reference.

## Problem

Every beat was placed at its sentence's character offset x the English clip length, which assumes
a voice that speaks at an even rate. The other languages scale those times by their clip length.
The English voice (Polly generative, Matthew) does not speak evenly. Measured against the v2 clips,
scene 6 drew "judge", "deploy", "test" and "teardown" about 3 s before they were said and "$0.45"
2.7 s early, and scene 5 ran 1-2 s early throughout.

Separately, `tests/test_intro_player.js` (the test that drives the player's real JavaScript) was
not run by pytest or CI, and it had been failing five assertions on v1 durations since v2 shipped.

## Outcome wanted

- In English, every beat appears when the voice starts the clause it illustrates, measured from the
  clip, not estimated.
- Mandarin, Cantonese, Japanese and Korean timing unchanged.
- The player test runs in the suite, and its numbers come from `durations.json`.

## Acceptance

- `tests/test_intro_timing.py` (new) passes, including the player test under node; negative
  controls.
- The deployed Introduction in English, played by the owner.
