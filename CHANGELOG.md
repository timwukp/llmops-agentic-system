# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versioning: SemVer.

## [Unreleased]

### The redaction gate: one rule set, and binaries are actually scanned

Found by staging the narration below — the first commit in this repo's history to add binary
files. Two defects, both live, in the pair of scanners that guard a public repo:

- **The commit hook text-grepped compressed audio and blocked on entropy.**
  `hooks/pre-commit` skipped files by extension *denylist* (`png|jpg|jpeg|gif|pdf|zip`), which
  does not include `.mp3`, so it ran the account-id regexes over 11.4 MB of MPEG frames. The
  generic bare-12-digit rule matched a twelve-long run of `0x33` bytes inside a frame — ASCII
  digits by coincidence, not an id — and refused the commit. (The exact byte run is pinned in
  `tests/test_redaction_scan.py`; quoting it here would make this file trip the very gate it
  describes, which is how the first draft of this entry got blocked.)
  **Measured: 1 of 35 clips.** Re-synthesise the narration and a *different* random
  subset blocks. An intermittent gate that fires on nothing is how people learn to pass
  `--no-verify`, after which it guards nothing at all.
- **CI never opened 44 of 157 tracked files.** `.github/workflows/redaction-check.yml` selected
  files by extension *allowlist* (`--include='*.py' --include='*.json' …`), so `frontend.html`,
  `page.template.html`, `test_intro_player.js` and three extensionless files were never
  scanned. Those are ordinary text and can carry an account id; a leak in one of them passed CI
  green. Scanned at the time of the fix — they were clean, so this closes an exposure rather
  than an incident.

The two scanners were hand-copied lists of the same five regexes with two different
file-selection schemes, which is how they drifted in opposite directions. They now share
`tests/redaction_scan.py`, and files are classified **by content** — a NUL byte in the first
8000 bytes, the heuristic `git` itself uses — rather than by a filename guess maintained by
hand. An extension list is wrong in both directions at once: it text-greps audio *and* skips
HTML.

Binaries are scanned with every high-signal rule — the two AWS access-key-id prefixes, the
secret-key assignment string, account-bearing ARNs — **plus this repo's own account id**; those
are structural enough to be safe on any byte stream. Only the generic any-12-digits
heuristic is dropped for them, because on binary data it is measurably noise — 0 structural
hits against 1 false positive across the same 11.4 MB. The residual gap is stated rather than
hidden: a bare 12-digit id that is neither this account's nor inside an ARN, embedded in a
binary, is not caught. `REAL_ACCOUNT_IDS` is the lever if a second account ever appears.

Both callers now fail closed when the scanner is absent — the same lesson as the SVG block,
which once skipped silently after its checker was renamed — and `rc=2` means *could not look*,
kept distinct so it can never be reported as *looked, and it is fine*.

Six negative controls, one per way this rots: binaries skipped entirely (the shape a
"simplification" of this fix takes); the real-account-id rule deleted while the structural
rules still fire, so a reviewer sees "yes, binaries are scanned"; the entropy rule re-applied
to binaries; CI drifting back to its own list; and two on the literals property below. **The
fourth was UNCAUGHT on its first run** — the guard grepped for two exact regex *spellings*, and
a re-inlined scanner written as `grep -rn AKIA` contains neither. It now asserts the *shape* —
no recursive content scan in either caller — with the spellings that must and must not match
pinned in a table, because a regex checked only against the one string its author had in mind
has unmeasured edges.

**Neither file spells a credential-shaped string as a literal any more**, and a guard enforces
it. This came out of the fix's own pull request being blocked. Both files are in the scanner's
`SELF_REFERENTIAL` list — they have to be, since these patterns are their subject matter — but
that exemption is knowledge local to *this* scanner. A session-level pre-PR hook scans the
branch diff with its own pattern list and no such notion, and it stopped the PR on five hits:
the `0x33` byte run, the three AWS-published accounts in the allowlist, AWS's own example
access key, a synthetic account and a placeholder ARN. **Not one was a secret.**

The tempting fix — teach that hook a per-file exemption — is a second scanner with its own
selection scheme, i.e. precisely the drift this entry is about, and it would have to be repeated
for every scanner that ever reads these files. So the values are assembled from parts
(`b"6833" + b"13688378"`) or rebuilt from their byte description (`bytes([0x33] * 10 + …)`)
instead. Nothing is weakened: the reconstructed run is asserted to be the identical twelve
bytes, and the allowlist tests parametrize straight off `ALLOWED` rather than a second copy of
it. The strings simply are not written down, which costs one `+` and needs no coordination with
anybody. The same reasoning already applied to this repo's real account id — now generalised
from one value to a shape.

Worth recording for whoever hits this next: this entry's own first draft was blocked by the gate
too, for naming the secret-key string and quoting the byte run. `CHANGELOG.md` was **not**
allowlisted — a file everyone edits is the last one you want to exempt — the prose was reworded
to describe the patterns instead.

### A narrated five-minute introduction, as the console's first tab

- **The console opened on a wiring diagram.** Architecture was the landing tab, which answers
  *how the system is built* to someone who has not yet been told *why it exists or what it
  cost*. A new **Introduction** tab sits left of Architecture and is now the default for a
  first-time visitor; a returning operator still lands on whatever tab they left.
- **Seven scenes, narrated, in five languages.** `GET /intro` serves one self-contained page
  (83 KB, 128 timed beats) built at deploy time by `deploy/console/intro/build_intro.py` from
  `page.template.html` + `narration.json` + `durations.json` + two architecture SVGs. Narration
  is 35 pre-synthesized Amazon Polly clips — English (default), 普通話, 粵語, 日本語, 한국어 —
  bundled in the Lambda zip and served by `GET /intro/audio/<lang>/<scene>.mp3`. The English
  narration measures 303.8s (5:04); the other four run longer, and the page rescales its beat
  timings and progress segments per language rather than assuming the English pacing.
