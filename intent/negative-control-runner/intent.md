# Intent: the negative-control runner verifies every pair it is credited with

- **Slug:** negative-control-runner
- **Author:** Claude (AI agent)
- **Date:** 2026-11-21
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's reply of 2026-11-21 (this session): "Accept intent".

Found while verifying PR #196 (llmops-retrieval network seal): on that branch the runner printed
555 PASS lines for 565 registered (guard, mutation) pairs. All 9 of #196's own pairs passed; the
other 10 are defects already on `main` @ `0459fbc`.

## Problem

`docs/TEST_RESULTS.md` says **556/556 negative controls** (565/565 on #196). That number is
derived by `tests/test_docs_claims.py` from the runner's `case(...)` registrations, not from what
the runner does with them. Two defects make the two differ:

1. **Cases registered below the runner loop never run.** The loop and its summary sit at line
   ~7274 of `tests/negative_controls/monitor_dispatch.py`. Batch 5's block (PR #195: 5 cases,
   7 pairs on `verify_sandbox.py` and `03_storage.py`) was appended after them, so those cases
   register after the loop has finished and the summary has printed. The runner says
   `all guards caught their break` and exits 0 while 7 pairs were never attempted. The count
   guard still counts them, because it reads the AST. Nothing checks that a registration comes
   before the loop.
2. **m315 cannot fail the way the runner requires.** It deletes
   `"model_package_approval_status": "PendingManualApproval"` from `DEFAULT_PARAMS`. Since
   `1d406ce` the handler reads `APPROVAL_STATUS_DEFAULT = DEFAULT_PARAMS["model_package_approval_status"]`
   at import, so the mutation now breaks the import (pytest exit 4), not the behaviour. The
   runner scores only exit 1 as a catch, so its 3 pairs report UNCAUGHT on every run.

So the documented "556/556" includes 7 pairs that never ran and 3 that cannot pass.

## Outcome wanted

- Running the runner prints one PASS line per registered pair and `all guards caught their break`.
- Registering a case below the runner loop fails the offline suite, so the count the docs derive
  from registrations stays equal to the number of pairs the runner runs.
- m315 still models its break (the prompt reads the approval status and nothing supplies one),
  and its 3 tests fail by their own assertions (exit 1), not by import error.
- The documented counts match. They are derived, so the docs move only if this change adds pairs.

## Acceptance

- `.venv/bin/python tests/negative_controls/monitor_dispatch.py` ends with
  `all guards caught their break`, and its PASS lines equal the pair count the docs state.
- The full suite passes, apart from the two tests this sandbox cannot run (a shell test that needs
  `mktemp` in the system temp dir, and one that binds 127.0.0.1). CI runs both.
- `docs/TEST_RESULTS.md` and `docs/TEST_RESULTS.zh-TW.md` agree with
  `tests/test_docs_claims.py`.

## Out of scope

- PR #196's own pairs. It is open against the same counts; whichever of the two merges second
  recomputes them.
- Making the runner part of CI. It mutates tracked files and takes minutes.
