# Research — Enterprise LLM lifecycle on AWS: lakehouse → QLoRA → registry → vLLM → feedback

**中文版：[LAKEHOUSE_LLM_LIFECYCLE.zh-TW.md](LAKEHOUSE_LLM_LIFECYCLE.zh-TW.md)**

Status: **research report, draft for the owner's decision** — nothing here is built or deployed.
Date: 2026-10-13. Scope: **stack 1 = AWS services** (AWS-managed open source + AWS-native).
Stack 2 (Databricks) and stack 3 (fully open-source, self-managed) are deferred, but every
component below names its stack-3 equivalent so the interfaces we choose now carry over.

Method: six research sub-agents ran in parallel (reference use case; S3 Tables + SageMaker
Unified Studio; MSK + Managed Flink; training + MLflow + vLLM; enterprise governance +
visualization; mapping of this repo). They read official AWS docs, What's New, the AWS Price
List API, and Apache project docs, dated 2026-10-12/13. The three most load-bearing claims were
re-checked first-hand (§11). The analysis was red-teamed with the critical-thinking method:
assumptions, adversarial scenarios, evidence grading (§9).

Tags used throughout: **[managed-OSS]** = AWS-managed open-source engine or open format;
**[AWS-native]** = AWS proprietary service; **S3→** = stack-3 equivalent.

---

## 1. Executive summary

**Goal.** An enterprise-grade, end-to-end, *visualized* service-pipeline platform from customer
data to inference on enterprise open-source models, with governance — following the owner's
architecture (PDF): data sources → streaming/CDC → lakehouse Bronze/Silver/Gold → QLoRA training
→ MLflow registry (base + adapter) → vLLM serving → client apps, with logs and user feedback
flowing back into Bronze.

**Recommendation in one paragraph.** Build it as an extension of the existing platform, not a new
one: keep Step Functions + the AgentCore harnesses as the spine and the console as the single
pane. Put the lakehouse on **S3 Tables (Iceberg v2)**; ingest with **MSK** (+ **MSK Connect /
Debezium** for CRM/ERP CDC); do Bronze→Silver→Gold as **batch Glue/Athena jobs driven by the
pipeline**, with **Managed Flink** added only after a prototype proves its S3 Tables path; feed
training from a **frozen, hashed export** of a pinned Gold snapshot; register in an **MLflow App**
(source of truth) synced to the SageMaker Model Registry, and make **approval gate deployment**;
serve with the **SageMaker vLLM DLC + LoRA adapter inference components** behind the native
**OpenAI-compatible API**; log inference asynchronously to Kafka → Iceberg for the flywheel; add
**SageMaker Unified Studio** as the governance/visual layer (catalog, lineage, notebooks), fed by
OpenLineage events from the driver — not as the orchestrator.

**What changed my mind during research (the five facts that shape the design):**

| # | Fact (verified) | Consequence |
|---|---|---|
| 1 | S3 Tables expires snapshots after **120 h by default**; an Iceberg **tag or branch makes snapshot management fail for the whole table** | A training run cannot rely on time-travel to its snapshot later. Freeze a hashed export per run. |
| 2 | SageMaker training jobs read only S3/EFS/FSx — **no Iceberg input channel** | Same answer: export the pinned snapshot (Athena `UNLOAD … FOR VERSION AS OF`). Keeps the current trainer. |
| 3 | The native **OpenAI-compatible endpoint path supports the vLLM DLC / SGLang DLC / custom — not LMI** | Switch serving from LMI to the SageMaker vLLM DLC (still vLLM, still managed-OSS). |
| 4 | **Model Monitor and Clarify are closed to new customers** (since 2026-07-30) | Monitoring = Data Capture/app logs + MLflow + Evidently + Athena; evaluation = `mlflow.genai.evaluate` + Bedrock judge. |
| 5 | Today the deploy agent **deploys first and registers after**; approval does not gate the endpoint | Invert it: register → approve → *approval event triggers* deploy. This is the core governance fix. |

**Demo cost (part-time, torn down between sessions): roughly $150–400/month plus ~$5–15 per
pipeline run.** Production-shaped streaming alone is ~$4.5–5.5k/month (§8).

---

## 2. Use case and simulated data (designed fresh)

The owner's reference repo (`databrick-sagamker-full-stack-self-learning`) turned out to be
**retail demand forecasting on two stacks**, with hand-authored sample rows and no LLM, QLoRA,
vLLM, conversations or CRM data. Its *patterns* are reusable — planted data defects, per-step
`schema.json` contracts, a cross-step reconciliation check, an interactive per-stage pipeline
diagram — but **its data is not**. So the LLM use case is designed here.

