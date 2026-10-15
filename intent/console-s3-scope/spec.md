# Spec: console S3 read scope

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. The console's S3 reads, from the AST of `deploy/console/lambda_function.py`:
   - `runs/{run_id}/manifest.json` (run detail), GetObject;
   - `finops/rates/rate_card_latest.json` (rate card), GetObject;
   - plan.json from the task's `plan_uri` (accept, readiness panel), GetObject. Live plan_uris are
     `runs/<id>/plan*.json`; the default handed to the orchestrator is `tasks/<id>/plan.json`;
   - intake: a customer-named URI (HeadObject/GetObject, ListBucket for an existence check or a prefix),
     and the drop-zone template upload under `customer-data/`;
   - `models-mirror/<repo>/` (mirror presence), ListBucket;
   - the Data tab (#162, merged in): `lakehouse/reports/` (list), `lakehouse/reports/<ds>/*.json` and
     `lakehouse/landing/<ds>/manifest.json` through `_s3_json`. The landing data itself is not read;
     the grant must not reach it;
   - the vendored `conductor_tools.service_launch_run`, handed the console's `s3` client, reads the
     plan_uri.
2. Dynamic keys go through one guard, `_scoped_key(uri, prefixes)`: it returns `(bucket, key)` only
   for the data bucket under a declared prefix tuple, otherwise `None`. `_PLAN_S3_PREFIXES =
   ("runs/", "tasks/")`, `_INTAKE_S3_PREFIXES = ("customer-data/", "runs/")`, uploads `customer-data/`.
   An intake URI outside scope is `unverified`, and the detail says why. Accepting a plan_uri outside
   scope is a 409. Today accept reads `data_bucket()` plus the URI's key and ignores the URI's own
   bucket, so it could hash a different object than the one `launch_run` reads.
3. Intake decides existence by `list_objects_v2(Prefix=key, MaxKeys=1)`, not by a HeadObject 404. A
   ListBucket grant scoped by `s3:prefix` may make S3 return 403 rather than 404 for a missing key.
   The list answers either way.
4. Policy: `S3ConsoleRead` (GetObject on `runs/*`, `tasks/*`, `customer-data/*`, and the rate-card key)
   and `S3ConsoleList` (ListBucket on the bucket ARN with `StringLike s3:prefix` on `runs/*`,
   `customer-data/*`, `models-mirror/*`). Same `llmops-agentic-ACCOUNT_ID-REGION` placeholder.
5. `tests/test_console_s3_scope.py` (offline): walks the AST, resolves each read to a pattern (literal,
   f-string head, or the guard's prefix constant), and asserts both directions against the policy.
   Also: no GetObject/ListBucket on `*`, every dynamic read is guarded, every hand-off of `s3` is
   declared.
6. `deploy.sh` writes its temp files under `${TMPDIR:-/tmp}`.
7. Live: redeploy, `simulate_principal_policy` per derived pattern (allowed) and controls (denied).
   Logged in `deploy/evidence/VERIFICATION_console_s3_scope.md` and `docs/TEST_RESULTS{,.zh-TW}.md`.
