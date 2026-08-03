# Changelog

All notable changes to this project are documented here.
Format: [Keep a Changelog](https://keepachangelog.com/); versioning: SemVer.

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

**783 pytest** (from 274 at v1.0.0), **99/99 negative controls** (85 mutations, 99
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
