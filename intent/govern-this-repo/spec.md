# Spec: tier 2, Monitor mode, nothing invasive

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. Write-time hook installed for both surfaces from the skill's shipped templates
   (`.kiro/hooks/sdlc-gate.json`, `.claude/settings.json`), gate scripts vendored into
   `.sdlc/scripts/` so the repo-local copy wins the resolver.
2. CI gate (`.github/workflows/sdlc-gate.yml`) runs on every pull request; it is NOT
   marked required in branch protection during the observation window.
3. `.sdlc/active` names this chain, which is fully accepted, so the Build gate is open
   on day one — existing in-flight work is not retroactively refused.
4. No existing file is modified: every installed path is new. The uncommitted local work
   (7 modified files) is untouched.
5. The installed Claude Code hook command is exercised in-place with a Claude Code-shaped
   event before the change ships: accepted chain → exit 0; a draft chain → exit 2.

## Out of scope

Branch protection changes; pulling the local tree up to origin/main; onboarding any other
repository before the observation window ends.
