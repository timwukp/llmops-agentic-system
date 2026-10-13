# SPIKE_06_s6_vpc_harness — VPC-mode AgentCore harness without NAT

- **Verdict:** FAIL
- **Gates:** Phase 1 PR-03: VPC foundation
- **Region:** us-east-1
- **Ran:** 2026-10-13T07:25:20Z → 2026-10-13T07:44:41Z

## Pass criteria

| id | criterion | result | detail |
|---|---|---|---|
| session | A session starts and the S3 skill sources resolve | **FAIL** | no parseable <<<REPORT>>> in the reply |
| model | The model call succeeds through the bedrock-runtime endpoint | **FAIL** | no text: RuntimeClientError: An error occurred (RuntimeClientError) when calling the InvokeHarness operation: Runtime initialization time exceeded. Please make sure that initialization completes in 120s. |
| tools | aws s3tables list-table-buckets, an Athena query and glue get-job succeed from the shell | **NOT RUN** |  |
| egress | Which tools need egress is recorded | **NOT RUN** |  |
| failures | What fails without NAT is recorded | **NOT RUN** |  |

## Observations

- **subnets:** {"use1-az1": "subnet-0d291e452aeddb625", "use1-az2": "subnet-04f717bb1567602fd", "use1-az4": "subnet-038bee6f430dcf4ed"}
- **harness_status:** READY
- **invoke_error:** RuntimeClientError: An error occurred (RuntimeClientError) when calling the InvokeHarness operation: Runtime initialization time exceeded. Please make sure that initialization completes in 120s.
- **raw_reply_tail:** 

## Errors

- teardown: security group sg-003b71004b35375f1: RuntimeError: still blocked after 4 attempts (AgentCore ENIs persist up to 8 h; run `cleanup` later): An error occurred (DependencyViolation) when calling the DeleteSecurityGroup operation: resource sg-003b71004b35375f1 has a dependent object
- teardown: subnet subnet-038bee6f430dcf4ed: RuntimeError: still blocked after 4 attempts (AgentCore ENIs persist up to 8 h; run `cleanup` later): An error occurred (DependencyViolation) when calling the DeleteSubnet operation: The subnet 'subnet-038bee6f430dcf4ed' has dependencies and cannot be deleted.
- teardown: subnet subnet-04f717bb1567602fd: RuntimeError: still blocked after 4 attempts (AgentCore ENIs persist up to 8 h; run `cleanup` later): An error occurred (DependencyViolation) when calling the DeleteSubnet operation: The subnet 'subnet-04f717bb1567602fd' has dependencies and cannot be deleted.
- teardown: subnet subnet-0d291e452aeddb625: RuntimeError: still blocked after 4 attempts (AgentCore ENIs persist up to 8 h; run `cleanup` later): An error occurred (DependencyViolation) when calling the DeleteSubnet operation: The subnet 'subnet-0d291e452aeddb625' has dependencies and cannot be deleted.
- teardown: vpc vpc-0374f7d0ad60f8180: RuntimeError: still blocked after 4 attempts (AgentCore ENIs persist up to 8 h; run `cleanup` later): An error occurred (DependencyViolation) when calling the DeleteVpc operation: The vpc 'vpc-0374f7d0ad60f8180' has dependencies and cannot be deleted.

## Teardown

- LEFT RUNNING (delete failed): security group sg-003b71004b35375f1 — RuntimeError: still blocked after 4 attempts (AgentCore ENIs persist up to 8 h; run `cleanup` later): An error occurred (DependencyViolation) when calling the DeleteSecurityGroup operation: resource s
- LEFT RUNNING (delete failed): subnet subnet-038bee6f430dcf4ed — RuntimeError: still blocked after 4 attempts (AgentCore ENIs persist up to 8 h; run `cleanup` later): An error occurred (DependencyViolation) when calling the DeleteSubnet operation: The subnet 'subne
- LEFT RUNNING (delete failed): subnet subnet-04f717bb1567602fd — RuntimeError: still blocked after 4 attempts (AgentCore ENIs persist up to 8 h; run `cleanup` later): An error occurred (DependencyViolation) when calling the DeleteSubnet operation: The subnet 'subne
- LEFT RUNNING (delete failed): subnet subnet-0d291e452aeddb625 — RuntimeError: still blocked after 4 attempts (AgentCore ENIs persist up to 8 h; run `cleanup` later): An error occurred (DependencyViolation) when calling the DeleteSubnet operation: The subnet 'subne
- LEFT RUNNING (delete failed): vpc vpc-0374f7d0ad60f8180 — RuntimeError: still blocked after 4 attempts (AgentCore ENIs persist up to 8 h; run `cleanup` later): An error occurred (DependencyViolation) when calling the DeleteVpc operation: The vpc 'vpc-0374f7d

## Run history (2026-10-13)

The table above is run 2. Both runs failed at the same point; the second run tested and rejected the
first hypothesis.

| Run | Started (UTC) | Endpoints | Outcome |
|---|---|---|---|
| 1 | 07:05:55 | S3 gateway + bedrock-runtime, bedrock-agentcore, sts, logs, ssm, s3tables, glue, athena, secretsmanager | VPC, 3 subnets (use1-az1/az2/az4) and all endpoints `available`; harness **READY**; `InvokeHarness` → **`Runtime initialization time exceeded. Please make sure that initialization completes in 120s`** |
| 2 | 07:25:20 | run 1 **+ ecr.api + ecr.dkr** (the AgentCore VPC docs list both as required without internet) | **Identical failure.** Hypothesis "ECR endpoints were missing" rejected. |

## What is established

- **A VPC-mode harness without NAT, with every endpoint the AgentCore VPC documentation lists, does not
  initialize a session within 120 s.** `CreateHarness` accepts the VPC configuration and the harness
  reaches READY — READY is no evidence of a working session (the same lesson as AGENTS.md's skill-source
  gotcha).
- **The runtime's CloudWatch log group exists but holds no log events** for either run: initialization
  failed before the agent process logged anything.
- **The service-managed S3 gateway no longer exists for runtimes created after the May 2026 rollout**
  (`requireServiceS3Endpoint` API documentation): all startup traffic, S3 included, goes through the
  customer VPC. This VPC had an S3 gateway endpoint (default policy) on its main route table, so S3 is
  not an obvious gap — but the runtime's other startup dependencies are not documented.
- **AgentCore's ENIs (`agentic_ai`) outlive the harness**, as documented (up to 8 h): both runs left their
  VPC, three subnets and security group behind (no charge). The billable interface endpoints were
  deleted in both runs. `python3 tools/spikes/s6_vpc_harness.py cleanup` deletes the leftovers once the
  ENIs are released.

## Consequences for Phase 1 (PR-03)

- **PR-03 is NOT unblocked.** Until a VPC-mode session initializes, lakehouse tasks stay on the PUBLIC
  data-prep harness; Glue jobs and Athena (which run in AWS-managed networks) are unaffected.
- **Phase 1's VPC must be a standing resource**, never torn down per run: ENI persistence makes a
  per-run VPC impossible to delete for hours.

## Next diagnostic (run 3)

Enable **Route 53 Resolver query logging** on the spike VPC, so the evidence names the hostnames the
runtime tries to resolve during initialization — and therefore which endpoint (if any) is missing —
instead of guessing. A positive control (the same harness in a VPC **with** a NAT gateway) separates
"an endpoint is missing" from "VPC-mode harnesses do not work without NAT at all".

