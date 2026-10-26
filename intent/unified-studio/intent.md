# Intent: sign in to SageMaker Unified Studio and see the lakehouse, without moving the pipeline

- **Slug:** unified-studio
- **Author:** Claude (AI agent)
- **Date:** 2026-10-24
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's chat decisions of 2026-10-24: "好，開始 Unified Studio 檢查", then "New llmops
domain (Recommended)", and, after the Lake Formation finding below, "IAM role only (Recommended)".

## Problem

The owner's end-to-end vision ends in "a login to SageMaker Unified Studio" with data governance.
The account has only an unrelated classic domain (`demo`). The lakehouse's S3 Tables catalog is in
IAM mode, and the approved first plan (register it with Lake Formation "keeping IAM access") turned
out not to exist: AWS's migration page says enabling Lake Formation access control "will revoke all
existing IAM-based access to your S3 Tables resources", at the register step, and hybrid access mode
is not offered for S3 Tables. Every pipeline principal reads the tables through IAM.

## Outcome wanted

- A person signs in to an IAM-based Unified Studio domain and can browse and query the lakehouse:
  read Silver and Gold; see Bronze's tables and columns but not its rows (raw PII).
- The pipeline's access is unchanged: the catalog stays in IAM mode and nothing is registered with
  Lake Formation.
- Nothing a Studio user does can change what the pipeline reads, or reach a pipeline role.

## Acceptance

- Offline guards and negative controls.
- `deploy/11_studio.py --verify` passes as the role itself (simulate_principal_policy, the required
  managed policy included), including the pipeline-unchanged checks and the domain.
- In the live domain, a Silver query returns rows and a Bronze row query is refused.
- A lakehouse run through the pipeline after the domain exists still succeeds.
