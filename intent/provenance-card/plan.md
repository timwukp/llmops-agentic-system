# Plan: the provenance card on every registered model

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 972848bd2f74d4b5351c5dc14bf8670461788f2b
- **Status:** accepted

`Accepted-for` is the merge base with `main` (PR #180's merge). Based on `main`, not on #181.

## Files changed

1. `pipeline/contracts/provenance.py` (new): the builder.
2. `orchestration/publish_provenance/handler.py` (new): the publisher.
3. `orchestration/state_machine.asl.json`: `PublishProvenance`.
4. `deploy/07_lambdas.py`: the `provenance` target. `deploy/iam/lambda_roles.json`: its role.
   `deploy/iam/sfn_execution_role.json`: invoke. `deploy/01_iam.py`: role list prose.
5. Tests:
   - `tests/test_provenance_card.py` (new);
   - `tests/test_orchestration.py`, `tests/test_docs_claims.py` as the counts require;
   - `tests/negative_controls/monitor_dispatch.py`;
   - `tests/redaction_scan.py`, `tests/test_redaction_scan.py` (tracked-file count).
6. Docs and records:
   - `docs/ARCHITECTURE.md`, `docs/ARCHITECTURE.zh-TW.md`; `docs/gen_architecture_svg.py` and the
     SVGs it regenerates;
   - `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`;
   - `deploy/README.md`, `PROJECT_STATE.md`, `CHANGELOG.md`, `AGENTS.md`, `README.md`,
     `README.zh-TW.md`; `deploy/console/lambda_function.py`, `deploy/console/deploy.sh` and
     `deploy/console/frontend.html` (the Outcome tab reads runs through the same module; its cost
     line names a baseline only when there is one); `deploy/01_iam.py` (role list prose);
   - `deploy/evidence/VERIFICATION_provenance_card.md` (new);
   - `intent/provenance-card/{intent,spec,plan}.md`, `.sdlc/active`.

## Order

Builder and its tests against the live shapes → publisher and its tests → ASL, IAM, deploy target →
counts, docs, negative controls → deploy (IAM by the owner if the classifier stops it) → backfill
the demo run through the Lambda → read back from the registry and on the SMUS page.

## Rollback

Remove `PublishProvenance` (MonitorReport → Complete again) and redeploy the state machine. A card
already written stays on its package; `UpdateModelPackage` can clear it.
