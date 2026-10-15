# VERIFICATION: the console role's S3 reads, scoped to what the console reads

- **Date:** 2026-10-15
- **Role:** `LlmopsAdminLambdaRole`, inline policy `LlmopsAdminPerms` (applied by `deploy/console/deploy.sh`)
- **Change:** `S3Manifests` (`s3:GetObject` + `s3:ListBucket` on `"*"`) replaced by `S3ConsoleRead` + `S3ConsoleList`
- **Intent chain:** `intent/console-s3-scope/`

## Before

Live, the role carried `S3Manifests` on `Resource: "*"`: every object in every bucket of the account.

## What the console reads (derived, not listed)

`tests/test_console_s3_scope.py` walks the AST of `deploy/console/lambda_function.py`. It resolves each
`s3.get_object` / `head_object` / `list_objects_v2` key, and the one hand-off of the `s3` client
(`conductor_tools.service_launch_run`), to a key pattern:

| Function | Action | Pattern | From |
|---|---|---|---|
| `run_detail` | GetObject | `runs/*` | literal `runs/{run_id}/manifest.json` |
| `_rate_card_doc` | GetObject | `finops/rates/rate_card_latest.json` | literal |
| `accept_task`, `task_readiness`, `service_launch_run` | GetObject | `runs/*`, `tasks/*` | `_PLAN_S3_PREFIXES` |
| `_verify_s3_uri` (intake) | GetObject, ListBucket | `customer-data/*`, `runs/*` | `_INTAKE_S3_PREFIXES` |
| `_maybe_template_upload` | GetObject | `customer-data/*` | `_UPLOAD_S3_PREFIXES` |
| `_verify_mirror` | ListBucket | `models-mirror/*` | literal `models-mirror/{repo}/` |

The console reads nothing under `reports/`, `lakehouse/`, `code/`, `skills/`, `athena/` or any other
bucket, and the Introduction tab's assets ship in the zip. Live plan_uris in `llmops-tasks`: 13 under
`runs/<id>/plan*.json`, 0 elsewhere.

The `"*"` grant was load-bearing for one path only. Intake checked any `s3://` URI a chat message
named, in any bucket, and reported it `verified`. The harness execution role that consumes a
`source_uri` reads only data-bucket prefixes, so that verdict could describe an object the pipeline
would get AccessDenied on. Such a URI is now `unverified` with the reason, and no S3 call is made.
The 23 intake URIs on record were all under `customer-data/`.

The test checks both directions: every derived read is granted, and every grant matches a derived read.
Each of five mutations made it fail: an unguarded `uri[5:].partition` key, an extra `reports/*` grant,
a dropped `runs/*` grant, `Resource: "*"`, and a new intake prefix with no grant.

## Deploy

`deploy/console/deploy.sh` (REGION=us-east-1). The first sandboxed run died at `mktemp -d`: macOS
ignores `$TMPDIR` when no template is given. That was **after** `put-role-policy`, so for a few minutes
the new policy ran with the old code. The old code fails open on a denied read (`unverified`), and the
script now gives `mktemp` a template under `$TMP_DIR`. Re-run: vendored boto3 1.43.114, package 29 MB,
function and API stage updated.

## Live read-back

The live `LlmopsAdminPerms` equals `iam-policy.json` after substitution, with `_comment` stripped:
**True**.

`simulate_principal_policy` on the role. Allowed cases are generated from the test's derivation (each
pattern's `*` replaced by a concrete key); ListBucket cases pass `s3:prefix` as context:

| Action | Bucket | Key / s3:prefix | Expected | Got | |
|---|---|---|---|---|---|
| s3:GetObject | `<DATA_BUCKET>` | `customer-data/verify-2026-10-15/obj.json` | allowed | allowed | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `finops/rates/rate_card_latest.json` | allowed | allowed | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `runs/verify-2026-10-15/obj.json` | allowed | allowed | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `tasks/verify-2026-10-15/obj.json` | allowed | allowed | PASS |
| s3:ListBucket | `<DATA_BUCKET>` | `customer-data/verify-2026-10-15/obj.json` | allowed | allowed | PASS |
| s3:ListBucket | `<DATA_BUCKET>` | `models-mirror/verify-2026-10-15/obj.json` | allowed | allowed | PASS |
| s3:ListBucket | `<DATA_BUCKET>` | `runs/verify-2026-10-15/obj.json` | allowed | allowed | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `reports/x.json` | implicitDeny | implicitDeny | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `lakehouse/gold/x.parquet` | implicitDeny | implicitDeny | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `code/distill/train_qlora.py` | implicitDeny | implicitDeny | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `finops/rates/history/x.json` | implicitDeny | implicitDeny | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `skills/llmops/x/SKILL.md` | implicitDeny | implicitDeny | PASS |
| s3:GetObject | `<DATA_BUCKET>` | `athena/x.csv` | implicitDeny | implicitDeny | PASS |
| s3:GetObject | `<OTHER_BUCKET>` (a real bucket in the account) | `runs/x/manifest.json` | implicitDeny | implicitDeny | PASS |
| s3:GetObject | `llmops-unrelated-example` | `customer-data/x.jsonl` | implicitDeny | implicitDeny | PASS |
| s3:ListBucket | `<DATA_BUCKET>` | (no prefix) | implicitDeny | implicitDeny | PASS |
| s3:ListBucket | `<DATA_BUCKET>` | `lakehouse/` | implicitDeny | implicitDeny | PASS |
| s3:ListBucket | `<DATA_BUCKET>` | `tasks/` | implicitDeny | implicitDeny | PASS |
| s3:ListBucket | `<OTHER_BUCKET>` | `runs/` | implicitDeny | implicitDeny | PASS |

**19/19 PASS.**

### As the runtime principal

Simulation evaluates IAM alone. The deployed Lambda also served the public `GET /api/run`:
- a real run (`run-20260806T001053Z-fb41420e`): manifest read, no error;
- a nonexistent run: `NoSuchKey`, not AccessDenied. Under the prefix-scoped ListBucket, S3 still
  answers a missing `runs/` key with 404. Intake decides existence by listing (`Prefix=key, MaxKeys=1`)
  either way, so its "object not found" does not depend on that.

## Offline

`tests/test_console_s3_scope.py`: 11 tests. `tests/test_console_tasks.py`: 6 new cases (out-of-scope
intake is `unverified` and makes no S3 call, template upload outside `customer-data/`, accept 409
outside the plan prefixes, readiness note), plus the two intake tests moved to in-scope URIs.

## Not changed

The orchestrator prompt still says intake checks with the console's "broader IAM" and that a customer
whose data "already sits elsewhere in S3" should give that URI. That URI now comes back `unverified`,
which the prompt already tells the agent to raise as an open question. Changing the wording needs a
harness update, so it is left as a follow-up.
