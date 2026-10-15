# VERIFICATION: the console role's S3 reads, scoped to what the console reads

- **Date:** 2026-10-15
- **Role:** `LlmopsAdminLambdaRole`, inline policy `LlmopsAdminPerms` (applied by `deploy/console/deploy.sh`)
- **Change:** `S3Manifests` (`s3:GetObject` + `s3:ListBucket` on `"*"`) replaced by `S3ConsoleRead` + `S3ConsoleList`
- **Deployed tree:** `main` at `7349dc6` (incl. #162, the Data tab) + this change, 2026-10-15 03:30:55 UTC
- **Intent chain:** `intent/console-s3-scope/`

## Before

Live, the role carried `S3Manifests` on `Resource: "*"`: every object in every bucket of the account.

## What the console reads (derived, not listed)

`tests/test_console_s3_scope.py` walks the AST of `deploy/console/lambda_function.py`. It resolves each
`s3.get_object` / `head_object` / `list_objects_v2` key, and the one hand-off of the `s3` client
(`conductor_tools.service_launch_run`), to a key pattern:

| Function | Action | Pattern | From |
|---|---|---|---|
| `run_detail` | GetObject | `runs/*/manifest.json` | literal `runs/{run_id}/manifest.json` |
| `_rate_card_doc` | GetObject | `finops/rates/rate_card_latest.json` | literal |
| `accept_task`, `task_readiness`, `service_launch_run` | GetObject | `runs/*`, `tasks/*` | `_PLAN_S3_PREFIXES` |
| `_verify_s3_uri` (intake) | GetObject, ListBucket | `customer-data/*`, `runs/*` | `_INTAKE_S3_PREFIXES` |
| `_maybe_template_upload` | GetObject | `customer-data/*` | `_UPLOAD_S3_PREFIXES` |
| `_verify_mirror` | ListBucket | `models-mirror/*/` | literal `models-mirror/{repo}/` |
| `data_overview` (Data tab, #162) | ListBucket | `lakehouse/reports/` | `LAKEHOUSE_REPORTS` |
| `data_run` via `_s3_json` (Data tab) | GetObject | `lakehouse/reports/*/*`, `lakehouse/landing/*/manifest.json` | `LAKEHOUSE_REPORTS` / `LAKEHOUSE_LANDING` f-strings |

Interpolations become `*` unless they are module string constants. So the Data tab's landing read is
`lakehouse/landing/*/manifest.json` and not `lakehouse/landing/*`: landing holds the raw generated data,
PII plants included, and the tab shows only the reports and the manifest. The console reads nothing
under `reports/`, `code/`, `skills/`, `athena/`, the rest of `lakehouse/`, or any other bucket. The
Introduction tab's assets ship in the zip. Live plan_uris in `llmops-tasks`: 13 under
`runs/<id>/plan*.json`, 0 elsewhere.

The `"*"` grant was load-bearing for one path only. Intake checked any `s3://` URI a chat message
named, in any bucket, and reported it `verified`. The harness execution role that consumes a
`source_uri` reads only data-bucket prefixes, so that verdict could describe an object the pipeline
would get AccessDenied on. Such a URI is now `unverified` with the reason, and no S3 call is made.
The 23 intake URIs on record were all under `customer-data/`.

The test checks both directions: every derived read is granted, and every grant fits inside a derived
glob (not merely overlaps one). Each of these mutations made it fail: an unguarded `uri[5:].partition`
key, an extra `reports/*` grant, a dropped `runs/*` grant, `Resource: "*"`, a new intake prefix with no
grant, `lakehouse/landing/*/manifest.json` widened to `lakehouse/landing/*`, a dropped
`lakehouse/reports/*/*` grant, and `_s3_json` reading the landing data. The widened-landing mutation
PASSED the first version of the check, which asked only that a grant overlap some read. That gap was
found while adding the Data tab's grants.

## Deploy, and a regression it caused

1. 03:04 UTC (`deploy/console/deploy.sh`, REGION=us-east-1). The first sandboxed run died at
   `mktemp -d`, **after** `put-role-policy`. The re-run completed. Both runs deployed a branch cut from
   `d8b9894`, before #162 (the Data tab, live since 2026-10-14) merged. So both runs **removed the live
   Data tab** (the code, and its `LakehouseTablesMetadata` statement).
2. 03:16 UTC: `llmops-admin` was redeployed from `main`, by a deploy outside this session. That restored
   the Data tab and also restored `S3Manifests` on `"*"`. The first 19/19 simulation in this PR described
   a policy that had already been replaced.
3. Found when PR #163 reported CONFLICTING. `main` was merged into the branch. The derivation was
   extended to the Data tab's reads (helper `_s3_json`, module-constant f-strings), and grants were added
   for them.
4. 03:30:55 UTC: redeployed from the merged tree (`main` at `7349dc6` plus this change). Everything below
   is against this deploy.

Lesson: the console has one Lambda and one inline policy, and anyone who deploys replaces both. Deploy
only from a tree that contains `main`.

## Live read-back

The live `LlmopsAdminPerms` equals `iam-policy.json` after substitution, with `_comment` stripped:
**True**.

`simulate_principal_policy` on the role, after deploy 4. The allowed cases are generated from the
test's derivation, with each `*` replaced by a concrete key. ListBucket cases pass `s3:prefix` as
context:

| Action | Bucket | Key / s3:prefix | Expected | Got | |
|---|---|---|---|---|---|
| s3:GetObject | `<DATA_BUCKET>` | `customer-data/verify-2026-10-15/obj.json` | allowed | allowed | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `finops/rates/rate_card_latest.json` | allowed | allowed | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `lakehouse/landing/verify-2026-10-15/obj.json/manifest.json` | allowed | allowed | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `lakehouse/reports/verify-2026-10-15/obj.json/verify-2026-10-15/obj.json` | allowed | allowed | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `runs/verify-2026-10-15/obj.json` | allowed | allowed | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `runs/verify-2026-10-15/obj.json/manifest.json` | allowed | allowed | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `tasks/verify-2026-10-15/obj.json` | allowed | allowed | PASS |
| s3:ListBucket | `<DATA_BUCKET>` | `customer-data/verify-2026-10-15/obj.json` | allowed | allowed | PASS |
| s3:ListBucket | `<DATA_BUCKET>` | `lakehouse/reports/` | allowed | allowed | PASS |
| s3:ListBucket | `<DATA_BUCKET>` | `models-mirror/verify-2026-10-15/obj.json/` | allowed | allowed | PASS |
| s3:ListBucket | `<DATA_BUCKET>` | `runs/verify-2026-10-15/obj.json` | allowed | allowed | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `reports/x.json` | implicitDeny | implicitDeny | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `lakehouse/landing/seed7-demo-20261014-110922/conversations.jsonl` | implicitDeny | implicitDeny | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `lakehouse/gold/x.parquet` | implicitDeny | implicitDeny | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `code/distill/train_qlora.py` | implicitDeny | implicitDeny | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `finops/rates/history/x.json` | implicitDeny | implicitDeny | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `skills/llmops/x/SKILL.md` | implicitDeny | implicitDeny | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `athena/x.csv` | implicitDeny | implicitDeny | PASS |
| s3:GetObject | `<OTHER_BUCKET>` (a real bucket in the account) | `runs/x/manifest.json` | implicitDeny | implicitDeny | PASS |
| s3:GetObject | `llmops-unrelated-example` | `customer-data/x.jsonl` | implicitDeny | implicitDeny | PASS |
| s3:ListBucket | `<DATA_BUCKET>` | (no prefix) | implicitDeny | implicitDeny | PASS |
| s3:ListBucket | `<DATA_BUCKET>` | `lakehouse/` | implicitDeny | implicitDeny | PASS |
| s3:ListBucket | `<DATA_BUCKET>` | `lakehouse/landing/` | implicitDeny | implicitDeny | PASS |
| s3:ListBucket | `<DATA_BUCKET>` | `tasks/` | implicitDeny | implicitDeny | PASS |
| s3:ListBucket | `<OTHER_BUCKET>` | `runs/` | implicitDeny | implicitDeny | PASS |

**25/25 PASS.**

### As the runtime principal

Simulation evaluates IAM alone. So the deployed Lambda also served its public GET routes:
- `GET /api/data` (Data tab): 6 datasets from the `lakehouse/reports/` list, all 10 tables, no errors.
- `GET /api/data/run?dataset=seed7-demo-20261015-030456`: land / silver / gold reports and the landing
  manifest all read. Manifest sha256 `7a05b9ad…` (the same as #162's evidence), 283 Silver pairs, 251/32
  Gold, plant check present, 8 rules.
- `GET /api/run` for a real run (`run-20260806T001053Z-fb41420e`): manifest read, no error.
- `GET /api/run` for a nonexistent run: `NoSuchKey`, not AccessDenied. Under the prefix-scoped
  ListBucket, S3 still answers a missing `runs/` key with 404. Intake decides existence by listing
  (`Prefix=key, MaxKeys=1`) either way, so its "object not found" does not depend on that.

## Offline

`tests/test_console_s3_scope.py`: 11 tests. `tests/test_console_tasks.py`: 6 new cases (out-of-scope
intake is `unverified` and makes no S3 call, template upload outside `customer-data/`, accept 409
outside the plan prefixes, readiness note), plus the two intake tests moved to in-scope URIs.

## Not changed

The orchestrator prompt still says intake checks with the console's "broader IAM" and that a customer
whose data "already sits elsewhere in S3" should give that URI. That URI now comes back `unverified`,
which the prompt already tells the agent to raise as an open question. Changing the wording needs a
harness update, so it is left as a follow-up.
