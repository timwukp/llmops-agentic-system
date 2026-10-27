# Intent: the console says what it is, LLMOps Governance

- **Slug:** llmops-governance-name
- **Author:** Claude (AI agent)
- **Date:** 2026-10-27
- **Accepted-by:** Tim WU
- **Status:** accepted

Accepted on the owner's chat decision of 2026-10-27: "LLMOps governance", after "為什麼要改" weighed
renaming against not.

## Problem

The console is titled "LLMOps Admin", which reads as a training back office. What it now does is govern
the run from raw data to a judged model: PII and fabricated-identifier rules, gates with intervals, the
provenance card on the model the approver opens. The owner's focus is data + AI governance.

## Outcome wanted

The console and its docs are titled **LLMOps Governance** (中文：LLMOps 治理). Display names only: the
repo, the `llmops-*` AWS resources and every API stay as they are. A descriptive name, chosen over a
coined one (Proofline): consistent with everything already named llmops, no trademark search needed
for internal and demo use, no overclaim; a distinct external brand can come later.

## Acceptance

- The page title, the sidebar brand, the intro page and the console docs say LLMOps Governance; the
  generated console diagram is regenerated, not edited.
- The name fits the sidebar beside its toggle.