**Proposed scenario — "SEA retail customer-service copilot"** (deliberately the same fictional
retailer as the reference, so the two self-learning sites connect):

| Source | Simulated as | Lands in Bronze as |
|---|---|---|
| Customer-service conversation history (chat/email) | LLM-generated multi-turn conversations (zh-TW + EN), via the existing Bedrock teacher, with personas, intents (order status, returns, stock, promo) and outcomes | `bronze.conversation_events` (one row per turn) |
| CRM (customers, tickets, CSAT) | A small Aurora/RDS PostgreSQL schema mutated by a generator (insert/update/delete) | CDC rows via Debezium → `bronze.crm_cdc` |
| ERP (orders, inventory) | Same DB; reuse the reference's SAP-style shapes (`MARD`-like stock, orders) | `bronze.erp_cdc` |
| Business system / web logs | Lambda/ECS Kafka producer replaying JSONL | `bronze.web_events` |
| Inference logs + user feedback (flywheel) | Written by the serving gateway | `bronze.inference_logs`, `bronze.feedback` |

**Planted defects** (borrowed idea, new content): PII in free text (names, phones, emails, card
fragments, Taiwanese ID formats), duplicate resends, out-of-order turns, schema drift (a new
field mid-stream), toxic/abusive turns, leaked `<think>` traces, near-duplicate conversations,
eval-set contamination. Each defect has a Silver/Gold rule that must catch it and a test that
proves the rule catches it.

