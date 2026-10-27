# Spec: Introduction v2

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **Script** (`deploy/console/intro/narration.json`, version 2): the same seven scene ids; new
   titles and text in en, zh (Simplified, for the Mandarin voice), yue, ja, ko; numbers spelled out
   per language; the comment's list of numbers and sources rewritten. No test, negative-control or
   shell count is spoken, and no figure for the cost-approval reference.
2. **Audio** (`deploy/console/synth_narration.py --force`): 35 clips and `durations.json` measured.
3. **Scenes** (`deploy/console/intro/page.template.html`): seven scenes redrawn with the template's
   existing styles; each beat placed on the English sentence it illustrates; the start screen says
   data + AI governance and four minutes.
4. **The promise:** English runs 4.3 min, so "five-minute" becomes "four-minute" in
   `deploy/console/frontend.html`, `deploy/console/README.md` and both READMEs (the READMEs' embedded
   recording is v1 and is labelled so); `tests/test_intro_bundle.py`'s window moves to 225–315 s.
5. **Guards:** `tests/test_intro_narration.py` derives every spoken and displayed number from its
   source (state machine, deployer, configs, evidence files), checks the same numbers in all five
   languages, and checks no language frames the agents as replacing people; negative controls.
