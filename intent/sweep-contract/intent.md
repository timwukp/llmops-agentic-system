# Intent: sweep.json has one shape, and the driver enforces it

- **Slug:** sweep-contract
- **Author:** Claude (AI agent)
- **Date:** 2026-10-17
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's request of 2026-10-17 (chat): "好，固定 sweep.json 的格式".

## Problem

The daily sweep's output is free-form, and it drifts every run. Four live files (2026-10-14, -15,
-16, and the 10-15 verification) name the same facts differently: `live_endpoints`/`endpoints`;
`total`/`total_live`/`live_count`; `orphaned_endpoint_configs`/`orphan_endpoint_configs`;
`timestamp_utc`/`generated_at`; `findings`/`open_findings`/`anomalies`/`tag_findings`;
`memory_sweep`/`agentcore`. The prompt named `memory_orphans` but not where it lives. A reader of
these files already misread one: a missing field was counted as 0 orphans.

## Desired outcome

A versioned contract (`pipeline/contracts/sweep.schema.json`), the prompt stating it, and the driver
rejecting a `stage_complete` whose sweep.json does not satisfy it, with the errors, so the agent
rewrites the file in the same session.

## Acceptance

- Offline: validator bites (rejects each observed drift variant), cross-field rules (counts match
  lists; an unmeasured count is null AND has a permission gap), the prompt names every required key
  (derived from the schema), the schema is vendored into the driver bundle.
- Live: deploy (driver bundle + monitor prompt) and one sweep through `llmops-monitor-sweep` that
  passes the contract, with the owner's go-ahead.
