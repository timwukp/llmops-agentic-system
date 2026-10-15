# VERIFICATION: the orchestrator's prompt states the data scope the platform enforces

- **Date:** 2026-10-15
- **Harness:** `llmops_orchestrator`, deployed as **version 20** (`deploy/05_harnesses.py --agent orchestrator`)
- **Intent chain:** `intent/orchestrator-intake-scope/`
- **Tool:** `tools/probe_orchestrator_intake.py`

## The two sentences

| Where | Before | After |
|---|---|---|
| Step 0 | "If their data already sits elsewhere in S3, ask for that exact URI instead." | Data is usable in place ONLY under `s3://<bucket>/customer-data/` (data-prep refuses a `source_uri` anywhere else; no stage reads another bucket); anything else comes in through the drop zone. |
| Intake | "the console checked those claims with its own broader IAM" | The console checks S3 only under `customer-data/` and `runs/`. A fact whose detail starts "not checked: outside" was NOT examined and is not evidence the object is missing. |

The step-0 sentence was wrong before #163. Data-prep's prompt already refused any `source_uri` outside
`customer-data/`.

## Offline

`tests/test_console_s3_scope.py` gains 3 guards (14 tests in the file). Each check is derived from its
source: the console's `_INTAKE_S3_PREFIXES`, the "not checked" f-string head in `_verify_s3_uri`, and
data-prep's own "must sit under the data bucket's X prefix". All 3 fail on the old prompt and pass on the
new one. `deploy/validate_config.py`: `RESULT: OK`.

## Tested BEFORE deploying

The owner's rule for this change: test first. The repo's prompt was passed per call through
`InvokeHarness`'s first-class `systemPrompt` field, so the live harness was not modified. The same calls
without the override are the baseline, which was the live prompt at that point. Each call opened a fresh
session ending in a data-discovery reply. No inline function was called and nothing was dispatched.
17 calls, 452,976 input / 26,737 output tokens.

**C0, is the override faithful?** AGENTS.md records that `additionalParams.system` loses the runtime's
skills manifest. Asked to list its skills, the agent gave the same 4 skills at the same
`.agents/skills/s3/<name>/SKILL.md` paths with and without the override. Input tokens were 6,814 vs
7,012: the +198 is the new prompt's ~595 extra characters, not a 1,148-token manifest loss. So the
first-class field keeps the manifest.

Pass criteria were written before the first run, and replies were graded by reading them:

| Scenario | New prompt (override) | Live prompt (baseline) |
|---|---|---|
| **S1** customer says the data is in `s3://acme-support-ml/...` | **3/3 PASS**: "It cannot read `s3://acme-support-ml/...` — no stage has access to that bucket"; drop zone | **0/2**: both offered the drop zone, AND both offered a path that does not exist: "If you'd rather grant cross-account read access to your bucket, that's possible too" |
| **S2** intake fact `unverified`, "not checked: outside …" | **3/3 PASS**: "The console didn't check that object … That's not a verdict on your file; it just hasn't been examined"; drop zone | **0/2**: both offered the drop zone, AND both said "The console checked your `source_uri` and flagged it", which is false |
| **S3** control: `customer-data/` upload, console-verified | **2/2 valid PASS**: treated as verified, moved to data discovery, no second trip to the drop zone; 1 invalid (below) | 1 PASS; 1 invalid (below) |

**S3's fixture was defective.** The "verified" fact named an object the probe never uploaded, and two
runs went and read it:
- The new-prompt run got a 404, said so ("I'm not doubting the console — the file clearly landed —
  but something removed it"), and asked for a re-upload. That is right for a missing object, but it
  does not test over-correction, so it is invalid.
- The live-prompt run got the same 404 and explained it away: "my runtime role can't read it, which is
  expected; the console's verification stands (it checked with broader IAM)". The harness role does read
  `customer-data/`; the object simply did not exist. The old sentence gave the agent a reason to dismiss
  a real absence.

A clean S3 needs a real object under `customer-data/`. Uploading one through the console's drop zone
is the operator's step and was not done; the owner judged the results sufficient to deploy.

## Deploy

- In-flight gate: 0 RUNNING `llmops-pipeline` executions, 0 consult tasks `thinking` or `accepting`.
- Drift gate: `tools/audit_drift.py` showed `systemPrompt` as the orchestrator's only drift, so the
  update could not revert another field.
- `05_harnesses.py --region us-east-1 --agent orchestrator`: `updated`, `READY`,
  `config_confirmed: true`, `harness_version: 20`, warmed.

## After deploying (no override)

- The tool's `GetHarness` read-back: live prompt == repo prompt (`prompts_differ: false`).
- C0: the same 4 skills and paths; 8,649 input tokens with and without the override.
- **S1 PASS:** "it cannot reach `s3://acme-support-ml/datasets/train.jsonl` (different bucket, no
  cross-account access)", then the drop zone.
- **S2 PASS:** "the console did *not* inspect that object; it simply can't be used where it is", then
  the drop zone.

## Found: the probe's own memory leaked across its scenarios

The post-deploy S2 reply also said: "Our records from your previous session show a copy was uploaded
… at `s3://<DATA_BUCKET>/customer-data/probe-orchestrator-intake-scope/train.jsonl`". That is the earlier
S3 calls' fabricated fixture, retrieved from memory, because every call shared one `actorId`. The
agent hedged ("if that file is current … confirm"; data-prep re-validates), and the isolation from real
actors held. Still, a probe whose scenarios can read each other's memory is not measuring one scenario
at a time. The tool now gives every call its own `actorId`.

**Left in the shared memory, not deleted** (deletion is the owner's decision):
13 records in `/users/probe-orchestrator-intake-scope/facts` and 9 in
`/episodes/probe-orchestrator-intake-scope`, plus per-session episode namespaces under it. No real
actor's namespace is affected.

## Not in this change

`tools/audit_drift.py` also reported drift unrelated to this change: `main` is ahead of what is live in
`llmops-harness-execution`'s inline policy, the `llmops_monitor` prompt, the state machine's
`PipelineModeChoice`, the driver and start-pipeline handlers, and `manifest.schema.json` in 7 Lambdas.
None of it was deployed here.
