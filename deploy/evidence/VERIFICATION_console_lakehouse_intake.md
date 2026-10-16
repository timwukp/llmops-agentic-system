# VERIFICATION — console intake for the lakehouse modes (2026-10-16)

**What was deployed.** The console (`deploy/console/deploy.sh`) and the orchestrator harness
(`05_harnesses.py --agent orchestrator`).

## Live

| Check | Result |
|---|---|
| `GET /api/task-types` from the deployed console | `lakehouse-ingest` (supported; required `generator_seed`, `generator_scale`) and `finetune-lakehouse` (supported; required `lakehouse_dataset`, `student_model_id`; optional budget, instance, iterations, keep_endpoint) |
| Intake verifier on `run-20261015t125141z-bcb61189` (PR-11's v2 dataset) | **verified**: "Gold train/eval 230/23; held-out acceptance set 120 items (generator v2, seed 7, demo)" |
| on `run-20261015t061901z-ff69ff34` (PR-09's v1 dataset) | **failed**: "generator v1 dataset: no held-out acceptance set, so a gate would score recall of the training phrasings -- ingest a new one" |
| on an id with no reports | **failed**: "no Gold report: run lakehouse_ingest for it first" |
| `simulate_principal_policy` as the console role `LlmopsAdminLambdaRole` | may Get `lakehouse/reports/*/gold.json` and `lakehouse/landing/*/manifest.json`; may NOT Get landing data or extracts. 4/4, so **no new grant** was needed |

The verifier ran against the live reports with the deployed code's function; the IAM simulation shows
that the console's own role reaches exactly those two objects.

## What the orchestrator is now told

Two new menu items:
- **LAKEHOUSE BUILD** (`lakehouse_ingest`): the plan must carry `generator_seed` and
  `generator_scale`.
- **LAKEHOUSE FINE-TUNE** (`lakehouse`): the plan must carry `lakehouse_dataset` (one intake verified,
  never one it marked failed), an explicitly named student, and explicitly chosen `gates` and
  `dataset`.

The fine-tune item also carries the arithmetic PR-09's borderline run lacked. At zero failures the
Wilson 95% lower bound is n/(n+3.84), so a 0.95 gate needs n ≥ 73 and cannot be decided on the 40-item
small set. A test derives every required key from start-pipeline's `MODE_REQUIRED_PARAMS` and
`MODE_REQUIRED_CHOSEN`, and fails if a menu item stops naming one.

## Not done here

A console task carried end to end needs a signed-in human: the conversation, then a KMS-signed plan
acceptance in the browser. Those are the owner's steps. Each piece that path depends on is verified
above, and every stage after dispatch was verified live in PR-09 and PR-11.
