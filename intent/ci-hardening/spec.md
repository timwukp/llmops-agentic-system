# Spec: CI / supply-chain hardening (Batch 4)

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **Scanner** (`tests/redaction_scan.py`): `HIGH_SIGNAL` gains ECR-registry-account-id, GitHub,
   Hugging Face, Anthropic, Slack and private-key rules, and the aws-secret rule requires a 40-char
   value (so the bare phrase in prose no longer fires); the staged selection uses `--diff-filter=ACMR`.
   `hooks/pre-commit` matches (`ACMR`).
2. **Push path** (`tools/push_via_api.py`): `_preflight_redaction_scan()` runs the tracked scan and
   `main` fails closed on a non-zero result before any blob/ref API call (skipped under `--dry-run`).
3. **Workflows**: `run-pipeline.yml` passes the three inputs through `env:` and validates the two
   counts as integers; `ci.yml` and `redaction-check.yml` declare `permissions: contents: read`.
4. **Guards**: `tests/test_ci_hardening.py` (fixtures built by concatenation so the file carries no
   real secret shape and needs no exemption); negative controls.
