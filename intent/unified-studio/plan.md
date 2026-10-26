# Plan: the llmops Unified Studio execution role

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 972848bd2f74d4b5351c5dc14bf8670461788f2b
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #180's merge).

## Files changed

1. `deploy/11_studio.py` (new): the role, its policies, `--dry-run` / `--verify` / `--teardown`.
2. Tests:
   - `tests/test_studio.py` (new);
   - `tests/negative_controls/monitor_dispatch.py` (m394–m400);
   - `tests/redaction_scan.py` and `tests/test_redaction_scan.py` (tracked-file count).
3. Docs and records:
   - `deploy/README.md` (run-order row 11);
   - `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`;
   - `PROJECT_STATE.md`, `CHANGELOG.md`, `AGENTS.md` (gotcha: Lake Formation on S3 Tables);
   - `deploy/evidence/VERIFICATION_unified_studio.md` (new);
   - `deploy/evidence/VERIFICATION_m1_lakehouse_e2e.md` (the one-click run, live);
   - `intent/unified-studio/{intent,spec,plan}.md`, `.sdlc/active`.

## Order

Role and offline tests → live `--dry-run`, then a read-only `simulate_custom_policy` of the managed
policy with and without the inline policy → create the role → `--verify` → the owner's console
setup → `--verify` again (domain) → a Studio query each way → one lakehouse run through the pipeline.

## Rollback

`deploy/11_studio.py --teardown` after the owner deletes the domain in the console. Nothing in the
pipeline changes, so there is nothing of the pipeline's to roll back.
