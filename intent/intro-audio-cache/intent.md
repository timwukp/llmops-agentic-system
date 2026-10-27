# Intent: a changed narration clip reaches the listener

- **Slug:** intro-audio-cache
- **Author:** Claude (AI agent)
- **Date:** 2026-10-27
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's chat report of 2026-10-27, after the English timing fix was deployed: the
English script is not updated, use the updated script (cross-check with Mandarin and Cantonese).

## Problem

The deployed English clips are v2: byte-identical to the repo (35/35), and every clip's synthesis
stamp matches the current v2 text in all five languages. What the owner heard was the v1 English.
The page requested `/intro/audio/<lang>/<scene>.mp3`, the same URL in every version, and the route
lets a browser keep a clip for a day (`max-age=86400`), so a browser that had played v1 kept playing
it. Nothing on the server can see that; the route's own comment said the cache was "keyed on the
deployment", which a browser cache is not.

## Outcome wanted

- A clip whose bytes change is requested under a new URL, so no cache can serve the old one.
- The server is unchanged in what it serves.

## Acceptance

- `tests/test_intro_bundle.py` checks the version of all 35 clip URLs against the committed mp3s;
  `tests/test_intro_player.js` checks the URL the player actually requests; negative controls.
- The owner hears the v2 English in the deployed console.