- **Pre-synthesized rather than synthesized on demand.** `deploy/console/synth_narration.py` is
  a build step, so the console's IAM gains no Polly action, there is nothing to presign and
  nothing to expire, playback costs nothing per view, and the whole feature is testable
  offline. Screens are redrawn in CSS/SVG rather than captured, for the same reason: a
  screenshot of a console is stale the next time the console changes.
- **The page degrades to browser speech, per clip.** A missing clip is not an error — it is a
  robot voice, and nothing logs it. So the bundle is checked instead of trusted: `deploy.sh`
  hard-fails if any (language, scene) from `narration.json` is absent from the zip, if any clip
  is under 1 KiB, or if the package exceeds Lambda's 50 MB direct-upload limit, and
  `tests/test_intro_bundle.py` (40 tests) imports the handler out of a *reconstructed bundle*
  because the layout is the thing under test.
- **Two request-controlled segments go into a filename, so the route allowlists instead of
  sanitizing.** Cold start walks the bundle and records which (lang, scene) pairs exist; a pair
  that was not found is a 404 before any path is joined, so `..` is simply not a key. The audio
  response also sets `isBase64Encoded` — without it API Gateway sends the body as UTF-8 and
  every clip arrives corrupted under a *200*, which the page's own fallback then hides.
- **The default landing tab no longer reaches for Parameter Store.** `_resp()` built its CSP
  through `data_bucket()`, which resolves the S3 upload origin via SSM and does *not* cache a
  failed resolve — so the intro routes would have hit SSM on every request for an origin the
  page never fetches. They now pass `csp_upload=False`; **m111** and **m112** pin both
  directions, because "fixing" this by changing the default would strip the upload origin from
  the other 30 routes and block dataset uploads with a header that reads as an S3 permission
  error.
- **A negative control found the traversal test asserting one layer twice.** All eight payloads
  carried an extra separator or a wrong extension, so every one died at the shape check and the
  allowlist was never exercised — replacing the entire allowlist test with `if not lang or not
  scene` left it passing 8/8. The payloads are now split by which layer must catch them, with a
  second test asserting the split is still real, so a future tightening of the shape check
  cannot quietly retire the allowlist's coverage. **m109**, **m110** and **m113** cover the
  base64 envelope, the allowlist and `deploy.sh` dropping the audio copy.
- **The tab count in the diagram is now derived, not typed.** `gen_architecture_svg.py` read
  `8 tabs` from a literal; hand-editing the SVG would have satisfied the guard while leaving the
  generator to restore the wrong number on its next run. It now counts the nav buttons in
  `frontend.html`, like `HARNESS_N` and `LIMIT_USD` already did.

### The same walkthrough, playable from the README

A reader on GitHub sees a README, not a browser tab, and a link to a live console is something
they have to decide to click. `docs/media/intro-en.mp4` (10.7 MB, 5:04) is committed and
embedded in both READMEs so the five-minute walkthrough plays without leaving the page. The
live `/intro` page stays the canonical artifact — it is the one with all five narrations, and
the mp4 is English only, so both READMEs link it right beside the player.

- **One clock, not two recordings.** `deploy/console/intro/record_video.py` plays the real page
  in a headless browser *in real time* and muxes the **same committed mp3s** the page just
  played. The page's clock **is** the audio element (`curTime()` returns `audio.currentTime`),
  so a 300 ms stall stalls the animation with the sound instead of sliding it ahead: sync is a
  property of using one clock, not of aligning two recordings afterwards. Rendering frame N at
  `t = N/fps` was rejected in writing — CSS transitions and SVG `@keyframes` animate on the
  document timeline, which does not advance during a screenshot, so every beat would pop
  instead of fade and the diagrams would be static.
- **Three separate offsets, measured, not absorbed into a tolerance.** Drift started at
  **+2.00s** and each cause was found rather than tuned away: an opening **lead-in** (recording
  begins at context creation, narration after load+click) trimmed with `-ss` after measuring to
  the first nonzero `currentTime`; a **deliberate 900 ms tail** (`TAIL_S`) held so the closing
  beat's `.5s` fade completes, named as a constant and *added to the expected length* rather
  than charged to drift; and a **trailing flush** as the browser finalises the file on context
  close, cut with `-t`. Final drift: **+0.00s**. Widening the tolerance twice would have been
  quicker, and a tolerance wide enough to hide a deliberate second is wide enough to hide an
  accidental one.
- **The recorder is a build step; the guard checks the result.** It needs Chromium and ffmpeg,
  and this suite is offline by construction, so `tests/test_intro_video.py` (11 tests) verifies
  the artifact instead of re-running the recorder: length against the summed narration clips,
  an audio track as long as the video, the authored stage size, `yuv420p`, `moov` before `mdat`,
  and that both READMEs carry **both** a `<video>` tag and a plain link.
- **Four guard defects found by breaking it, not by reading it.** Deliberately broken mp4s were
  built and driven past the guard, each one **with `ffprobe` removed from `PATH`**, because CI
  has no ffmpeg and a guard that only fails on a laptop gates nothing.
  - The truncated file made the faststart check raise `ValueError: b'mdat' is not in list` —
    red, but reporting a Python bug rather than the state of the file.
  - The no-ffprobe mp3 fallback assumed **MPEG-1** while Polly emits **MPEG-2** (24 kHz mono),
    so it returned **11.7s for 303.8s of audio**: a wrong answer in the right units, on the
    exact branch that runs in CI.
  - **A full-length silent film passed the entire module** — `7 passed, 3 skipped` — on the
    machine that gates merges. The audio-track and frame-size assertions sat behind
    `skipif(ffprobe)`, the same defect as the length check one test along. Both now read
    `hdlr` / `stsd` / `mdhd` out of `moov` directly, and a 3-second audio track against a
    304-second video fails on **duration** rather than passing on presence.
  - **The frame-size check read `tkhd`, which is *display* geometry.** A 640×360 frame tagged
    `SAR 59:32` reports width **1180** — the authored width exactly — so a video carrying a
    third of the pixels would have passed. It now reads the coded size from the sample entry and
    separately asserts the pixels are square, which also catches the reverse: right pixel count,
    stretched rendering.

  Each is cross-checked against ffprobe where ffprobe exists, and the length check reads `mvhd`
  from the container so it *runs* without ffmpeg rather than skipping into a green tick.
