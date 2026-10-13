# Intent: record the lakehouse LLM-lifecycle research and the owner's decisions

- **Slug:** lakehouse-research
- **Author:** Claude (AI agent)
- **Date:** 2026-10-13
- **Accepted-by:** Tim WU
- **Status:** accepted

## Problem

The owner asked for an enterprise-grade, end-to-end, visualized LLM lifecycle on AWS (data → streaming/CDC →
Iceberg medallion → QLoRA → MLflow registry → vLLM → feedback loop) with governance. Six research sub-agents
produced findings that now drive a multi-phase build; they live only in an untracked file, which also makes the
repo's file census fail locally.

## Desired outcome

The research report (EN + zh-TW) is committed with the owner's decisions of 2026-10-13 recorded in §13, so every
following phase PR can cite it.

## Acceptance

- `docs/research/LAKEHOUSE_LLM_LIFECYCLE.md` and `.zh-TW.md` are tracked, structurally parallel, and contain no
  account identifiers.
- The census and test-count guards pass.
