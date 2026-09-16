# Plan: eleven new files, two censuses refreshed

- **Spec:** ./spec.md
- **Author:** Claude (AI agent)
- **Accepted-by:** Tim WU
- **Accepted-for:** 5eb7e6f36fe1ea31a1f76153ee64f22ae88b5132
- **Status:** accepted

`Accepted-for` is `git merge-base origin/main HEAD` for the installation PR, recorded
at acceptance. The local working tree is 10 commits behind that base with 7 locally
modified files; the PR is cut from origin/main directly, and every path below is new, so
the two states cannot conflict.

## Amendment 1 — two existing files, forced by the census guards (re-accepted)

CI refused the original plan: `tests/redaction_scan.py` and `tests/test_redaction_scan.py`
carry derived counts of tracked files ("201"), and eleven new files falsify them. Both are
updated to 212 per the spec's Amendment 1. Verified in a clone of this branch:
`python3.12 -m pytest tests/test_redaction_scan.py -q` → 49 passed.

## Files changed (all new)

1. `.sdlc/scripts/sdlc_pretooluse_hook.py` — vendored from the skill
2. `.sdlc/scripts/sdlc_gate.py` — vendored
3. `.sdlc/scripts/sdlc_ci_gate.py` — vendored
4. `.kiro/hooks/sdlc-gate.json` — Kiro surface
5. `.claude/settings.json` — Claude Code surface (repo had none; no merge needed)
6. `.github/workflows/sdlc-gate.yml` — CI gate, Monitor mode (not required)
7. `.sdlc/active` — `govern-this-repo`
8. `.sdlc/version` — artifact schema 1
9-11. `intent/govern-this-repo/{intent,spec,plan}.md` — this chain

## Files changed (existing, Amendment 1)

12. `tests/redaction_scan.py` — census comment 201 → 212; latest-movement sentence added
13. `tests/test_redaction_scan.py` — two count claims 201 → 212 (binary count 35 unchanged)

## Verification

- Both hook configs' commands run in-place against this repo with surface-shaped stdin
  events: accepted chain exits 0 on both; a draft-chain fixture exits 2 on both.
- `sdlc_ci_gate.py --repo . --require-active` over the installation file list passes.
- The skill's own suites already cover the scripts byte-for-byte (vendored unmodified).
