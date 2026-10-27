# VERIFICATION — Introduction v2 (2026-10-27)

## What the owner asked for

Check Get started → Introduction against the current design and update it, repo first and then
the app; then "好，照這個方向，翻譯成另外三種語言" after reviewing the English and Chinese draft.

## Found in v1

- **Stale numbers spoken:** "six Lambdas" (8 today), "eleven harness task states" (39 states, 13
  harness tasks on the happy path); the result shown was August's 0/16.
- **Wrong config shown:** scene 4 displayed `"global.anthropic.claude-opus-5"`; all seven harnesses
  run `global.anthropic.claude-fable-5`.
- **Framing against the owner's instruction:** "seven agents hold the pager", opening on engineers'
  "glue work, and being on call", in all five languages.
- **A guard that never existed:** narration.json's comment said `tests/test_intro_narration.py`
  re-derives every number. No such file had ever been committed.

## Built

- **Script v2:** seven scenes, five languages, numbers spelled per language; the story is data + AI
  governance (a model that passed every gate and was still wrong; four planes; what was proven).
- **Audio:** `synth_narration.py --force`, 35 clips, 13,424 characters (≈ $0.32 of Polly at the
  generative and neural rates). Measured lengths: en 257.8 s (4.30 min), zh 310.6 s, yue 342.1 s,
  ja 395.5 s, ko 352.1 s.
- **Scenes:** all seven redrawn with the template's existing styles; each beat on the English
  sentence it illustrates (sentence start = character offset × measured clip length). The build
  reports 125 beats, every one inside its clip.
- **The promise:** English is 4.3 min, so the page, the console README and both READMEs say four
  minutes; the READMEs' embedded recording is v1 and is labelled as such (a new recording needs an
  upload to GitHub by the owner).
- **Guards:** `tests/test_intro_narration.py` (new) re-derives every spoken and displayed number
  from the state machine, the deployer, the configs and the evidence files, checks the same numbers
  in all five languages, and checks that no language frames the agents as replacing people; five
  negative controls, each caught.

## Not yet verified live

- The Introduction played in the deployed console (owner), after the merge and a deploy from main.
