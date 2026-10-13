# SPIKE_06_s6_vpc_harness — VPC-mode AgentCore harness without NAT

- **Verdict:** FAIL (7 VPC runs, 5 topologies; the identical configuration passes in PUBLIC mode)
- **Gates:** Phase 1 PR-03: VPC foundation
- **Region:** us-east-1
- **Ran:** 2026-10-13, runs 1–2 from a laptop, runs 3a–3e from the build runner (`tools/runner/`)

## Pass criteria (run 3a: the spike's own question, no NAT)

| id | criterion | result | detail |
|---|---|---|---|
| session | A session starts and the S3 skill sources resolve | **FAIL** | no parseable <<<REPORT>>> in the reply |
| model | The model call succeeds through the bedrock-runtime endpoint | **FAIL** | `RuntimeClientError: Runtime initialization time exceeded. Please make sure that initialization completes in 120s.` |
| tools | aws s3tables list-table-buckets, an Athena query and glue get-job succeed from the shell | **NOT RUN** | no session |
| egress | Which tools need egress is recorded | **NOT RUN** | no session |
| failures | What fails without NAT is recorded | **NOT RUN** | no session |

## Run history (2026-10-13)

Every VPC run used a fresh VPC (10.43.0.0/16) with subnets in use1-az1/az2/az4 — the AZ IDs AgentCore
supports — and a security group whose egress is the default allow-all. The harness clones the
data-prep harness: same model, the three S3 skill sources, the `llmops-harness-execution` role.
Runs 3a–3e add Route 53 Resolver query logging, so each records the hostnames the runtime resolved
between `InvokeHarness` and the failure.

| Run | Network | Interface endpoints | S3 | Memory | Harness | `InvokeHarness` |
|---|---|---|---|---|---|---|
| 1 | VPC, no NAT | 9 (bedrock-runtime, bedrock-agentcore, sts, logs, ssm, s3tables, glue, athena, secretsmanager) | gateway endpoint | managed (API default) | READY | init exceeded 120 s |
| 2 | VPC, no NAT | run 1 + ecr.api + ecr.dkr | gateway endpoint | managed | READY | init exceeded 120 s |
| 3a | VPC, no NAT | 11 | gateway endpoint | **disabled** | READY | init exceeded 120 s |
| 3b | VPC **+ NAT** | 11 | gateway endpoint | disabled | READY | init exceeded 120 s |
| 3c | VPC + NAT | 11 | **via NAT** (no gateway endpoint) | disabled | READY | init exceeded 120 s |
| 3d | **PUBLIC** (no VPC) | — | — | disabled | READY | empty stream, no error (see below) |
| 3d′ | **PUBLIC** (no VPC) | — | — | disabled | READY | **PASS, 5/5 criteria** |
| 3e | VPC + NAT | **none** (public DNS via NAT) | gateway endpoint | disabled | READY | **health check failed or timed out** |

Hostnames resolved during the failed initialization (count, all `NOERROR`):

| Run | `kepler-ola-us-east-1-prod-logs-bucket.s3…` | `time.aws.com` | `api.ecr…` | `<registry>.dkr.ecr…` | `logs…` |
|---|---|---|---|---|---|
| 3a | 39 | 9 | 2 | 1 | 2 |
| 3b | 39 | 1 | 2 | — | — |
| 3c | 48 | 1 | — | 1 | 1 |
| 3e | 60 | 7 | 5 | 1 | 6 |

The registry is **not this account's** (compared against the caller's account id offline; the
evidence redactor masks every 12-digit run, so it reads `<ACCOUNT_ID>` above): the runtime image comes
from a service-owned ECR repository. No run resolved `bedrock-agentcore`, `sts`, `bedrock-runtime`,
the data bucket the skills live in, or the ECR layer bucket (`prod-us-east-1-starport-layer-bucket`).

## What is established

- **The configuration is not the problem.** In PUBLIC mode the identical harness (same model, skills,
  role, memory disabled) initializes, lists all three S3 skills, streams model output and classifies
  all 11 probes (3d′). One earlier PUBLIC attempt (3d) returned an empty stream with no error member;
  it is recorded, not explained, and the rerun passed.
- **No network topology we control changes the outcome.** Rejected one at a time:
  * *missing ECR endpoints* — run 2 added them; identical failure;
  * *managed memory* — runs 3a–3e disabled it (`memory={"disabled": {}}`); identical failure;
  * *needs public egress* — 3b added a NAT; `time.aws.com` went from 9 lookups to 1 and
    initialization still exceeded 120 s;
  * *the S3 gateway endpoint path* — 3c sent S3 through the NAT instead; identical failure;
  * *the security group* — the AgentCore VPC guide states inbound is not required ("The runtime only
    initiates outbound connections"), and egress was allow-all in every run;
  * *our interface endpoints' private DNS* — 3e removed all of them; the error **changed** (from
    "initialization time exceeded" to "health check failed or timed out") but the runtime still never
    served a request.
- **Initialization stops before the runtime does any work of ours.** Every failed run resolved only
  the service's own dependencies — its ECR registry, a service-owned S3 log bucket (looked up 39–60
  times, which reads as retries), CloudWatch Logs and NTP — and never the hosts a session would use.
  The runtime's CloudWatch log group held no events in runs 1–2.
- **READY is not evidence of a working VPC-mode harness**, the same lesson as AGENTS.md's skill-source
  gotcha: `CreateHarness` accepted every VPC configuration and every harness reached READY.

## Consequences for Phase 1 (PR-03)

- **PR-03 stays gated.** Lakehouse tasks run on the PUBLIC data-prep harness, as 3d′ shows they can;
  Glue and Athena run in AWS-managed networks and are unaffected.
- **Next step is the service team, not another topology.** The evidence above (5 topologies, the
  PUBLIC control, the DNS profiles, the two error messages) is what to bring to the AgentCore team.
  If they cannot answer, the remaining instrument is VPC Flow Logs on the runtime's ENIs, to see
  whether the S3 log-bucket and ECR connections complete.
- **Phase 1's VPC must be a standing resource**, never torn down per run: AgentCore's ENIs persist up
  to 8 h after a harness is deleted, which held every spike VPC for hours and filled the account's
  VPC quota (15) by run 3c.
- **A harness created without `memory` now gets managed memory** (live, 2026-10-13: the runner's role
  was refused `CreateMemory` until the spike passed `memory={"disabled": {}}`). Tracked separately.

## Teardown

- Every billable resource of every run was deleted: interface and gateway endpoints, NAT gateways
  and their Elastic IPs, Resolver query-log configs and associations, log groups, harnesses.
- Left, by design and at no charge: one VPC per run that created a harness, with its three subnets and
  security group, held by AgentCore `agentic_ai` ENIs. Run 1's VPC was deleted by `cleanup` once its
  ENIs were released (about 8 h); the others follow the same way
  (`python3 tools/spikes/s6_vpc_harness.py cleanup`, which deletes what is free and reports the rest).
- Two early attempts at run 3a each left a Resolver query-log configuration and association behind
  (a teardown race, then a missing grant — both fixed in this change); both were deleted by hand. Two
  more attempts stopped on runner grants before creating a harness, and one left a route table
  (tag-authorization consistency, fixed); deleted by hand with its VPC.