**Gold contract** (owner's schema): `instruction, input, output` + `split` (train/eval) +
`pair_id` + provenance. The trainer today consumes chat `messages` JSONL
(`pipeline/training/distill/train_qlora.py`), so the export step maps
`instruction/input/output → messages` — a pure function with its own test.

---

## 3. Target architecture — stack 1 (AWS)

```
 Sources (simulated)            Ingest                     Lakehouse: S3 Tables (Iceberg v2)
 ─────────────────            ──────────                 ───────────────────────────────────
 CS conversations ──┐        MSK (Kafka)  ─────────────► Bronze  raw turns / CDC / logs
 Web/business logs ─┼─────►  + Glue Schema Registry      │  Glue Detect-PII · dedupe · DQDL
 CRM/ERP DB (CDC) ──┘        MSK Connect + Debezium      ▼
                             [phase 3: Managed Flink]    Silver  instruction pairs (MERGE on pair_id)
                                                         │  quality filter · balance · decontaminate
                                                         ▼
                                                         Gold    qlora_train / qlora_eval + dataset_manifest
                                                         │
                     ┌──── Athena UNLOAD … FOR VERSION AS OF <snapshot> → frozen JSONL + sha256 (S3)
                     ▼
  QLoRA training job (PEFT+bnb nf4) ─► MLflow App (runs, eval, registry = source of truth)
                     │                     │ sync
                     │                     ▼
                     │               SageMaker Model Registry  ── PendingManualApproval
                     │                     │  human KMS-signed approval → Approved
                     │                     ▼ (EventBridge: package approved)
                     └────────────►  Deploy: vLLM DLC base inference component
                                       + one LoRA adapter IC per approved version
                                           │  /openai/v1/chat/completions
                                           ▼
  Clients (agents, CS app) ─► Gateway (authN/Z, tenant quotas, Guardrails, PII-redacting logger)
                                           │ async
                                           └──► MSK topic inference.logs ─► Bronze (flywheel)

 Governance & visualization across all layers:
   Step Functions + harness driver ─ OpenLineage events ─► SageMaker Unified Studio (catalog, lineage)
   Console (single pane): Pipeline · Data · Models · Inference · Lineage · Cost
```

### Component table (tag · why · stack-3 equivalent)

| Layer | Component | Tag | Why this one | S3→ |
|---|---|---|---|---|
| Ingest | **Amazon MSK** (Provisioned Standard for demo; Express for prod/streaming tables) | managed-OSS | Kafka is the open contract; replay, many consumers | Kafka on Strimzi |
| CDC | **MSK Connect + Debezium** | managed-OSS | Only open path with before/after images; Firehose CDC was removed 2025-09-24; Aurora zero-ETL lands in Redshift storage, not S3 Tables | Strimzi KafkaConnect + Debezium |
| Schema | Glue Schema Registry | AWS-native (free) | Required by MSK streaming tables; contract for producers | Apicurio / Karapace |
| Bronze landing | **MSK streaming tables** *or* Iceberg Kafka Connect sink *or* Firehose | managed-OSS edge / AWS-native | See §4.2 — trade-off between cost floor and openness | Kafka Connect Iceberg sink |
| Lakehouse | **S3 Tables, Iceberg v2** | managed-OSS (open format) | Managed compaction + snapshots; Glue/Athena/Spark/PyIceberg all read it | Iceberg + Polaris/Lakekeeper on S3/MinIO |
| Transform | **Glue 5 Spark / Athena MERGE**, Glue Data Quality (DQDL) | managed-OSS engine | Batch is what training needs; auto-lineage to Unified Studio from Glue 5 | Spark + Great Expectations |
| PII | Glue Detect-PII (+ Bedrock Guardrails ANONYMIZE for zh-TW) | AWS-native | Comprehend PII is EN/ES only — insufficient for zh-TW | Presidio |
| Stream processing | **Managed Flink** (phase 3, after prototype) | managed-OSS | Streaming PII redaction, sessionization, CDC upserts | Flink K8s operator |
| Training | SageMaker training job running the **existing** `train_qlora.py` | AWS-native compute + OSS libs | Only option that controls bnb 4-bit; already built and proven | torchrun / Ray Train on K8s |
| Tracking/registry | **MLflow App** (serverless, no added charge) → sync to Model Registry | managed-OSS → AWS-native mirror | MLflow is the portable source of truth; Registry gives approvals + model cards + IAM condition keys | Self-hosted MLflow |
| Serving | **SageMaker vLLM DLC** + base IC + **adapter ICs** | managed-OSS engine on AWS-native hosting | Native OpenAI path; swap adapters without redeploying the base | vLLM on KServe / llm-d |
| Gateway | AgentCore Gateway inference target *or* API Gateway + Lambda | AWS-native, open protocols | Tenant JWT, TPM limits, Cedar policy + Guardrails | LiteLLM / Envoy AI Gateway |
| Guardrails | Bedrock `ApplyGuardrail` (explicit call) | AWS-native | Org guardrail enforcement covers only Bedrock calls, not self-hosted endpoints | NeMo Guardrails / Llama Guard |
| Evaluation | `mlflow.genai.evaluate` + Bedrock judge (BYOI) | managed-OSS + AWS-native | Clarify is closed to new customers | lm-eval-harness |
| Monitoring | App logs + Data Capture → Iceberg → Athena; Evidently; CloudWatch | managed-OSS | Model Monitor closed to new customers | Evidently + Prometheus |
| Governance | **SageMaker Unified Studio** (IAM domain), Lake Formation (when FGAC needed) | AWS-native | Catalog, glossary, subscriptions, lineage graph, notebooks | OpenMetadata/DataHub + Marquez + Ranger |
| Lineage | OpenLineage events → DataZone `PostLineageEvent` | open standard | No native model/endpoint lineage nodes — we emit them | Marquez |

---

## 4. Layer-by-layer design

### 4.1 Data sources and generator
A new deploy script (`deploy/10_lakehouse.py`, idempotent, `--dry-run`, SSM `/llmops/lakehouse/*`)
plus a generator package (`pipeline/lakehouse/generate/`) that produces the five sources with
seeded randomness, so every demo run is reproducible and every planted defect is countable.
Conversations are produced by the existing Bedrock teacher path, so no new model dependency.

### 4.2 Streaming and CDC — three landing options, one recommendation
| Option | Monthly floor (demo) | Open? | Limits |
|---|---|---|---|
| A. MSK Standard (2× t3.small) + **Iceberg Kafka Connect sink** for Bronze | ≈ $150 (MSK ≈ $70 + 1 MCU ≈ $80) | Fully OSS | Append only (fine for Bronze); exactly-once; schema evolution option |
| B. MSK **Express** + **streaming tables** | ≈ $900 (3-broker floor) | Kafka-native, delivery proprietary | Read-only table, no schema evolution/backfill/upsert, JSON + GSR only, same account/Region |
| C. Firehose (from producers or MSK) | ≈ $20–60 | AWS-native | Iceberg 1.5.2, exact schema match, extra fields dropped, one writer per table |

**Recommendation: A for the demo, B for production-scale Bronze.** CDC: Debezium (1 task — the
MySQL connector is single-task) → MSK → Bronze append; Silver applies upserts as a batch
`MERGE INTO` keyed on primary keys. **MSK Provisioned cannot be paused** — "demo mode" means a
`--teardown` that deletes the cluster and connector; with 8 h × 22 days the streaming tier costs
≈ $75/month.

### 4.3 Lakehouse medallion on S3 Tables
Two table buckets: `llmops-raw` (Bronze; PII present; CMK; short retention) and
`llmops-curated` (Silver/Gold; de-identified). All names lowercase (Lake Formation/Glue/Athena
otherwise cannot see them). **Stay on Iceberg v2** — Athena cannot read v3, the upgrade is
one-way, Firehose bundles Iceberg 1.5.2.

| Table | Key columns | Written by |
|---|---|---|
| `bronze.conversation_events` | event_id, conversation_id, turn_idx, role, content, raw_meta (JSON string), ingest_ts — `day(ingest_ts)` | Kafka sink |
| `bronze.crm_cdc` / `erp_cdc` | Debezium envelope (op, before, after, ts_ms, source) | Kafka sink |
| `bronze.inference_logs` / `feedback` | request_id (join key = `InferenceId`), adapter_ic, model_version, redacted prompt/response, tokens, latency, rating | gateway → Kafka |
| `silver.instruction_pairs` | pair_id (deterministic hash → idempotent MERGE), instruction, input, output, lang, source_ref, pii_status, redaction_method, dq_run_id | Glue Spark |
| `gold.qlora_train` / `gold.qlora_eval` | instruction, input, output, split, pair_id | Glue/Athena |
| `gold.dataset_manifest` | dataset_version, table, **snapshot_id**, export_uri, **sha256**, row_count, dq_score, pii_report_uri, run_id | export step |

Gates: Glue DQ rules on Silver/Gold (`IsComplete instruction/output`, `Uniqueness pair_id = 1.0`,
`RowCount > N`, no `<think>` in output). DQDL cannot evaluate list columns — flatten `pii_entities`.

### 4.4 From Gold to training (the reproducibility hinge)
1. data-prep stage runs Athena `UNLOAD (SELECT … FROM gold.qlora_train FOR VERSION AS OF <id>)`
   to `runs/<run_id>/distillation/` (general-purpose bucket, versioned).
2. Maps rows to `messages` JSONL, computes sha256, writes `gold.dataset_manifest` + the run
   manifest (`{table_arn, snapshot_id, extract_sha256}`).
3. **Macie scans the extract** (Macie cannot scan table buckets — the existing coverage check in
   `deploy/03_storage.py` extends to this prefix).
4. Training proceeds **unchanged** (launch-and-release, EventBridge resume).

Why not read Iceberg inside the job (one sub-agent's suggestion)? Because the snapshot may be gone
after 120 h, a VPC/network-isolated job may have no route to `s3tables`, and Macie cannot see it.
The export is also the artifact the right-to-be-forgotten purge must cover (a `DELETE` becomes
physical only after snapshot expiry + `nonCurrentDays`).

### 4.5 Training
Keep the existing QLoRA trainer (nf4, double quant, LoRA r16/α32), **adapter-only output**
(`--merge_adapters false`) so adapters stay first-class. Log to the MLflow App
(`mlflow==3.10.x` + matching `sagemaker-mlflow`, pinned to the App version) with the Gold snapshot
as an input dataset. Instances: ml.g6.xlarge/2xlarge (L4, $1.13–1.22/h) for 1.7B; g6e for 8B.

Alternative worth knowing: **SageMaker serverless model customization** supports Qwen3 1.7B/8B
with LoRA (bf16, not QLoRA) at ≈ $0.50 per 1M tokens and logs to MLflow automatically. If 4-bit
is not a hard requirement, it removes the GPU-instance choice entirely.

### 4.6 Registry and approval (governance core)
1. `mlflow.register_model` → automatic Model Package in a group per base model, status
   `PendingManualApproval`; `CustomerMetadataProperties` = {run_id, snapshot_id, extract_sha256,
   mlflow_run_id, adapter_uri, base_model@revision}.
2. eval stage logs gate metrics via `sagemaker_mlflow.evaluate` → model-card evaluation details;
   plus a Bedrock judge job (BYOI) for safety/quality.
3. **Human approval reuses the existing KMS-signed, hash-chained approval** and then calls
   `UpdateModelPackage(Approved)` — note an MLflow lifecycle alias does **not** set Approved by
   itself. An IAM condition key (`sagemaker:ModelLifeCycle/stage`) keeps agent roles from
   promoting to production.
4. **Deploy is triggered by the approval event**, never by the training stage.

### 4.7 Serving
- SageMaker **vLLM DLC** (pinned dated tag) on an endpoint with a **base inference component**
  (`SM_VLLM_ENABLE_LORA=true`, `SM_VLLM_MAX_LORAS`, `SM_VLLM_MAX_LORA_RANK`,
  `VLLM_ALLOW_RUNTIME_LORA_UPDATING=true`); CUDA-13 images need `InferenceAmiVersion`.
- **One adapter IC per approved version** (`BaseInferenceComponentName`, `ArtifactUrl` →
  `adapter.tar.gz`, files at the archive root). Promotion = `CreateInferenceComponent`; in-place
  swap = `UpdateInferenceComponent` with auto-rollback alarms; rollback = point back at the
  previous `ArtifactUrl`. The base is never redeployed.
- Clients call `…/endpoints/<ep>/inference-components/<ic>/openai/v1/chat/completions` with a
  short-lived bearer token minted by the gateway; tenants never hold AWS credentials.
- Serve a bf16 base and **evaluate the QLoRA adapter on that serving dtype** (trained on an nf4
  base, served on bf16 — a real mismatch the eval gate must measure, not assume away).

### 4.8 Feedback loop (flywheel)
Gateway logs `{request_id, tenant, adapter_ic, model_package_arn, redacted prompt/response,
tokens, latency}` asynchronously to Kafka `inference.logs`; ratings go to `feedback` keyed by
`request_id` (= `InferenceId`). Data Capture is a secondary audit copy (it stops at high disk
use; streaming coverage unverified). A scheduled Silver job joins logs + feedback, filters
(rating, PII, toxicity, dedupe, decontamination against eval), and proposes the next Gold
version — which goes through the same DQ + human approval before it can train anything.

---

## 5. Governance (enterprise)

| Area | Today in this repo | Add |
|---|---|---|
| Data access | IAM, `customer-data/` read-only | S3 Tables IAM mode first; Lake Formation (row/column, LF-tags `classification/pii/tenant`) when multi-team |
| PII | Macie on general-purpose buckets + regex scan | Glue Detect-PII Bronze→Silver; Guardrails ANONYMIZE for zh-TW; Macie on the frozen extract |
| Data quality | readiness audit (prompt-driven) | Glue DQ rulesets as hard gates; results posted to the catalog |
| Lineage | hand table in PROJECT_STATE | OpenLineage START/COMPLETE per stage from the harness driver → Unified Studio; ML nodes (job, adapter, package, IC) emitted by us |
| Model approval | KMS-signed plan acceptance; registry always Pending | Registry-gated deploy; approver-only IAM condition keys; model cards |
| Inference | none | Gateway: Cognito JWT, per-tenant RPM/TPM, Cedar policy + Guardrails (LOG_ONLY first), PII-redacted logging |
| Cost | Cost tab, budgets, ON/OFF switches | Tenant × adapter token attribution from `inference_logs`; new SKUs in `cost_model.py` |
| Audit | stage events, transcripts | CloudTrail data events for MLflow (`CallMlflowApp`) and InvokeEndpoint |
| Accounts/network | single account, public-network harnesses | Phase 5: governance / data / ml-dev-stage-prod accounts; VPC-only with PrivateLink |

---

## 6. Visualization — the single pane

Principle: **the console renders governance state, it never owns it.** Approvals live in the
registry, lineage in DataZone, metrics in MLflow/CloudWatch. The console is a run-centred status
and decision plane that deep-links to each service's own page; it does not re-implement the
MLflow UI or a lineage editor (AWS consoles generally cannot be iframed).

New console surfaces (all derived from APIs, per the repo's derive-don't-copy rule):

| Surface | Shows | Source APIs |
|---|---|---|
| **Pipeline** (extend) | new stages: Ingest, Bronze, Silver, Gold, Export, Register, Approve, Promote | Step Functions history, `STAGE_FLOW` additions |
| **Data** (new) | tables, snapshot, row counts, DQ pass %, PII disposition, freshness, Macie coverage | Glue/S3 Tables, Glue DQ, `GetTableMaintenanceJobStatus` |
| **Models** (new) | package groups, versions, approval/lifecycle, gate metrics vs thresholds, Approve/Reject (KMS signer) | `ListModelPackages`, `DescribeModelPackage`, MLflow REST |
| **Inference** (new) | tenant × adapter tokens/cost, throttles, guardrail interventions, adapter→package map | gateway metrics, `inference_logs` via Athena |
| **Lineage** (new run view) | Gold snapshot → extract → run → adapter → package → IC → tenants, each a deep link | DataZone `QueryGraph`, `GetLineageNode` |

The reference repo's interactive diagram (click a stage → its data, schema, service; deep-link
`#step=…`) is a good model for the Architecture tab's lifecycle view.

---

## 7. Integration with the current platform

| Seam | Change |
|---|---|
| Deploy scripts | `deploy/10_lakehouse.py`, `11_streaming.py`, `12_mlflow.py`, `13_serving_gateway.py` — idempotent, `--dry-run`, `--teardown`, SSM `/llmops/*` |
| State machine | new states (Export, Register, ApproveWait, Promote); `PipelineModeChoice` gets `lakehouse` mode; spine-order and state-count doc guards will fire (EN + zh-TW) |
| Harnesses | data-prep gains `export_gold`; deploy gains `promote_adapter` (adapter IC, no merge); eval gains MLflow logging; possibly a new `llmops_lakehouse` harness for medallion ops |
| Contracts | `events.py` (e.g. `MODEL_REGISTERED` into `STAGE_EVENT_MAP`, `MODEL_APPROVED` needing a rule + translator), manifest schema (`dataset{table_arn,snapshot_id,sha256}`, `adapter_ic`) — also fix the schema's `^llmops-` run-id pattern vs the `run-…` ids the code mints |
| IAM | new scoped statements (s3tables, glue catalog, MLflow `CallMlflowAppApi`, inference-component actions, kafka-cluster); **no `*FullAccess`** even though AWS docs suggest it; verify each with `simulate_principal_policy` as the runtime role |
| Network | MSK and Flink are VPC-only → the Lambdas/harnesses that touch them need VPC mode; interface endpoints bill per AZ (≈ $7/endpoint/AZ/month) |
| Repo rules | public-repo redaction, bilingual docs, generated SVGs, `PROJECT_STATE.md`, evidence files, SDLC intent/spec/plan per phase, no stacked PRs |

---

## 8. Cost (us-east-1, on-demand, from the AWS Price List; demo assumes teardown)

| Item | Demo | Production-shaped |
|---|---|---|
| S3 Tables storage + maintenance (≈250 GB) | ≈ $10 | $350–700 + compaction ≈ $130 |
| MSK (2× t3.small, part-time) / (3× Express xlarge) | ≈ $20–70 | ≈ $1,800 + data ≈ $870 |
| MSK Connect Debezium (1 MCU) / (2×2 MCU) | ≈ $25–80 | ≈ $320 |
| Managed Flink (phase 3; 1+1 KPU) / (8+1 KPU) | $0–160 | ≈ $760 |
| Glue/Athena batch medallion | ≈ $5–20 | scales with data |
| MLflow App | $0 | $0 |
| Training run (1.7B QLoRA, g6.xlarge, ~2–4 h) | ≈ $3–5 per run | per run |
| Serving (g6.2xlarge, only while demoing) | $1.22/h | ≈ $892/month per instance 24×7 |
| VPC interface endpoints (≈6 × 2 AZ) + NAT | ≈ $90–120 | per AZ |
| Unified Studio domain | **unverified** — price before enabling | — |

Demo total ≈ **$150–400/month** + per-run training/serving. The two cost traps: MSK Provisioned
cannot be paused (delete it), and serving endpoints bill while InService (the existing teardown
stage stays mandatory).

---

## 9. Critical review (red team)

### 9.1 Assumptions, most fragile first
| Assumption | Why it may be wrong | Mitigation |
|---|---|---|
| **A1. The lifecycle needs real-time streaming.** | Fine-tuning consumes data in batches (days/weeks). Streaming buys freshness that training does not use, at the highest cost and risk in the design. | Keep MSK (the *sources* are streams, CDC is inherently streaming, and the flywheel needs a log bus), but do Silver/Gold in batch; add Flink only when a streaming consumer exists. |
| **A2. Managed Flink can write S3 Tables on a current Flink.** | The official AWS sample says Flink 1.20 "isn't supported with the S3 Table Sink yet"; the REST-catalog route is undocumented. | Prototype first (§10, spike 1); fallback is the Kafka Connect Iceberg sink + Glue batch. |
| **A3. QLoRA is the right technique.** | For 1.7B, 4-bit saves little memory; it adds a train/serve dtype mismatch and a bnb dependency (vLLM ≥ 0.28 moved bnb to a plugin). | Keep QLoRA (owner's spec, already built) but measure the serving-dtype gap in eval; keep serverless LoRA as a costed alternative. |
| **A4. "LMI" is the serving layer (owner's diagram).** | The native OpenAI path does not list LMI. | Use the SageMaker vLLM DLC — same engine, PagedAttention + continuous batching + multi-LoRA retained. |
| **A5. PII tooling handles zh-TW.** | Comprehend PII is EN/ES only; Chinese quality of Glue Detect-PII / Guardrails is unmeasured. | Spike 4: measure recall on planted zh-TW PII before trusting any detector; layer detectors. |
| **A6. Unified Studio can be the integration hub.** | No single "run" API; ML training is not auto-lineaged; domain KMS key is fixed at creation; adds IAM surface. | Use it as governance/visual layer fed by our OpenLineage events; Step Functions stays the spine. |
| **A7. One AgentCore Gateway can front SageMaker.** | SigV4/bearer-token signing to the SageMaker runtime from a Gateway provider target is unverified; bearer tokens live ≤ 12 h. | Spike 3; fallback API Gateway + Lambda authorizer minting tokens per request. |

### 9.2 Adversarial scenarios
- **Silent irreproducibility:** a model is audited three weeks later; its Gold snapshot was
  expired at 120 h. → frozen export + sha256 in the manifest (§4.4).
- **Approval bypass:** an agent with registry permissions promotes its own model. → approver-only
  IAM condition keys + deploy only on the approval event + KMS-signed record.
- **PII into weights:** a missed phone number in Silver is memorized by the adapter. → layered
  detection, a PII gate on Gold, Macie on the extract, and canary-string tests at eval.
- **Feedback poisoning:** a tenant floods thumbs-up on bad answers to steer the next round. →
  per-tenant caps, judge re-scoring, decontamination, human approval of each new Gold version.
- **Runaway cost:** a demo cluster left running. → `--teardown` scripts, the Cost tab's budget
  sweep, and the 15-minute alarm (already built).
- **Multi-writer conflicts:** two writers on one Iceberg table fail optimistic commits. → one
  writer per table by design.

### 9.3 Evidence quality
- **Strong (verified first-hand, §11):** S3 Tables snapshot defaults and tag/branch failure;
  OpenAI-compatible container list (no LMI); MSK streaming-table constraints.
- **Strong (two independent sub-agents agree):** Macie does not scan table buckets; Model
  Monitor/Clarify closed to new customers; training jobs have no Iceberg input.
- **Moderate (single sub-agent, official doc cited):** prices, quotas, version numbers, LMI
  release mapping, DataZone lineage node types.
- **Weak / open:** everything marked UNVERIFIED in §10 — these become spikes, not assumptions.
- Caveat: the AWS docs MCP server failed (DNS) for every sub-agent; they read the same pages over
  HTTP. Many features are dated 2026, after the analysing model's training data — so the docs,
  not memory, are the evidence.

### 9.4 Alternatives considered
- **All AWS-native (Firehose + Glue + serverless customization + Bedrock CMI):** cheapest and
  fastest, but loses the managed-OSS preference and multi-LoRA (Bedrock CMI needs merged weights,
  no Converse for Qwen3). Rejected as the primary, kept as the cost baseline.
- **Unified Studio as orchestrator (MWAA Serverless):** duplicates Step Functions + harnesses and
  discards the platform's proven spine. Rejected.
- **In-job Iceberg reads for training:** rejected for reproducibility (§4.4).

---

## 10. Phased plan and decisions needed

**Phase 0 — spikes (≈ 2–3 days, < $50):** each answers one UNVERIFIED item with a live test and
an evidence file.
1. Managed Flink 1.20 → S3 Tables via Glue Iceberg REST (or confirm the fallback).
2. vLLM DLC + base IC + adapter IC: OpenAI path on the *adapter* IC, Data Capture with streaming,
   `InferenceComponentName` metric dimension, bnb plugin need.
3. AgentCore Gateway → SageMaker OpenAI path (auth), else API Gateway + Lambda.
4. zh-TW PII recall: Glue Detect-PII vs Guardrails vs privacy-filter on planted data.
5. MLflow App → Model Registry sync, EventBridge events, approval alias behaviour.

**Phase 1 — Lakehouse foundation (batch):** S3 Tables buckets/tables, generator, Glue
Bronze→Silver→Gold with DQ + PII, Gold export → existing training. Console **Data** tab.
**Phase 2 — Registry + serving:** MLflow App, registry-gated deploy, vLLM DLC multi-LoRA, gateway
+ guardrails + OpenAI API. Console **Models** + **Inference** tabs.
**Phase 3 — Streaming + flywheel:** MSK, Debezium CDC, Kafka sink to Bronze, inference-log topic,
feedback join; Flink if spike 1 passes. Demo-mode teardown.
**Phase 4 — Governance layer:** Unified Studio IAM domain, OpenLineage emission, catalog assets,
Lake Formation if needed. Console **Lineage** view.
**Phase 5 — Enterprise hardening:** multi-account, VPC-only/PrivateLink; then stacks 2 and 3.

**Decisions for the owner:**
1. Phase order — lakehouse-first (recommended: training consumes batch data) or streaming-first
   (more visible in a live demo)?
2. Bronze landing — Kafka Connect sink (open, ≈$150 floor) or Express streaming tables (≈$900
   floor, zero ops)?
3. Keep QLoRA, or also offer serverless bf16 LoRA as a second training path?
4. Conversation language mix (zh-TW + EN?) — drives the PII tooling choice.
5. Gateway — AgentCore Gateway (if spike 3 passes) or API Gateway + Lambda?
6. Unified Studio now (phase 4) or earlier for demo visibility?

---

## 11. First-hand verification of the load-bearing claims (2026-10-13)

| Claim | Source read directly | Result |
|---|---|---|
| S3 Tables snapshot defaults 1 / 120 h; tags, branches or `history.expire.*` make snapshot management fail for the whole table | docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-maintenance.html | **Confirmed** |
| OpenAI-compatible path supports vLLM DLC, SGLang DLC, custom containers (LMI not listed); bearer token ≤ 12 h; `CallWithBearerToken` needs `Resource: *` | docs.aws.amazon.com/sagemaker/latest/dg/realtime-endpoints-openai-compatible.html | **Confirmed** (note: the page's prerequisites use `AmazonSageMakerFullAccess` — this repo will use scoped policies) |
| MSK streaming tables: Express only, new table per channel, no backfill, JSON + GSR, same account/Region, 5–15 min freshness, $10/TB | docs.aws.amazon.com/msk/latest/developerguide/msk-data-delivery-iceberg.html | **Confirmed** |

## 12. Key sources (dated 2026-10-12/13 unless noted)
- S3 Tables: s3-tables.html, -maintenance.html, -integrating-aws.html, -integrating-glue-endpoint.html, -integrating-open-source.html, -VPC.html, working-with-apache-iceberg-v3.html; Athena gdc-register-s3-table-bucket-cat.html, unload.html
- Unified Studio: adminguide supported-regions, iam-based-domains-overview, supported-blueprints, network-isolation, associated-accounts; userguide datazone-data-lineage-apis, datazone-support-matrix, use-mlflow-experiments
- Streaming: MSK msk-data-delivery-iceberg*.html, limits.html, msk-connect.html; Managed Flink release-version-list.html, flink-2-2.html, how-pricing.html; Firehose apache-iceberg-considerations.html, history.html; Iceberg flink-writes, kafka-connect, multi-engine-support
- Training/serving: SageMaker customizing-models*.html, mlflow.html, mlflow-track-experiments-model-registration.html, realtime-endpoints-openai-compatible.html, realtime-endpoints-adapt.html, model-monitor-availability-change.html; aws/deep-learning-containers docs/vllm; aws/model-hosting-container-standards 05_lora_adapter_management.md; docs.djl.ai LMI release notes; docs.vllm.ai lora/bnb
- Governance: bedrock guardrails-use-independent-api.html, guardrails-enforcements.html, evaluation-judge.html; bedrock-agentcore gateway-target-inference-provider.html, gateway-rate-limits*.html, policy-guardrails-in-policies.html; Macie data-classification.html; Glue detect-PII.html, glue-data-quality.html; Comprehend how-pii.html
- AWS What's New (S3 Tables timeline, MLflow Apps 2025-12-02, OpenAI-compatible endpoints 2026-05-21, service availability changes 2026-06-30); AWS Price List API (S3, MSK, Kinesis, SageMaker; published 2026-09)

---

## 13. Owner decisions (2026-10-13)

These supersede the cost-driven defaults above; the research findings themselves are unchanged.

| # | Question (§10) | Decision |
|---|---|---|
| 1 | Phase order | **Lakehouse first** — Phase 0 spikes → Phase 1 batch lakehouse → Phase 2 registry + serving → Phase 3 streaming + flywheel |
| 2 | Bronze landing | **Iceberg Kafka Connect sink** on MSK Connect (open source, schema evolution) — on **MSK Express** brokers |
| 3 | QLoRA vs serverless LoRA | QLoRA by default; the serving-dtype gap is measured; a `lora_bf16` path is added in Phase 2 |
| 4 | Conversation languages | **Traditional Chinese + English** (PII tooling decided by spike S4) |
| 5 | Gateway | Decided by spike S3 (AgentCore Gateway vs API Gateway + Lambda) |
| 6 | Unified Studio timing | **Phase 1** — IAM-based domain with catalog/query/notebook visibility and Glue auto-lineage; full OpenLineage + model lineage in Phase 4 |
| 7 | Cost | **Not a constraint — design for the best enterprise architecture**: MSK Express 3-AZ, Managed Flink as a core component (gated only by spike S1's feasibility), VPC-only + PrivateLink from Phase 1, production-grade serving with teardown as a switch, and a documented demo-vs-standing cost posture |
| 8 | Spikes | Treated as deploy-class scripts (`tools/spikes/`): direct create, tagged, teardown in `finally`, evidence file. Real training/eval/deploy work still goes through the pipeline |

Phase 0 now has **seven** spikes: the five in §10 plus **S6** (VPC-mode harness without NAT) and **S7**
(S3 Tables substrate: Iceberg v2, deterministic `UNLOAD … FOR VERSION AS OF`, Glue 5 IAM-mode writes,
Unified Studio visibility and lineage, domain price) — both needed because VPC and Unified Studio moved
into Phase 1.
