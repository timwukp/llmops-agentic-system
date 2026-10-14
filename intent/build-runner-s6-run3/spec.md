# Spec: build runner + S6 run 3

- **Intent:** ./intent.md
- **Author:** Claude (AI agent)
- **Signed-off-by:** Tim WU
- **Accepted-by:** Tim WU
- **Status:** signed-off

## Requirements

1. `tools/runner/runner.py`: plan / up / doctor / push / exec / fetch / down. Private subnet + NAT, no
   ingress, no key, no public IP, IMDSv2, encrypted root; SSM-only access; idempotent by tag; SSM
   `/llmops/runner/instance_id`.
2. Role: `AmazonSSMManagedInstanceCore` + inline policy scoped by tag / name prefix; no `*FullAccess`;
   `iam:CreateRole` only with the `llmops-spike-boundary` boundary, which holds no IAM actions.
   Create-time AgentCore actions that authorize against `*` are scoped by `aws:RequestTag/project`.
3. Identity guards: `doctor` (profile + any `AWS-AttachIAMToInstance` ForceReplace association),
   `exec` refuses on either, `up` repairs the profile; a job exits 5 under another identity and 6 if
   the identity changes mid-job; the SSM command returns the job's exit code.
4. Jobs: tree tarball with a rebuilt git index, re-asserted pins (incl. numpy), boundary ARN exported.
5. Spikes: `boundary_kwargs()` on every role they create. S6: Resolver query logging (waits for
   ACTIVE; never disassociates mid-transition), tagged private route table, consistency retry for
   fresh resources, memory disabled, variants NAT / no S3 gateway / PUBLIC / no interface endpoints,
   stream error members recorded, `cleanup` that skips held VPCs.
6. Tests: runner posture, identity guards, a static guard that every spike AWS call is granted.
