# Plan: the negative-control runner verifies every pair it is credited with

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** f22ca65a802d22d154b022fa08bb5895ef1f2470
- **Status:** accepted

Accepted on the owner's reply of 2026-11-21 (this session): "Accept plan", against `0459fbc`
(`main` at PR #195's merge). While the change was being verified, `main` moved to `f22ca65`
(PRs #196 to #200, the retrieval series). #196 edits the same count lines, the CHANGELOG head, the
redaction census and `.sdlc/active`. The owner re-bound the plan on 2026-11-27 (this session):
"改綁到 f22ca65". `Accepted-for` is that commit, the base this branch is built on. #196's 6
controls (9 pairs) sit above the runner loop, so on `f22ca65` this change gives 481 mutations and
566 pairs. Spec 3's 475 and 557 were counted on `0459fbc`.

## Files changed

1. Runner and guard: `tests/negative_controls/monitor_dispatch.py`, `tests/test_docs_claims.py`.
2. Census: `tests/redaction_scan.py`, `tests/test_redaction_scan.py`. The tracked-file total moves
   with this change's new files, and the census guard requires the comment to move in the same
   change.
3. Records: `CHANGELOG.md`, `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`,
   `deploy/evidence/VERIFICATION_negative_control_runner.md` (new),
   `intent/negative-control-runner/{intent,spec,plan}.md`, `.sdlc/active`.

## Order

1. The guard (spec 2), red against the current file.
2. Move the runner block (spec 1). The guard goes green.
3. m401 (spec 3) and the m315 rewrite (spec 4).
4. A full runner run, then the full suite, then the records (spec 5) with the measured numbers.
5. Push via the Git Data API (`tools/push_via_api.py`). Open a PR to `main` and leave it unmerged.

## Deploy

None. Test infrastructure and documentation only. No AWS resource, IAM or bundle changes.

## Rollback

Revert the commit.
