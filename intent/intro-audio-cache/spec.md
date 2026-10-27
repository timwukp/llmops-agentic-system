# Spec: clip URLs carry the clip's hash

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **Build** (`deploy/console/intro/build_intro.py`): substitute `__AUDIO_V__` with the first 12 hex
   of each committed clip's sha256, per language and scene.
2. **Player** (`deploy/console/intro/page.template.html`): request
   `/intro/audio/<lang>/<scene>.mp3?v=<that hash>`.
3. **Servers:** the Lambda route already reads the path only (its cache comment is corrected); the
   recorder's local server (`deploy/console/intro/record_video.py`) strips the query.
4. **Guards:** `tests/test_intro_bundle.py` derives every version from the mp3s and checks the
   route serves a versioned request; `tests/test_intro_player.js` derives the expected URL from the
   mp3 with node's crypto. Two negative controls (no version; a version that is not the hash).