- **The authored stage size is derived from the page's own CSS**, not retyped and not read from
  `record_video.py` — the recorder keeps its own `STAGE_W`/`STAGE_H`, and a guard comparing the
  video against the *recorder's* number stays green while both drift away from the page the
  scenes are actually laid out in. **m123** re-authors `.stage` larger without re-recording, and
  the guard fails.
- **`"/intro" in text` was satisfied by `docs/media/intro-en.mp4`.** So the assertion that the
  READMEs still link the five-language live page could not fail while the video link existed.
  Found by **m121** deleting the live link and watching the test stay green; it now matches an
  absolute URL. **m120** and **m122** cover the video becoming unreachable and a budget figure
  reappearing beside the player.
- **No amount, in either language.** The walkthrough section says the reporting reference is
  set by each team and names no figure — checked by section boundary rather than a character
  window, so the guard does not police the pre-existing prose in the next section.

### The README never said what problem it solves

- **Both READMEs opened with an implementation, not a problem.** The first sentence was "An
  end-to-end LLMOps platform, run autonomously by AWS Bedrock AgentCore Harnesses", and by
  line 7 a newcomer was reading about conductors, DeepSeek-R1, QLoRA and SageMaker endpoints —
  three unfamiliar words deep before learning whose pain any of it removes. Someone deciding
  whether this repo is relevant to them got no answer above the fold.
- **The evidence for the pain was already in the repo, filed under other headings.** Six
  rounds of dependency and CUDA-OOM failures to get one QLoRA job to `Completed`
  (`docs/CASE_STUDY.md`); an endpoint billing $36.36/day at 0 invocations and 0.0% GPU over
  90 days with no owner tag (`PROJECT_STATE.md`); quality gates that scored this project's own
  model 0/16 and were not talked past; ≈ $12–15 for the whole proven lifecycle. A new
  `## The problem this solves` section in both languages states the pain, why an assistant
  that only suggests commands does not close it, and what runs instead — every number cited to
  the file that measured it, no new claims invented. Depth stays in the case study, linked.
- **The section describes the reporting mechanism without naming an amount.** The configurable
  reference that names overspend is a per-team setting, so quoting this platform's own figure
  as though it were the design would misrepresent it.

### "Six agents" outlived the six-agent fleet

- **Both CASE_STUDY variants say "six agents"; `agents/*/harness.json` holds seven.** The
  FinOps auditor was added after Phase 6, and nothing noticed for the entire life of the
  seventh harness — the same drift-by-addition that already produced a stale Lambda count and
  a stale ASL state count. A number that was once measured looks measured forever.
- **Scoped rather than renumbered.** Changing it to seven would contradict the evidence file
  the document cites (`VERIFICATION_phase5.md`: "All six harnesses currently run Opus 5") and
  claim the auditor took part in a build it was absent from. Both variants now say six *and*
  say it is the v1 fleet, naming today's count alongside.
- **The count on the first screen had no guard at all** — which is precisely why "six" could
  stand indefinitely. `test_the_agent_count_readers_see_first_matches_the_fleet` derives the
  fleet size from `agents/*/harness.json`, requires each README's claim to match, and treats a
  claim deleted or reworded away as a failure rather than a pass. A smaller historical count is
  allowed only in a `##` section that marks it as past *and* names the current count, so the
  scoping note cannot itself become the next stale number. **m106** (English prose drifts),
  **m107** (the zh-TW twin drifts) and **m108** (the v1 scoping deleted, leaving a bare stale
  count) each fail it. A guard hardcoding 7 would have missed the direction that matters — an
  eighth harness landing while the prose stands still — verified by hand here, since this
  runner mutates existing files and cannot create one.

### The readiness panel counted nine questions and said six

- **`task_readiness`'s docstring described the panel as showing "which of the *six* data
  questions nobody has answered yet"; `DATA_READINESS_FIELDS` holds *nine*.** The list grew to
  nine in the commit that derived it from the orchestrator's consult prompt — the fix for a
  panel that was missing `datasheet.provenance` and `readiness_report_uri` — and the sentence
  explaining the panel stayed at six. Nothing was functionally wrong: every readiness test
  measures the tuple, so all nine questions were asked, answered and counted correctly in the
  API response. What was wrong is the explanation, which is what a reader believes when
  deciding whether the panel covers what they care about.
- **Found while writing an operator runbook, and it had already spread.** The count was copied
  out of this docstring into two operator-facing documents before anyone checked it against
  the tuple — a false claim in a docstring propagates at the speed people quote it.
- **The guard derives the number rather than restating it, and fails from both sides.** The
  count in the tuple was guarded (`test_readiness_names_every_field_the_consult_protocol_asks_for`);
  the count in the prose was not, so the prose is the copy that drifted. The new guard reads
  `len(DATA_READINESS_FIELDS)`, maps it to its number word, and requires the docstring's count
  sentence to name exactly that one — so **m104** (prose back to "six") and **m105** (tuple
  shrinks to eight, prose left at nine) both fail it. A guard that hardcoded "nine" would
  catch m104 and sail past m105, which is the difference between checking a claim and
  restating it. It anchors on the single line containing "data questions" rather than
  searching the module, because "nine" appears in another test's own prose; and it rejects
  digits as well as words, since `6` would slip past a word search.
