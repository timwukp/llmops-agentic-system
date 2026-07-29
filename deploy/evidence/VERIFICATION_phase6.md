# Phase 6 verification — console, hardening, docs, final audit

Date: 2026-07-29 · Region: us-east-1 · Redacted per SECURITY.md.

## Gate

> console live; hardening landed; bilingual docs; CI green; redaction clean

**Result: PASSED.**

## llmops-admin console — deployed and wired

Deployed from timwukp/bedrock-agentcore-agent-ops-console `deploy/deploy.sh`
(idempotent; took over the existing stack and re-pointed it at this platform):

- HTTP 200 live; Cognito admin login (password in Secrets Manager
  `agent-admin/dashboard-login`)
- Monitored harnesses: `llmops_orchestrator` (primary) + `llmops_finetune`
- QA_BUCKET → the platform data bucket (driver writes
  `reports/run-latest/test-report-latest.json` every stage — contract satisfied
  by construction)
- TARGET_REPO → timwukp/llmops-agentic-system; SPANS_SINCE → platform creation
- Data feeds already flowing: OTel traces (always_on), online evaluation configs
  on all harnesses, DDB runs, S3 reports

## Hardening landed

1. **Automatic model failover in the driver** (the manually-proven procedure,
   now code): on stream death with a 5xx signature, hot-swap the harness to its
   fallback model (Fable 5 → Opus 5) via UpdateHarness, wait READY, emit a
   ModelFailover event, then same-session salvage retry. Best-effort — a
   failover failure can never break the retry path.
2. **Between-turn Lambda continuation** (from Phase 5's Sandbox.Timedout):
   proven in the final e2e iteration.
3. **Fail-closed gates** with regression test (30/30 suite green).

## GitHub Actions trigger (4th of 4)

`.github/workflows/run-pipeline.yml`: workflow_dispatch with typed inputs,
OIDC (`AWS_OIDC_ROLE_ARN` repo secret — no account id in the file, no
long-lived keys), fire-and-monitor semantics (the SFN run outlives the job).

## Bilingual documentation suite (8 files)

ARCHITECTURE / TRIGGERS / TEST_RESULTS / CASE_STUDY — each in English +
Traditional Chinese. All facts sourced from the six evidence files; the three
reproducible checks quoted in TEST_RESULTS were re-run before writing
(30/30 unit tests, 6/6 configs `RESULT: OK`, SVG geometry CLEAN).

## Final audit

- Full-repo redaction scan: account id CLEAN · keys CLEAN · non-allowlisted
  12-digit ids CLEAN
- Unit tests 30/30 · harness configs 6/6 OK · SVG geometry CLEAN
- Zero orphaned billable resources (endpoints list empty; scheduler DISABLED;
  interface-endpoint VPC config exists but is prod-only, not deployed)

## Platform cost summary (entire build, all six phases)

Teacher tokens ≈ $6.29 · training jobs ≈ $0.80 · endpoints ≈ $4 · misc ≈ $1
→ **≈ $12–15 total** — within the $45–60 validation budget with wide margin.
