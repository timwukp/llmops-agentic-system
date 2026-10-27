# Intent: the captions show the same sentences in every language

- **Slug:** intro-captions
- **Author:** Claude (AI agent)
- **Date:** 2026-10-27
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's chat report of 2026-10-27, with screenshots of scene 1 at 0:06 in English,
Mandarin and Cantonese: Mandarin is the same as Cantonese, English is different; check all the
consistency.

## Problem

The caption was a 150-character window centred on the clip's proportional position. 150 characters
is two thirds of a Chinese scene and a fifth of an English one (680 characters in scene 1). At 0:06
Mandarin and Cantonese showed the scene from its first sentence; English showed
"… ned on a store's customer conversations …", cut mid-word with the first sentence gone. The owner
read it as an English script that had not been updated. The script and the clips were v2.

## Outcome wanted

- At any moment, all five languages caption the same sentences of the scene, starting on a sentence.
- No caption page is wider than two caption lines.

## Acceptance

- `tests/test_intro_captions.py` (new); `tests/test_intro_player.js` asserts the owner's exact
  frame (English scene 1 at 0:06 starts with its first sentence) in all five languages; negative
  controls.
- The owner compares the three languages at the same frame in the deployed console.