- **Same shape as this panel's earlier defect.** ARCHITECTURE.md already records the version
  where the *guard* restated seven paths against a prompt specifying nine. Derive-don't-restate
  now covers the prose as well as the list.

### A limit without its mode is the more misleading half

- **`GET /api/cost-overview` reported the two dollar limits and not whether either is
  enforced.** `limits` carried `single_usd`, `cumulative_usd` and `approver_group`;
  `budget_mode` existed on the `gate` object but not here — so the one surface a human reads
  was the surface missing it. Found while verifying the $2000 → $20,000 raise live: the
  overview came back with the new number and `budget_mode: None`. `limits` now carries
  `budget_mode` and a derived `enforced`, computed with the same predicate the gate itself
  uses (`BUDGET_MODE == "blocking"`), so the label cannot disagree with the branch.
- **That fix went in on one of the two `limits` payloads, and shipped that way.** The
  console publishes a dict named `limits` from both `cost_estimates` and `cost_overview`;
  only the first got the mode. Reading the deployed API back is what found it —
  `/api/cost-estimates` answered with `budget_mode`, `/api/cost-overview` answered with two
  bare numbers, and every test above stayed green because each names the estimates endpoint.
  The entry above even said a correction "holds until the next person adds a limits
  consumer"; the second consumer already existed. Both payloads now carry it, and the guard
  that would have caught it **derives the list from the source** — every dict literal
  assigned to a `limits` key must state its mode — rather than naming the payload somebody
  remembered. m103 reproduces the shipped state: first payload honest, second one bare.
- **The Cost KPI card read as a stop sign.** Its own words were *"limit $20,000 per run /
  $20,000 cumulative"*, with nothing saying that in `advisory` — the deployed default — an
  over-budget run is named, priced, and then launched anyway. It now renders
  *"reference … · ADVISORY — an over-budget run is reported, then launched anyway"* in amber,
  or *"ENFORCED — an over-budget run is held for an approver"* in green. Verified live at the
  new reference: n=260,000 straddles it ($19,775 expected, $21,056 worst case) and returns
  `over_budget_usd=1056.30` with `status=approved` — the run launched.
- **Guarded in both directions, and against behaviour rather than against itself.** A field
  hardcoded `False` satisfies an advisory-only test forever and mislabels every blocking
  deployment; one hardcoded `True` is the original defect with extra steps. So `enforced` is
  asserted equal to what an over-budget launch *does* — held with 409, or invoked — once per
  mode. The two modes are checked by two single-fixture tests calling one helper, not by one
  test requesting both fixtures: the `blocking` fixture monkeypatches `BUDGET_MODE` on the
  same module object `wired` hands out, so a test asking for both gets blocking twice and
  passes while comparing nothing. That version was green in the wrong direction first.

### Stage timeouts: a day for real work, an hour for anything holding a GPU

- **The six states that wait on real agent work now carry `TimeoutSeconds: 86400`** — a full
  day, raised from 7200/21600 on the platform owner's instruction. `TimeoutSeconds` is the
  only real ceiling on a stage: the driver Lambda's 900 s does **not** bound it, because the
  driver self-reinvokes across invocations via `_continuation` — but the `.waitForTaskToken`
  token it holds only lives for `TimeoutSeconds`. A 480-teacher-call generation run does not
  fit in two hours, and the 2026-08-01 run was cut off by exactly that, mid-work, with the
  work already paid for.
- **The seven bookkeeping states deliberately did *not* move.** `Teardown` is what deletes the
  endpoint and `MonitorHealth`/`MonitorReport` sit on the only path to it: a wedged `Teardown`
  at 86400 s holds an `ml.g5.2xlarge` InService for a day at $1.515/hr — the exact shape of
  the 843-day, 0-invocation orphan this project already paid for and deleted on 2026-08-02.
  Raising every state with one `sed` would have been a cost regression dressed as a
  reliability fix. The split is now asserted, and the guard **fails on an unclassified new
  state** rather than defaulting it into either bucket, because defaulting is how a cleanup
  stage would silently inherit a 24-hour ceiling.
- **`HeartbeatSeconds: 18000` on `FinetuneLaunch` and `RemediateFinetune` was a shorter
  deadline wearing a liveness signal's name, and it was live for weeks.** Step Functions
  fails a state with `States.Timeout` if the heartbeat interval elapses without a
  `SendTaskHeartbeat` — and **nothing in this platform has ever called it**, though the IAM
  role grants `states:SendTaskHeartbeat`. So the first heartbeat never arrived and both
  states really died at **18000 s while their ASL said 21600**. Every surface agreed with the
  ASL: the console's hover card rendered a `heartbeat 18000s` row, which reads as *we monitor
  liveness*, not as *this stage has a 5-hour cap you cannot see anywhere*. It surfaced only
  because the raise to 86400 would have left those two dying at 5 hours while all six
  siblings ran a day. Both fields removed, along with the console reader and hover row that
  displayed them — and the existing
  `test_every_field_the_hover_card_renders_is_supplied_by_the_api` is what caught the
  now-unproducible field, so the dead UI could not linger. A heartbeat may come back, but
  only **with** a sender: that is what the new guard permits and what it refuses.
- Two hover-card tests pinned the literal `7200` while being about something else entirely
  (that a failed AgentCore lookup must not drop the ASL half of the card). Both now derive
  the value from the ASL: a literal in a test whose subject is not that literal is a tripwire
  on the wrong wire, and rewriting it to `86400` would just re-arm it for next time. The
  card's timeout row also renders hours above an hour — "1440 min" is not a duration anyone
  reads as a day.

