# Intent: the harness and console roles cannot forge events, rewrite instruments, or self-approve

- **Slug:** iam-scopedown
- **Author:** Claude (AI agent)
- **Date:** 2026-10-28
- **Accepted-by:** Tim WU
- **Status:** accepted

Security audit Batch 2a, the low-risk IAM scope-down (the per-agent role split and VPC move are
Batch 2b, which needs a usage inventory and canary). Accepted on the owner's 2026-10-28 decisions.

## Problem

The shared harness execution role could: `events:PutEvents` on the pipeline bus with no condition
(a hijacked agent forges `EscalatedToHuman` and drives a conductor triage); Get **and Put** on
`contracts/*`, `pipeline/contracts/*` and `reports/*` (it can rewrite `cost_model.py`, the gate
schema and checker it is judged by or executes, and the driver's canonical report); and
`CreateModelPackage` with no condition on `ModelApprovalStatus`, while `model_package_approval_status`
could be set from a webhook body, a `launch_run` param or a signed plan — registering a
human-approved model with no human. Separately, the internet-facing console role held
`UpdateHarness` on `*` (rewrite any harness's prompt, in any project), plus unused
`secretsmanager:GetSecretValue` on the admin login secret and `logs:PutRetentionPolicy` on `*`.

## Outcome wanted

- The harness role cannot emit bus events, cannot write the instruments/code it is measured by, and
  cannot register an Approved model.
- No input (params, webhook, or signed plan) can set the model approval status; the pipeline always
  registers PendingManualApproval. A human approves in the registry.
- The console can read/update only this project's harnesses, and loses the unused secret and
  retention-policy grants.

## Acceptance

- `tests/test_iam_scopedown.py` (new) and the existing IAM/finops tests pass; negative controls.
- Deploy-time: `simulate_principal_policy` confirms the removed actions now Deny and the kept ones
  still Allow, and that the NeverApprove condition key is honoured for CreateModelPackage.
