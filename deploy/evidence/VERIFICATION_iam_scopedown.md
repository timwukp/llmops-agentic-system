# Verification: IAM scope-down (Batch 2a)

Intent chain: `intent/iam-scopedown/`. Date: 2026-10-28. Base: `main` at `56bf1d9`.
Detail (private): `~/Downloads/llmops-security-audit/`.

## Changed

- **Harness role** (`deploy/iam/harness_execution_role.json`): removed `PutPipelineEvents`; narrowed
  `S3PipelineObjects` writes to runs/finops/monitoring/cost/models-mirror; added
  `S3InstrumentsReadOnly` (Get on contracts/*, pipeline/contracts/*, reports/*) and
  `S3DenyInstrumentWrites` (Deny Put/Delete on those + code/* + skills/*); added
  `NeverApproveAtRegistration` (Deny Create/UpdateModelPackage when ModelApprovalStatus=Approved).
  Deployed inline policy 9380 chars (limit 10,240).
- **Start-pipeline**: `_merge_params` forces `model_package_approval_status` to
  PendingManualApproval regardless of params or signed plan. This is the guarantee that does not
  depend on whether the IAM condition key is populated for CreateModelPackage at call time.
- **Console role** (`deploy/console/iam-policy.json`): `UpdateHarness`/`GetHarness` scoped to
  `harness/llmops_*` (was `*`); dropped the unused `Secrets` statement and `logs:PutRetentionPolicy`.

## Verified (offline)

- `tests/test_iam_scopedown.py` (10): no PutEvents on any Allow; instruments Get-only + backed by
  an explicit Deny; NeverApprove Deny present; no params/plan can register Approved; default is
  PendingManualApproval; console touches only llmops_* harnesses and lost the secret/retention grants.
- `tests/test_finops.py` updated (the readable-prefix union now includes S3InstrumentsReadOnly) and
  passes; the plan-displaces-defaults test excludes the now-pinned approval field.
- 5 negative controls, each caught. Full suite otherwise green (2 known sandbox-only failures).

## Deferred to Batch 2b (needs usage inventory + canary)

- SageMaker instance-type / MaxRuntime caps and ECR account pinning — an allowlist that is even
  slightly wrong denies a legitimate training job at run time, so it belongs with the per-agent
  split and the canary runs, not in this can't-break-a-run batch.
- Bedrock `InvokeModel` scoping — the judge uses a global inference profile; scoping it to one ARN
  risks breaking the judge.

## Not yet verified live (deploy-time)

- `simulate_principal_policy` on the live harness role: the removed actions (events:PutEvents,
  PutObject on contracts/reports, CreateModelPackage with ModelApprovalStatus=Approved) must return
  Deny, and the actions agents legitimately use must still Allow. The NeverApprove condition key's
  runtime efficacy for CreateModelPackage is confirmed here; start-pipeline's force is the backstop.