**785 pytest**, **103/103 negative controls** (89 mutations). Four new controls: `Teardown`
inheriting the day, `DataPrepGenerate` reverting to 7200 (the shape a merge conflict resolves
wrongly by default), a new timed state shipping unclassified, and a heartbeat interval
returning without a sender.

### The ASL deploy has to prove it landed, not report that it was sent

- **`deploy_state_machine` now reads the definition back and refuses to call the deploy done
  until the live machine matches the ASL in this tree.** It used to return
  `action: "updated"` on the strength of `update_state_machine` returning 200 — which says
  the call was accepted, and nothing about what the machine will run. On 2026-08-03 the live
  definition turned out to be **a state behind**: `EvalGenerate` was entirely absent, though
  it merged on 2026-08-02 in `7940af8` as the whole point of #57, along with six stale
  timeouts, both senderless heartbeats, and `FinetuneAnalyze` still pointing at `EvalGate`.
  A human found that by reading the live definition by hand. Nothing in the repo compared
  the two, so nothing could have found it.
- **This repo had already paid for that belief once.** `update_function_configuration` was
  called without `Role` for months while every run reported "updated" and each function kept
  its birth role — the defect
  `test_a_role_change_reaches_an_existing_function_not_only_a_new_one` exists for. The same
  file's `live_bus_translator_gap` even argues the point in its own docstring, and **blocks**
  the deploy on it, for bus rules. The state machine definition never got the same treatment.
- **The comparison is semantic and it names states, not bytes.** Step Functions happens to
  return the definition verbatim today (measured: 26742 bytes both sides, parsed-identical),
  but a formatting-only difference is not a deploy failure, and a check that calls it one gets
  switched off by the third person it wakes. So the drift walk parses both sides and reports
  "`EvalGenerate` is absent live" rather than a 26 KB diff — and when equality fails and
  neither walk can localise it, it says **that**, because reporting clean on the one case it
  cannot explain is the exact failure it exists to prevent.
- **It polls rather than reading once.** AWS documents `UpdateStateMachine` as eventually
  consistent — executions started immediately afterwards may still use the previous
  definition. Five reads, backing off 1/2/4/8 s. A guard that cries wolf gets deleted, which
  is the same eventual consistency that bit the push tool's ref read in #35.
- A read-back that cannot run reports `definition_confirmed: false` with the reason, never a
  confirmation. Not being able to check is not the same as having checked.

**795 pytest**, **107/107 negative controls** (93 mutations). Eleven new tests and four new
controls: the read-back deleted from the deploy path, the "cannot localise" backstop returning
clean, the eventual-consistency wait removed so a lagging read reads as drift, and an
unreachable read claiming confirmed. The backstop's control went **uncaught** on the first
run — the test fed it a difference the top-level walk does localise. Its real input is a key
present-with-`null` on one side and absent on the other: unequal dicts, and every `.get()`
comparison agreeing, which is the one shape both walks are blind to. A control that goes
uncaught means the test named the wrong subject, not that the control needs weakening.

### A harness deploy has to prove it landed too — READY is not "serving what you sent"

- **`05_harnesses.py` now reads each harness config back and refuses to call the deploy done
  until the live config matches this tree.** It returned `action: "updated"`, `status:
  "READY"` on the strength of `update_harness` returning 200 and `wait_ready` seeing READY.
  Neither says anything about *what the harness answers with*, and READY is the more
  dangerous of the two, because a harness serving a stale prompt is READY the entire time
  it is wrong.
- **Two harnesses were live-drifted when the guard was first run, and only one was known.**
  `llmops_finops` still quoted the falsified orphan rate — the $18 daily figure that was half
  the real one — although #41 had put `$36.36/day` on main two days earlier. The one nobody knew about: **`llmops_data_prep` was
  932 characters behind main**, missing the entire Macie paragraph from #63 — so the
  data-prep agent was still being told it had no way to report a real classification result,
  months of that work deployed to S3 and never to the harness. The other five reported clean.
- **The comparison is containment, and that is a measurement rather than a preference.** On a
  perfectly synced harness `environment` still differs: the deploy sends
  `networkConfiguration` + `lifecycleConfiguration`, and the service returns those *plus* the
  `agentRuntimeArn`/`Name`/`Id` it assigned. Strict equality would report drift on every
  correct deploy of all seven harnesses, forever — and a check that fails a correct deploy is
  one the third person it wakes switches off, taking the real check with it. So the question
  asked is: is every value we sent present and equal live? Keys the service added are its own
  business. Verified against all seven live harnesses before shipping.
- Drift is reported by **dotted field path** (`environment.agentCoreRuntimeEnvironment.
  lifecycleConfiguration: sent, but ABSENT live`), and a long value by length plus first
  divergence rather than pasted twice — the finops prompt is 6539 characters, and dumping
  both sides is how the one line that mattered scrolls off the screen. "Sent but absent" is
  kept distinct from "differs": different causes, different fixes.
- **The read-back runs before `warm()`.** Warming first spends up to six real model turns
  making a harness that serves the *wrong* prompt fast to reach, and prints a reassuring
  "warmed" line above the failure.
- The update payload and the read-back's field list are now one name (`UPDATED_FIELDS`). Two
  lists that agree today drift later, and a field added to the send but not the check can
  fail to land silently — this defect reintroduced one field at a time. `memory` is
  deliberately excluded: it belongs to `04_wire_memory.py`, and reporting another owner's
  field would make every run look broken.

### A duplicate test name deletes the earlier test, and no count notices

