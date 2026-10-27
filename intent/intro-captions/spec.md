# Spec: caption pages that break between sentences

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **Pages** (`deploy/console/intro/build_intro.py`, `caption_pages`): three pages per scene. Mandarin
   breaks at the sentence start nearest each third of its text. A language whose sentence with the
   same number starts within 0.08 of the same fraction breaks there (English and Cantonese, in every
   scene); otherwise at the sentence start nearest Mandarin's fraction, unless
   `narration.json`'s `caption_breaks` names the sentence (three Japanese/Korean breaks). The build
   fails on a page wider than 350 columns (CJK counts two), a named break that begins no sentence,
   or an English page that begins no measured clause.
2. **Player** (`page.template.html`): show the current page whole, with "…" where text continues.
   English turns the page at the measured clause; the other languages, and English under the TTS
   fallback, at their share of the clip.
3. **Guards:** `tests/test_intro_captions.py` (new); `tests/test_intro_player.js` checks scene 1 at
   0:06 in all five languages and English's turn at 12.5 s. Two negative controls.
