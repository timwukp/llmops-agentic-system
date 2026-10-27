# Plan: identifier grounding, in the data and in the gate

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** c84e0c48e6fb9f2a9f39acaf29f3e422ceb012e0
- **Status:** accepted

`Accepted-for` is the merge base with `main`: PR #182's merge. First written against #181's merge
(`31e5a95`) and replayed onto #182's when it merged; the scope did not change.

## Files changed

1. `pipeline/lakehouse/transform.py`, `pipeline/lakehouse/generate.py`: the rule, the dedup after it,
   the contract, the Gold DQ rule.
2. `pipeline/eval/identifier_grounding.py` (new): the scorer.
3. `agents/eval/harness.json`: the clause.
4. `deploy/console/lambda_function.py`: the demo preset's gate. `pipeline/contracts/provenance.py`:
   the fourth gate shape, `tests/test_provenance_card.py` its fixture.
5. Tests:
   - `tests/test_identifier_grounding.py` (new), `tests/test_lakehouse_transform.py`,
     `tests/test_orchestration.py` (the pinned eval instrument set);
   - `tests/negative_controls/monitor_dispatch.py`;
   - `tests/redaction_scan.py`, `tests/test_redaction_scan.py` (tracked-file count).
6. Docs and records:
   - `docs/TEST_RESULTS.md`, `docs/TEST_RESULTS.zh-TW.md`;
   - `AGENTS.md`, `CHANGELOG.md`, `PROJECT_STATE.md`;
   - `deploy/evidence/VERIFICATION_identifier_grounding.md` (new);
   - `deploy/evidence/VERIFICATION_provenance_card.md` (the Outcome tab reading that found this,
     whose own commit missed #182's merge);
   - `intent/identifier-grounding/{intent,spec,plan}.md`, `.sdlc/active`.

## Order

Rule and scorer with tests → eval prompt → deploy (`10_lakehouse.py` jobs, `03_storage.py` mirror,
`05_harnesses.py --agent eval`) → re-judge the 2026-10-23 model through start-pipeline (`eval_only`,
gates incl. `identifier_grounding: 0.95`) → one build-then-train run through start-pipeline with the
same gates. The console preset ships with a console deploy from this branch once it carries #182
(replayed onto #182's merge), never from a branch without it.

## Rollback

Revert the commit and redeploy the same three targets; Silver and Gold rebuild per run.
