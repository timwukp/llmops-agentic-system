# Phase 0 spikes

Each spike answers one question the AWS documentation could not settle, by creating real
resources, measuring, and deleting them again. They gate the lakehouse LLM-lifecycle build
(`docs/research/LAKEHOUSE_LLM_LIFECYCLE.md`, §10 and §13). Spikes are deploy-class scripts
(owner decision 2026-10-13): they call AWS directly. Pipeline work never does.

| Spike | Question | Gates | `run()` |
|---|---|---|---|
| `s1_flink_s3tables.py` | Managed Flink upserts to S3 Tables via Glue Iceberg REST, public and VPC | Phase 3 Flink | pending |
| `s2_vllm_multilora.py` | vLLM DLC base + adapter ICs: OpenAI path, Data Capture, metrics, swap/rollback, dtype gap | Phase 2 serving | pending |
| `s3_gateway_auth.py` | AgentCore Gateway vs API Gateway + Lambda in front of the OpenAI path | Phase 2 gateway | pending |
| `s4_pii_recall_zhtw.py` | zh-TW / EN PII recall: Glue Detect-PII vs Guardrails vs privacy-filter | Phase 1 PR-07 | **4/4 PASS (2026-10-13)**: regex + Guardrails — `deploy/evidence/SPIKE_04_s4_pii_recall_zhtw.md` |
| `s5_mlflow_registry.py` | MLflow App → Model Registry sync, aliases, approval event, condition-key deny | Phase 2 registry | pending |
| `s6_vpc_harness.py` | VPC-mode harness without NAT | Phase 1 PR-03 | **FAIL, 2 runs (2026-10-13)**: session init timeout; run 3 diagnostic pending — `deploy/evidence/SPIKE_06_s6_vpc_harness.md` |
| `s7_lakehouse_substrate.py` | Iceberg v2, Glue 5 IAM-mode writes, deterministic `UNLOAD … FOR VERSION AS OF`; Unified Studio (`--part domain`, follow-up) | Phase 1 PR-04/05 | **substrate: 7/7 PASS (2026-10-13)** — `deploy/evidence/SPIKE_07_s7_lakehouse_substrate.md`; domain part pending |

## Usage

```bash
python3 tools/spikes/s7_lakehouse_substrate.py plan --account-id <ACCOUNT_ID>   # offline
python3 tools/spikes/s7_lakehouse_substrate.py run                              # creates, measures, deletes
python3 tools/spikes/s7_lakehouse_substrate.py sweep                            # leftovers by tag
```

## Contract (enforced by `_spike.py`, tested in `tests/test_spikes.py`)

- **Criteria are data.** `PASS_CRITERIA` is declared before anything runs; the evidence file prints
  each one as PASS / FAIL / NOT RUN, and recording an undeclared criterion raises.
- **`plan()` is offline.** It lists every resource `run()` will create.
- **Creation and deletion are written together.** `track(kind, id, deleter)` is called at creation;
  `teardown()` replays the deleters newest first and keeps going past a stuck one.
- **Teardown always runs** (`finally`), unless `--keep` is passed for debugging.
- **Everything is tagged** `project=llmops-agentic-system`, `spike=<name>`; `sweep` finds leftovers
  from a run that died before teardown.
- **Evidence is redacted.** `deploy/evidence/SPIKE_0N_<name>.md` has registered values (e.g. the data
  bucket → `<DATA_BUCKET>`) replaced, every 12-digit run replaced by `<ACCOUNT_ID>`, and longer digit
  runs (Iceberg snapshot ids) grouped with separators; raw output goes to `*.local.json`, which
  `.gitignore` excludes.
- **Account-level settings are never changed silently.** If S7 finds the S3 Tables ↔ Glue Data
  Catalog integration missing, it records that and stops instead of enabling it.

Resource names that AWS will not let you reuse right after deletion (table buckets) carry a
`YYYYMMDD-HHMMSS` suffix per run.

Each spike's `run()` is implemented and exercised live in that spike's own PR, in critical-path
order: S7 → S6 → S4, then S1, S2, S3, S5.
