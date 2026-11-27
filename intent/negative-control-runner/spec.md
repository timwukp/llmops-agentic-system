# Spec: the negative-control runner verifies every pair it is credited with

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

Signed off on the owner's reply of 2026-11-21 (this session): "Sign off spec".

## Requirements

1. **The runner runs last** (`tests/negative_controls/monitor_dispatch.py`).
   - The runner block moves, unchanged, to the end of the file, below every case block. The block
     is: the module-level `if __name__ == "__main__":` that installs the signal handlers and calls
     `_restore_from_journal()`, `failed = []`, the `for name, rel, mutate, tests in CASES` loop,
     and the summary.
   - A comment at the head of the block says it must stay last, and why: a case registered below
     it is counted by the docs and never run.
   - The loop body does not change. The existing signal and journal guards
     (`test_the_control_runner_restores_its_mutation_even_when_signalled`) keep passing as they are.
2. **A registration below the loop fails the suite** (`tests/test_docs_claims.py`, next to
   `test_the_documented_negative_control_count_matches_the_runner`).
   - New test `test_no_negative_control_is_registered_after_the_runner_loop`. It finds the one
     module-level `for` statement whose iterable names `CASES` (it fails if there are zero or
     several), then fails if anything after that line registers a case. "Registers" means a
     `case(...)` call, or a `CASES.append/extend/insert` call or an assignment to `CASES`
     outside `def case`. The failure message names each late registration's line.
   - The existing count guard also checks the mutation count the claim sentence states
     (`N mutations` in EN, `N 個 mutation` in zh-TW) against the number of `case(...)`
     registrations. Today only the pair count is checked, and the mutation count sits in the same
     sentence, typed by hand.
3. **A control for the new guard.** `m401` appends a `case(...)` call to the end of the runner's
   own text, the shape Batch 5 had. It lists the new test. It is registered above the runner
   block. That makes 475 mutations and 557 pairs.
4. **m315 breaks behaviour, not the import.** The mutation deletes the `DEFAULT_PARAMS` entry
   **and** rewrites `APPROVAL_STATUS_DEFAULT = DEFAULT_PARAMS["model_package_approval_status"]`
   to `DEFAULT_PARAMS.get("model_package_approval_status")`. Each anchor is asserted. The module
   still imports, `APPROVAL_STATUS_DEFAULT` is `None`, and `_merge_params` sets the merged value to
   `None`. That is the control's intent: the prompt reads the approval status and nothing supplies
   one. Its 3 listed tests stay as they are. Measured on a scratch copy of `main`: each fails by
   its own `AssertionError` with pytest exit 1.
5. **Records.**
   - `docs/TEST_RESULTS.md` and `docs/TEST_RESULTS.zh-TW.md`: the counts the guards derive
     (negative controls, mutations, collected tests), and one sentence in each language on why the
     count used to overstate.
   - `CHANGELOG.md`.
   - `deploy/evidence/VERIFICATION_negative_control_runner.md` (new): the runner's summary from a
     full run, the PASS count against the registered pair count, and the run time.
   - The redaction census in `tests/redaction_scan.py` / `tests/test_redaction_scan.py` moves by
     the new tracked files.

## Test plan

- TDD: the new guard is written first and must fail on the current file, naming Batch 5's 5 late
  registrations. Then the block moves and the guard passes.
- The m401 control, run through the runner, must PASS: the guard fails with exit 1.
- One full runner run from a clean tree, in the background, with no edits to the tree while it
  runs. It must print `all guards caught their break` with 557 PASS lines.
- The full suite (`.venv/bin/python -m pytest tests/ -q`). The two tests this sandbox cannot run
  are named in the evidence file. CI runs them.

## Non-goals

- No change to which tests any existing control lists, or to any guarded source file.
