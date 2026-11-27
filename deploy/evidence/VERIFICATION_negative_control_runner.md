# Verification: the negative-control runner runs every pair the docs credit it with

Intent chain: `intent/negative-control-runner/`. Dates: 2026-11-21 (built and verified on `main`
at `0459fbc`) and 2026-11-27 (rebuilt and re-verified on `main` at `f22ca65`, after PRs #196 to
#200 merged).
Offline only: test infrastructure and documentation, no AWS resource touched.

## Found

While verifying the retrieval network-seal change, the runner printed 555 PASS lines for 565
registered pairs on that branch. The 10 missing pairs are on `main` too, where the docs said
**556/556**:

- **7 pairs never ran.** Batch 5's 5 cases (`verify_sandbox.py` / `03_storage.py`) were appended
  below the runner loop (line 7274 on `main`), at lines 7339-7359, so they registered after the
  loop had finished and the summary had printed. The count guard reads every `case(...)` in the
  AST, so it counted them.
- **3 pairs could not pass.** m315 deleted `"model_package_approval_status"` from `DEFAULT_PARAMS`.
  Since `1d406ce`, `APPROVAL_STATUS_DEFAULT = DEFAULT_PARAMS["model_package_approval_status"]` runs
  at import, so the deletion is a `KeyError` at import and pytest exits 4. The runner scores only
  exit 1 as a catch.

## Changed

- The runner block moved, unchanged, to the end of `tests/negative_controls/monitor_dispatch.py`,
  with a comment saying it must stay last.
- `test_no_negative_control_is_registered_after_the_runner_loop` (new, `tests/test_docs_claims.py`)
  fails if a `case(...)` call, or any other addition to `CASES` outside `def case`, sits below the
  one module-level loop over `CASES`. Written first. Against `main` it failed with
  `5 registration(s) at line(s) [7339, 7344, 7349, 7353, 7359] come after the runner loop at line
  7274`.
- Control m401 appends a `case(...)` to the runner's own text and lists that test.
- m315 also rewrites the import-time lookup to `DEFAULT_PARAMS.get(...)`. The module imports,
  `APPROVAL_STATUS_DEFAULT` is `None`, and `_merge_params` sets the merged value to `None`. On a
  scratch copy of `main`, each of the 3 listed tests failed by its own `AssertionError` with pytest
  exit 1:
  - `test_every_model_param_a_prompt_reads_is_one_the_driver_supplies`: "DEFAULT_PARAMS does not
    supply one -- the prompt reads it and gets nothing"
  - `test_every_param_a_prompt_reads_has_something_that_writes_it`: "classified as having a
    default, but DEFAULT_PARAMS does not define it"
  - `test_the_deploy_prompt_is_told_where_the_artifact_it_serves_comes_from`: "nothing puts it
    there -- the exact defect this test was written for"
- `test_the_documented_negative_control_count_matches_the_runner` also checks the mutation count
  the TEST_RESULTS sentence states (`481 mutations` on `f22ca65`), which was typed by hand.

## Verified

Both runs are from a fully staged tree, with nothing edited while they ran. After each run the
working tree matched the index, so every mutation was restored.

**On `main` at `f22ca65` plus this change (what the PR carries), 2026-11-27:**

- **Full runner run:** `.venv/bin/python tests/negative_controls/monitor_dispatch.py` printed **566
  PASS lines**, 0 FAIL, 0 SKIP-BROKEN, 0 RECOVERED. It ended `all guards caught their break` (exit 0)
  in 8 min 18 s. 566 is the pair count `test_the_documented_negative_control_count_matches_the_runner`
  derives from the 481 registrations. Among the 566:
  - Batch 5's 7 pairs: `isolation is off by default`, `the worker stops scrubbing credentials` (2),
    `the TLS deny is inverted`, `tables lose deletion protection` (2), `the stage-events table loses
    PITR`. Each `1 failed`.
  - m315's 3 pairs, each `1 failed` (exit 1).
  - m401: `test_no_negative_control_is_registered_after_the_runner_loop` `1 failed in 0.09s`.
  - The retrieval-network pairs #196 added, all above the loop, all PASS.
- **Full suite:** `.venv/bin/python -m pytest tests/ -q` gave **2567 passed**, 1 failed,
  1 deselected, of 2569 collected, in 3 min 14 s. Neither excluded test can run in this agent's OS
  sandbox, and CI runs both.
  - The failure is `test_the_shell_suite_is_documented_with_its_assertion_count`. It runs
    `tests/test_capacity_race_guard.sh`, which needs `mktemp` in the system temp dir (`4 passed, 6
    failed` inside the shell suite). It fails the same way on `main` at `0459fbc` here.
  - The deselected test is `TestDriver::test_a_stream_that_goes_quiet_at_the_wall_still_hands_off`.
    It binds 127.0.0.1, which this sandbox refuses.
- **SDLC gate:** `python3 .sdlc/scripts/sdlc_ci_gate.py --require-active --changed-files-from <diff
  vs f22ca65> --base-sha f22ca65…` reported `SDLC CI GATE PASSED`. The approval is bound to the base
  this change is built on.

**On `main` at `0459fbc` plus this change, 2026-11-21, before #196 to #200 merged:** 557 PASS lines,
0 FAIL, exit 0, in 8 min 07 s, and 2559 passed of 2561 with the same two sandbox-only exclusions.
