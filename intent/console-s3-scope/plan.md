# Plan: console S3 read scope

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** d8b989456dc0730a6542fc7a44525eda4f3ff885
- **Status:** accepted

`Accepted-for` is the merge base with `main` at acceptance (PR #161's merge).

## Files changed

1. `deploy/console/iam-policy.json`: replace `S3Manifests` with `S3ConsoleRead` + `S3ConsoleList`.
2. `deploy/console/lambda_function.py`: the scope constants, `_scoped_key`, guarded reads, list-based
   intake existence.
3. `deploy/console/deploy.sh`: `${TMPDIR:-/tmp}`.
4. `tests/test_console_s3_scope.py` (new). `tests/test_console_tasks.py`: FakeS3 list returns `Size`;
   intake tests use in-scope URIs, plus out-of-scope cases.
5. `deploy/evidence/VERIFICATION_console_s3_scope.md` (new), `docs/TEST_RESULTS.md`,
   `docs/TEST_RESULTS.zh-TW.md`, `CHANGELOG.md`, `PROJECT_STATE.md`.
6. `tests/redaction_scan.py`, `tests/test_redaction_scan.py`: census counts, if they move.
7. `intent/console-s3-scope/{intent,spec,plan}.md`, `.sdlc/active`.
