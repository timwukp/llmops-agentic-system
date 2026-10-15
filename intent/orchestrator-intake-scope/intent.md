# Intent: the orchestrator's prompt states the data scope the platform enforces

- **Slug:** orchestrator-intake-scope
- **Author:** Claude (AI agent)
- **Date:** 2026-10-15
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's request of 2026-10-15 (chat): "另開一個 PR 修 orchestrator prompt", the follow-up
named in PR #163.

## Problem

Two sentences in the orchestrator's consult prompt describe a platform that does not exist:
- Step 0: "If their data already sits elsewhere in S3, ask for that exact URI instead." Data-prep's prompt
  refuses any `source_uri` outside the data bucket's `customer-data/`, and no stage can read another
  bucket. So the orchestrator collects URIs the next stage is told to refuse. This was wrong before #163.
- Intake: "the console checked those claims with its own broader IAM". Since #163 the console checks only
  `customer-data/` and `runs/`, and reports anything else as `unverified` with "not checked: outside …".
  The prompt does not say what that means. An agent can read it as "the file is missing", or as a
  failure that warrants another try.

## Desired outcome

The prompt says where data can be used in place, sends everything else to the drop zone, and tells the
agent that a "not checked" fact was not examined.

## Acceptance

- An offline guard derives the prefixes and the "not checked" wording from their sources: the console's
  `_INTAKE_S3_PREFIXES`, the f-string `_verify_s3_uri` emits, and data-prep's own prompt.
- The live orchestrator carries the new prompt. A live consult turn, given an out-of-scope URI, asks
  for the drop zone and does not ask for the URI.