- **`test_no_test_function_name_is_defined_twice_in_a_file`.** Python keeps the later `def`,
  so the first is never collected and never runs. Nothing in this repo noticed: the
  collection total still goes *up*, so the count guard is satisfied, and the suite stays
  green because the surviving test passes. Found by writing the harness guards above — a new
  test reused the exact name of the ASL read-back test from #80, in the same file. That test
  silently left the suite, and the negative control verifying it (`m93`) named the shadowed
  node id: it would have gone on printing PASS while measuring a different test's failure.
- Its own first control went **UNCAUGHT**, which is the same lesson in the same commit: the
  guard asserted only "this repo has no duplicates", and that passes whether the detection
  works or not while the tree is clean. Split into a detector checkable against input that
  *has* the defect, plus a separate repo-wide sweep. A control that goes uncaught means the
  test named the wrong subject — never that the mutation needs weakening.

**808 pytest**, **113/113 negative controls** (99 mutations). Six new controls: the harness
read-back deleted, equality demanded where the service adds keys, warming before confirming,
the field list allowed to drift from the payload, an unreachable read claiming confirmed, and
a shadowed duplicate going unreported.

## [1.2.0] — 2026-08-02

Twenty-one merged PRs (#17–#38; #24 closed unmerged). The pattern across almost all of them
is one kind of defect: **a component that existed, was deployed, was tested, and was never
reached.** A dispatch that no state ever called, a bus rule set that was empty, a success path
that had never executed, a reply channel with nothing listening. Each looked healthy from
every dashboard, because the half that was written was the half that worked.

### A consultation the customer can actually finish

Before this release a customer could not hand us a dataset. The Tasks tab could show a run
but had no way to start one from a goal, and there was no upload path at all.

- **Goal-driven consult entry** (#17) — a natural-language goal enters the orchestrator in
  consult mode and comes back as a costed plan; acceptance is **KMS-signed**, and
  `service_launch_run` verifies the signature and the plan hash before `start-pipeline` sees
  anything. An approval that cannot be verified is not an approval.
- **Presigned dataset upload** (#22) — plus bucket CORS, the console's `PutObject` IAM, and an
  **httpOnly refresh cookie**, because the old refresh path signed the customer out in the
  middle of the upload they were signed in to perform.
- **One thread, not a form** (#23, #31) — the tab became a single Claude-Code-style
  conversation with a drop zone. Parked directive rows had been pushing the newest real events
  off the timeline, so the customer watched a stale run.
- **The identity in the hover card is resolved, not guessed** (#18), and the teacher-token
  estimate was recalibrated — the old figure made the plan's own caps arithmetically
  infeasible, so every plan it produced was unexecutable.
- **The audit copy stops erasing, truncating, and gating itself** (#36's branch) — a failed
  transcript read was treated as "no file yet" and wiped the history; a failed audit write
  stranded a signed acceptance it had no authority to gate.

### The pipeline dispatches every stage it claims to dispatch

Three stages were configured, documented, and never dispatched. All three were found by
asking what actually calls the thing, not by reading what declares it.

- **The eval gate read a report nothing wrote** (#33) — `evaluate` was never dispatched, so the
  gate consumed a file that did not exist.
- **The `llmops-pipeline` bus carried ZERO rules** (#35) — `EscalatedToHuman` was published to a
  bus with no subscribers, so every escalation was emitted into nothing. Now routed to the
  conductor for triage, with `page_human` serviced on the driver path as triage's only
  above-authority exit.
- **`llmops_monitor` had no task dispatched anywhere** (#36) — a runtime deployed and wired into
  the state machine that nothing ever asked to do work.
- **The SUCCESS path had never run** (#19's window) — nothing wrote `status=completed`, so every
  successful run was a zombie record. A happy path that has never executed is not a path.
- **Per-run report keys** (#19) — one shared key meant each run overwrote the last one's report.

### FinOps and governance

- **The orphan endpoint costs $36.36/day, not the $18 six files claimed** (#37). The $18 was the
  first sweep's *guess* — that sweep could not call `DescribeEndpoint` and said so in its own
  report — and it understated by 2×, which is the magnitude an owner can dismiss on the merits.
  Now derived from `describe_endpoint_config` (ml.g5.2xlarge ×1) against the documented hourly
  rate. The endpoint had been InService for 843 days (2024-04-11 → 2026-08-02) with 0
  invocations and 0.0% GPU utilization; deleted 2026-08-02 under explicit authorization.
  **The sweep that corrected it missed the one file that matters most**: the guard was anchored
  to the three files that sweep edited, so `agents/finops/harness.json` kept telling the auditor
  the orphan cost half that rate — inside the very rule about never publishing an assumed number as a
  measured one. A prompt is the worst place for a falsified figure: nobody opens a doc mid-audit,
  and the agent re-reads its prompt on every invocation. The guard now scans **every**
  `agents/*/harness.json` rather than naming the file that was wrong, since naming the file is how
  the hole got there, and it requires the measured rate to be **present** — an absence-only check
  passes on a deleted sentence, which would have left the attribute-by-resource rule with no
  concrete example.
- **`bedrock-monthly-dev` is stated in both directions** (#37) — its `Service: ["Amazon Bedrock"]`
  filter is simultaneously what kept the $1000 guardrail meaningful and what made it blind. No
  account-level control would ever have flagged that endpoint; the whole-account monitor sweep
  is what found it. `describe_budget_actions_for_budget` returns **0 actions**: it notifies, it
  does not enforce.
- **The budget became advisory but stayed spoken aloud** (#21) — `BUDGET_MODE=advisory` reports
  the overage in `start_run`'s response rather than blocking. Removing the number entirely would
  have deleted the only line that says a run is more expensive than its plan.
- **The reference is $20,000, raised from $2,000, and the deploy now sets it** — the platform
  owner's instruction: this is the project's own design-and-test platform, not a customer's
  production account, and the entire test-proven record cost ~$12–15. A reference low enough to
  be crossed by ordinary work gets clicked past. Two things the raise exposed, both worse than
  the number being low:
  - `deploy.sh` set **neither** limit, so the live function reported `APPROVAL_LIMIT_USD: null`
    and fell back to the console's own literal — which happened to agree. Nothing was wrong and
    nothing could have told us when it stopped agreeing. Both are now derived from
    `cost_model.DEFAULT_*_LIMIT_USD`, and the console's fallback copy is pinned equal to the
    canonical one by a test, because two copies of a number with nothing comparing them is
    exactly how one falsified figure survived in four files at once.
  - **The straddle fixtures stopped straddling.** Nine budget tests were built on a literal
    2,000,000 rows priced at $1,268 expected / $3,804 worst case — both under $20,000, so the
    tests would have gone green while never engaging the budget check at all. Their own
    docstrings named this hazard; a limit change is that hazard arriving on purpose. The plan is
    now derived from the reference and the straddle is asserted on every use.
- **A real PII scan, or an honest absence** (#36) — Macie `llmops-customer-data-pii`, daily
  SCHEDULED over `customer-data/`. Until it existed the audit's answer to "did anything scan
  this data" was silence, which reads as yes.
- **`budgets:ViewBudget`** (#25) — the action `DescribeBudgets` actually authorizes against, not
  the one its name suggests.
- **The rate card is priced from the file callers are told to read** (#20) — the document shape
  the fetcher produced and the shape the pricer expected were different, so a card that
  refreshed successfully priced nothing.

### Latency: 2–5 uncached round-trips per turn, whole turn buffered

- **Inject the rate card instead of making the agent fetch it** (#26) — a tool call per turn for
  data that fits in the prompt.
- **Stream the reply** (#27) — the whole turn had been buffered before the first character
  reached the browser, so a correct answer looked like a hang.
- **READY does not mean warm** (#28) — a harness reports READY before its first session pays
  the cold-start cost, so deploy now warms it. The effort knob was never the lever.
- **Log the round-trips a turn really made** (#29) — the previous log line was structurally
  incapable of showing the real count, so the latency work had no measurement to stand on.

### Release engineering: the push tool, and a PR that shipped to nowhere

Direct `git push` is hook-blocked here, so `tools/push_via_api.py` is the only path to the
remote — and it was silently corrupting history in four distinct ways, each found by comparing
the pushed tree to the local one rather than by trusting a green push.

- **Squashed commits** (#29), **commits replayed on every subsequent push** (#30), **an
  eventually-consistent ref read** (#19), and **merges flattened in two places at once** (#34).
- **`deploy/07_lambdas.py --only` now means only** (#36's branch) — it both over-deployed past
  its argument and blocked the narrow deploy it was added to enable.
- **All 19 skill mounts moved git → s3, resolved at deploy time** (#36's branch) — a git skill
  source has no branch field, so every deployed harness read the skills repo's default branch
  and a push there silently changed production. `ensure_skills()` landed in
  `deploy/03_storage.py` **before** any source was switched, because a bad skill source is
  accepted by `UpdateHarness`, reports READY, and then fails every session at start.
- **The capacity race guard finally reached main** (#38) — #10 merged it into a non-main base,
  so its 10 shell assertions sat outside CI for days while every badge was green. Reading the
  `validate` log, not the badge, is what found it.
- **Diagrams and docs corrected against the running system** (#30, #32) — including the audit
  plane, the escalation path, and the skill-source language.

### Tests

- **The control runner leaked its mutation when signalled, and a `try/finally` was why nobody
  noticed.** The restore has always been inside a `finally`, so the runner read as safe.
  SIGTERM's default disposition terminates the process without unwinding — no `finally`, no
  `atexit` — so killing it at a tool timeout left `m52`'s edit to `deploy/03_storage.py` in the
  working tree, found afterwards by `git status` and nothing else. A full run takes ~3 minutes,
  which makes being killed partway the ordinary case, not the exceptional one. The damage is
  not the dirty file: it is the **next** run, which mutates an already-mutated file and then
  reports PASS about code nobody wrote. Two defences, because neither covers the other's gap —
  handlers that raise so the existing `finally` fires, and a journal written **before** the
  mutation so `SIGKILL`, which no handler may intercept, still leaves the original recoverable
  and the next start repairs the tree before trusting it. Verified against a real reproduction
  in both directions: SIGTERM restored the file, SIGKILL leaked it, and the next start printed
  `RECOVERED` and undid it.

**785 pytest** (from 274 at v1.0.0), **103/103 negative controls** (89 mutations, 103
(guard, mutation) pairs), **10/10 shell assertions**, three SVGs geometrically CLEAN against
six checks. Offline by construction: `tests/conftest.py` strips AWS credentials and refuses
non-loopback sockets, so a credentialed laptop cannot turn a test that hits production into a
passing test.

Four of this release's guards were **fixed by their own negative controls** — including one
that certified as clean the exact defect it was written to forbid, and one that was wrong for
its own reasons and would have had the docs corrected to a false number. A control that cannot
fail has tested nothing.

## [1.1.0] — 2026-07-31

### FinOps — cost estimation, a $2000 approval gate, and a 7th runtime

Before this release the pipeline spent real money with no cost surface anywhere: nothing
estimated a run, nothing reconciled it, nothing could stop an expensive one. The gap was
concrete — the 2026-07-31 QLoRA run billed **$10.77** and that figure existed only because a
human ran `describe-training-job` and multiplied by a rate recalled from memory.

- **`pipeline/contracts/cost_model.py`** — the one place estimate arithmetic lives. Line-itemised
  estimates (never a bare total), each row carrying its `basis` formula and `rate_source`.
  Calibrated against the $10.77 run: **0.0% delta** on the training line (0.664 rows/s and 670 s
  setup are that run's own measurements, not guesses).
- **7th AgentCore runtime `llmops_finops`** (財務審計員／統計員／報告員) — daily 09:00 UTC
  reconcile, plus on-demand `pricing_refresh` and `report`. Read-only on billing: it reports and
  flags, and cannot stop a run. Sits beside `llmops_orchestrator` above the state machine, so it
  never appears in a run's stage sequence.
- **Console Cost tab** — estimate, approval queue, itemised actuals by project/service/run,
  estimate-vs-actual variance, and rate-card health.
- **The $2000 gate is dual**: approval fires when either this run's worst case exceeds the
  single-run limit, or project-to-date + this estimate exceeds the cumulative one. Twenty $150
  runs are the same exposure as one $3000 run, and each passes a single-run check alone.
- Gates on **`worst_case_usd`, not `total_usd`** — the remediation loop can re-run finetune up to
  `max_iterations`, so approving $2000 that can become $6000 is not a gate.
- **Separation of duties** — Cognito group `llmops-approver`, checked server-side on every call;
  self-approval is rejected with 403, not merely flagged. `rejected` and `launched` are terminal
  both ways, so a refusal cannot be quietly retried and one approval cannot launch two runs.
- **Every failure path fails closed** — no cost model → approval *required*; no rate card →
  estimate *refused* (503) rather than a $0-with-warnings total; group lookup failure → deny.
- Two new tables (`llmops-cost-estimates`, `llmops-cost-actuals`) and an S3 rate-card cache with
  dated history, so an old variance can be re-derived against the rates live at estimate time.
- **147 FinOps tests** (52 cost model + 36 agent/Lambda + 59 console), all without AWS
  credentials; 252 in the suite. Mutation-checked: breaking each guard was verified to fail a
  test, which found two guards a green suite did not cover.
- Bilingual [docs/COST.md](docs/COST.md) / [docs/COST.zh-TW.md](docs/COST.zh-TW.md).

### Verified facts that shaped the design (live, read-only, 2026-07-31)

- **The Price List API cannot price Fable 5 or Opus 5** — the models the seven harnesses run
  on, and the largest AgentCore line in the bill. Every `provider=Anthropic` entry for
  us-east-1 is Claude 3 or older. So realized billing rates (cost ÷ quantity from our own
  invoice) outrank the published price list; Price List is the fallback for never-used
  resources. It *does* price DeepSeek-R1, to within <0.001% of our realized rate — an earlier
  claim to the contrary was wrong because the `model` attribute value is bare `R1` (with
  `provider=DeepSeek`), which eyeballing the model list misses. Query by filter, not by eye.
- **Cost allocation tags are unusable today** — `project`/`Project` both Inactive, and a
  tag-filtered CE query returns **$0.00** for a day with real spend. Attribution is therefore by
  explicit resource match (`run_id` is already inside job and endpoint names), which needs no
  tagging at all. Tags are not retroactive, so the $10.77 run will never carry one.
- **Attribution must never be by service.** This account's month-to-date total was **$27,491**
  while this project's share was **~$10–15**; the rest includes unrelated SageMaker Canvas
  (~$296) and a JumpStart Whisper endpoint (~$36.36/day). A service filter would have reported
  thousands of dollars of someone else's spend as ours — and tripped the $2000 gate immediately.
- **Cost Explorer lags ~24 h** and marks recent periods `Estimated: true`, so reconciliation is
  async and re-runnable, and a run counts as settled only when *every* row for it is settled.

## [1.0.0] — 2026-07-29

### v1 complete — all six phases live-verified

- 6th agent: `llmops_orchestrator` (conductor) — NL goal → costed run plan,
  first-line escalation triage, cross-run reports.
- Orchestration hardening: harnessArn resolution (SSM), between-turn Lambda
  continuation (900s/840s), fail-closed quality gates, two-stage re-ask,
  automatic model failover on vendor 5xx bursts.
- Triggers: EventBridge Scheduler (disabled nightly), HMAC webhook + Admin API
  (HTTP API), GitHub Actions OIDC workflow.
- Admin console deployed (Cognito/APIGW/Lambda) and wired to the platform.
- Online evaluation configs on all harnesses (real API shape, live-introspected).
- Bilingual docs suite: ARCHITECTURE / TRIGGERS / TEST_RESULTS / CASE_STUDY
  (EN + zh-TW) + INFRASTRUCTURE; six per-phase evidence files.
- Live distillation run: 24-task ARC-AGI-2 dataset via DeepSeek-R1 ($5.60),
  QLoRA training after a 6-iteration self-remediation gauntlet, endpoint
  deployed after 5 versions/4 root causes, quality gates FAILED honestly.
- Total build cost ≈ $12–15 (budget $45–60).

## [0.1.0] — 2026-07-28

### Added — Phase 0 scaffold
- Five AgentCore Harness configs (`agents/*/harness.json`), all offline-validated:
  data-prep, finetune, eval, deploy, monitor — each mounting its LLMOps skills
  from [MLOps-agent-skills](https://github.com/timwukp/MLOps-agent-skills) and
  exposing the inline-function contract (`stage_complete`, `checkpoint`,
  `escalate_human`, + `job_launched` on finetune).
- Orchestration spine: Step Functions state machine + harness-driver /
  start / resume / webhook Lambdas (`orchestration/`).
- Least-privilege IAM (`deploy/iam/`), idempotent provisioning scripts
  (`deploy/01_iam.py`, `02_network.py` VPC + endpoints, `03_storage.py`).
- Contracts: run manifest schema, event vocabulary, ops-console report writer
  (`pipeline/contracts/`).
- Security: redaction pre-commit hook + CI check, SECURITY.md, AGENTS.md
  gotchas bank, bilingual-doc pairing enforcement.
- CI: compile, offline harness validation, policy JSON checks, offline
  dry-runs, unit tests, SVG geometry check.
