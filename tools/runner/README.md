# Build runner

A dedicated EC2 instance in this account for **long build/test jobs** — spikes that create a VPC
and wait out ENIs, the full test suite — so they keep running when the laptop that started them is
closed (owner decision 2026-10-13: long-task runner only). Pipeline work is not run here; it goes
through the state machine and the harnesses, as always.

```bash
python3 tools/runner/runner.py plan        # offline: what `up` builds
python3 tools/runner/runner.py up          # idempotent: VPC, NAT, role, boundary, instance; repairs the profile
python3 tools/runner/runner.py doctor      # read-only: anything that would make a job run as the wrong identity
T=$(python3 tools/runner/runner.py push)   # tar the working tree to s3://<DATA_BUCKET>/runner/trees/
python3 tools/runner/runner.py exec --tree "$T" --cmd "python -m pytest -q tests"
python3 tools/runner/runner.py fetch --command-id <id>   # status + output tail
python3 tools/runner/runner.py down        # deletes everything `up` created
```

Results (`job.log`, any `deploy/evidence/SPIKE_*`) land in `s3://<DATA_BUCKET>/runner/results/<run_id>/`.

## Security posture (asserted offline in `tests/test_runner.py`)

- **No inbound path.** Security group with zero ingress rules, no public IP, no key pair; access is
  SSM only. IMDSv2 required, hop limit 1. Encrypted gp3 root volume.
- **Scoped role, no `*FullAccess`.** `llmops-build-runner-role` = `AmazonSSMManagedInstanceCore` + an
  inline policy scoped by the `project=llmops-agentic-system` tag and the `llmops-spike-*` /
  `llmops_spike_*` name prefixes. Every EC2 mutation is tag- or creation-scoped.
- **Roles it creates are capped.** `iam:CreateRole` / `PutRolePolicy` on `role/llmops-spike-*` require
  the permissions boundary `llmops-spike-boundary`, which holds no IAM actions. Jobs receive it as
  `LLMOPS_SPIKE_BOUNDARY_ARN` (`tools/spikes/_spike.py: boundary_kwargs`).
- **Every AWS call a spike makes is granted.** A test derives the calls from each spike's source and
  checks the policy allows each action. It checks actions only, not resources or conditions: the
  CreateHarness family below was found by live probe, not by this test.

## Identity guards

Live on 2026-10-13 the account's legacy SSM Quick Setup association (`AWS-AttachIAMToInstance`,
`InstanceIds=*`, `ForceReplace=true`, hourly) replaced this instance's role with
`AmazonSSMRoleForInstancesQuickSetup` at :01 — and six other instances' every hour. The owner set it to
`ForceReplace=false` (association v3; v1 in 2020 had been `false`). The runner no longer depends on that:

- `exec` runs `doctor` first and refuses if the instance profile is not `llmops-build-runner-profile` or
  if any association can replace it (`--accept-identity-risk` overrides only the latter, for a job
  shorter than the schedule). `up` restores the profile on this instance only.
- A job exits **5** before touching anything if it is not running as `llmops-build-runner-role`.
- A watchdog checks the identity every 30 s for the whole job; a change is recorded in `identity.log`,
  appended to `job.log`, and the job exits **6** whatever the command returned.
- The SSM command exits with the job's own code, so SSM status means what it says (it used to report
  Success for `exit=1`).

## Grants found live, one at a time

Each of these failed on the runner first and was granted only after the failure named it:
`logs:DescribeLogGroups` at account level and `iam:CreateServiceLinkedRole` for
`route53resolver.amazonaws.com` (Resolver query logging); `route53resolver:GetResolverQueryLogConfigAssociation`;
and `CreateHarness`, `CreateAgentRuntime`, `CreateAgentRuntimeEndpoint`, `CreateWorkloadIdentity` and
`TagResource`, which authorize against `harness/*`, `runtime/*` and the workload-identity directory
before any name exists — so they are scoped by `aws:RequestTag/project` instead of by name.
