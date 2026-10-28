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

## Verified live (2026-10-27)

- **Deployed from main** (`f26bc9b`, PR #186's merge) with `deploy/console/deploy.sh`; the function's
  code was updated at 07:23:53 UTC. The deployed package, downloaded back, is the build from main:
  `intro.html` sha256 `ca72ca0e…` on both sides, `frontend.html` `aced8e85…` on both sides, and the
  35 clips byte-identical to the repo's, each compared by its language folder.
- **Played by the owner, all five languages.** Mandarin and Cantonese: pictures and narration match.
  **English: they do not**, and the owner asked for English to follow the Mandarin and Cantonese
  versions.

## English timing (the fix, `intent/intro-en-timing`)

- **Cause, measured.** The beats sat at each sentence's character offset x the clip length, which
  assumes an even speaking rate. The other four languages scale the same times by their clip length
  and were judged right. The English voice (generative) pauses at lists and slows on numbers.
  Against the clause starts measured from the English clips, the old times were early by up to
  3.2 s in scene 6 ("judge" 9.5 s against 12.70 s spoken, "deploy" 10.3 s against 13.33 s, "$0.45"
  13 s against 15.72 s) and by 1-2.2 s through scene 5.
- **Measured how.** `measure_clauses.py` takes ffmpeg's silencedetect pauses (-40 dB, 80 ms) and
  assigns one to each clause by dynamic programming on clause length. Taking the pause nearest each
  clause's estimate instead moved scene 6 by one clause from "with thirty pieces" on, because the
  voice pauses inside "Silver and Gold". 133 clauses across 7 scenes.
- **Fixed how.** 108 beats name their clause (`data-at`); the build writes the measured start as
  `data-en`, which only English reads. Mandarin, Cantonese, Japanese and Korean are byte-for-byte
  the same schedule as before (`data-t` untouched).
- **A test that was not running.** `tests/test_intro_player.js` was outside pytest and CI and failed
  5 of 72 assertions on main (v1 durations: 5:04, 43.61 s, 64.18 s). It now derives them from
  `durations.json`, and `tests/test_intro_timing.py` runs it. Against main's page its new check
  fails ("English scene 6 at 12 s ... is not the data-t schedule: 19 vs 19"); against this branch,
  76 of 76 pass.

## The English the owner heard was v1 (`intent/intro-audio-cache`)

- **Reported** after the timing fix was deployed (2026-10-27 08:27 UTC): the English script is not
  updated; use the updated script and cross-check with Mandarin and Cantonese.
- **What was deployed was v2.** The live clips are byte-identical to the repo's (35/35). Every clip's
  synthesis stamp (sha256 of text, voice and engine) matches the current text: 7/7 in each of en,
  zh, yue, ja, ko. All 7 English clips differ from v1's. v2's English scene 1 opens on the model
  that passed every gate (33.2 s); v1's opened on "glue work" (42.5 s).
- **Cause.** The page requested `/intro/audio/<lang>/<scene>.mp3`, one URL per scene in every
  version, and the route sends `cache-control: public, max-age=86400`. A browser keys that cache on
  the URL, so one that had played v1 within the day kept playing v1. The route's comment said the
  cache was keyed on the deployment.
- **Fix.** The page requests `<scene>.mp3?v=<first 12 hex of the clip's sha256>`; the route reads
  the path only and is unchanged. Against the deployed page (no version), the player test fails 3
  assertions naming the unversioned URLs; against this branch it passes.

## The English caption was a slice (`intent/intro-captions`)

- **Reported** with screenshots of scene 1 at 0:06 (2026-10-27): Mandarin and Cantonese captioned
  the scene from its first sentence; English captioned "… ned on a store's customer conversations,
  it answered seventy-two point five percent … and ev …". Read as an English script not updated.
- **What was live was v2** (#188 deployed at 08:48 UTC: the package and the page served over HTTP
  both equal the build from main, sha256 `4761bb8a…`; 35/35 clips identical). The caption was the
  defect: a 150-character window centred on the proportional position. English scene 1 is 680
  characters and Mandarin 226, so the same window was a fifth of one and two thirds of the other.
- **Fix.** Three pages per scene that break between sentences. Every page in all five languages was
  printed and compared: English and Cantonese break at Mandarin's sentence in all 7 scenes (21
  pages); position alone put three Japanese/Korean breaks one sentence off (ja s3, ko s5, ja s6),
  which `caption_breaks` now names. English turns pages on the measured clause.
- **Checked.** The player test reproduces the screenshot against the deployed page ("… ed on a
  store's customer conversations") and passes on this branch in all five languages at 0:06.

## Captions confirmed by the owner (2026-10-28)

- After #189 was deployed (09:24 UTC; the package, the page served over HTTP and the build from main
  all `7002390f…`; 35/35 clips identical), the owner reloaded and compared: **"英文和中文一致"**,
  English and Chinese captions agree. A two-pages-per-scene variant was tried in a working copy while
  the owner was still looking at a page cached from before the deploy; it was withdrawn uncommitted
  when the deployed three-page version was confirmed.
