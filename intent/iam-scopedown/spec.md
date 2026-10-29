# Spec: IAM scope-down (Batch 2a)

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. **Harness role** (`deploy/iam/harness_execution_role.json`): remove `PutPipelineEvents`; narrow
   `S3PipelineObjects` write set to runs/finops/monitoring/cost/models-mirror; add
   `S3InstrumentsReadOnly` (Get on contracts/*, pipeline/contracts/*, reports/*) and
   `S3DenyInstrumentWrites` (Deny Put/Delete on those plus code/*, skills/*); add
   `NeverApproveAtRegistration` (Deny Create/UpdateModelPackage when ModelApprovalStatus=Approved).
   Keep the deployed inline policy under the 10,240-char limit.
2. **Start-pipeline** (`orchestration/start_pipeline/handler.py`): `_merge_params` forces
   `model_package_approval_status` to the default regardless of params or plan; `APPROVAL_STATUS_DEFAULT`
   names it. This is the guarantee independent of the IAM condition key.
3. **Console role** (`deploy/console/iam-policy.json`): split `AgentCoreControl` into `AgentCoreList`
   (List on *) and `AgentCoreOwnHarness` (Get/Update on harness/llmops_*); drop the `Secrets`
   statement and `logs:PutRetentionPolicy`.
4. **Deferred to 2b** (needs usage inventory + canary): SageMaker instance-type / MaxRuntime caps,
   ECR account pinning, Bedrock `InvokeModel` scoping (the judge uses a global inference profile).
5. **Guards:** `tests/test_iam_scopedown.py`; the finops prefix tests updated to union the new
   read-only statement; the plan-displaces-defaults test excludes the now-pinned approval field.
   Negative controls.
