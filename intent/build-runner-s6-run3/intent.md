# Intent: a build runner that survives a closed laptop, and S6 run 3

- **Slug:** build-runner-s6-run3
- **Author:** Claude (AI agent)
- **Date:** 2026-10-13
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

Spikes create VPCs, wait out AgentCore ENIs for hours and run long; the test suite has two tests the
laptop sandbox cannot run. The owner asked for a secure EC2 runner in this account so build and test
continue when the laptop is closed — long-task runner only, with a dedicated role scoped by the
`llmops-*`/spike tags and prefixes (2026-10-13). Spike S6 (VPC-mode harness) failed twice with a
120 s initialization timeout; run 3 was to find out why rather than guess.

## Desired outcome

A runner whose identity, privileges and inbound surface are asserted offline, guarded against
having its role replaced, and verified live; S6 run 3 diagnostics (DNS query logging, NAT, S3
gateway, PUBLIC and endpoint controls) with honest evidence.

## Acceptance

- Runner security properties pinned by tests; every spike AWS call granted by the runner policy.
- A job refuses to run, and reports, when the instance does not hold the runner role.
- Each S6 run 3 variant recorded in the evidence with its result; teardown leftovers recorded.
