# Verification: CI / supply-chain hardening (Batch 4)

Intent chain: `intent/ci-hardening/`. Date: 2026-10-30. Base: `main` at `5c688ea`.

## Changed

- **`tests/redaction_scan.py`**: `HIGH_SIGNAL` gains rules for ECR-registry account ids
  (`[0-9]{12}\.dkr\.ecr\.`, the leak the bare 12-digit rule skips), GitHub / Hugging Face /
  Anthropic / Slack tokens and private-key blocks; the aws-secret rule now requires a 40-char value
  so the bare phrase in prose no longer fires. The staged selection uses `--diff-filter=ACMR`
  (catches renamed-and-edited files). `hooks/pre-commit` matches.
- **`tools/push_via_api.py`**: `_preflight_redaction_scan()` runs the tracked scan, and `main` fails
  closed on a finding before any blob/ref API call. This is the documented push path (commits are
  hook-blocked), so it was the one path that reached the public remote unscanned.
- **`.github/workflows/run-pipeline.yml`**: the three inputs pass through `env:` and the counts are
  validated as integers, so a `note` can no longer break out of the `run:` script in the step that
  holds the assumed AWS role. **`ci.yml`** and **`redaction-check.yml`** declare
  `permissions: contents: read`.

## Verified (offline)

- `tests/test_ci_hardening.py` (14) — every fixture built by concatenation so the file carries no
  real secret shape and needs no exemption; the push guard tested functionally (a finding stops the
  push before git/network). Existing redaction suite updated and passing. 5 negative controls, each
  caught.

## Deferred (noted in the intent)

- Pinning actions to commit SHAs (Dependabot is the durable answer), whole-commit-range CI scanning
  (GitHub push-protection/secret-scanning is the better control — a repo-settings action for the
  owner), hashed dependency lock files, `torch.load(weights_only=True)`, running the SDLC gate from
  the base ref, and the Kiro installer checksum.
