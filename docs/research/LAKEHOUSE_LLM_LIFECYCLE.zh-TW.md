# 研究報告 — AWS 上的企業級 LLM 生命週期：湖倉 → QLoRA → 模型註冊 → vLLM → 回饋迴圈

**English: [LAKEHOUSE_LLM_LIFECYCLE.md](LAKEHOUSE_LLM_LIFECYCLE.md)**

狀態：**研究報告，供擁有者決策的草稿** —— 本文內容尚未建置或部署。
日期：2026-10-13。範圍：**第 1 套技術棧 = AWS 服務**（AWS 託管開源 + AWS 原生）。
第 2 套（Databricks）與第 3 套（全開源、自建）延後，但以下每個元件都標註了第 3 套的對應物，
讓我們現在選的介面日後可以直接沿用。

方法：六個研究子代理平行執行（參考用例；S3 Tables + SageMaker Unified Studio；MSK + Managed
Flink；訓練 + MLflow + vLLM；企業治理 + 視覺化；本 repo 現況盤點）。資料來源為 AWS 官方文件、
What's New、AWS Price List API 與 Apache 專案文件，日期 2026-10-12/13。三個最關鍵的論點已親自
複核（§11）。分析以 critical-thinking 方法做紅隊審查：假設、對抗情境、證據分級（§9）。

標籤說明：**[managed-OSS]** = AWS 託管的開源引擎或開放格式；**[AWS-native]** = AWS 專有服務；
**S3→** = 第 3 套（全開源）的對應物。

---

## 1. 摘要

**目標。** 一個企業級、端到端、**可視化**的服務 pipeline 平台，從客戶數據一路到企業開源模型推理，
並具備治理 —— 依擁有者的架構（PDF）：資料來源 → 串流/CDC → 湖倉 Bronze/Silver/Gold → QLoRA 訓練
→ MLflow 模型註冊（基礎模型 + adapter）→ vLLM 推理 → 客戶端應用，日誌與使用者回饋流回 Bronze。

**一段話的建議。** 在現有平台上擴充，而不是另起爐灶：保留 Step Functions + AgentCore harness 作為
主幹，console 作為單一視窗。湖倉採用 **S3 Tables（Iceberg v2）**；以 **MSK** 攝取（CRM/ERP 的 CDC
用 **MSK Connect / Debezium**）；Bronze→Silver→Gold 以**由 pipeline 驅動的 Glue/Athena 批次作業**
完成，**Managed Flink** 等原型證明其 S3 Tables 寫入路徑後再加入；訓練讀取**釘選 Gold 快照的凍結、
帶雜湊的匯出檔**；在 **MLflow App** 註冊（唯一真實來源）並同步到 SageMaker Model Registry，並且
**讓核准來閘控部署**；以 **SageMaker vLLM DLC + LoRA adapter 推理元件（inference component）**
提供服務，透過原生 **OpenAI 相容 API** 對外；推理日誌非同步寫入 Kafka → Iceberg 形成資料飛輪；
**SageMaker Unified Studio** 作為治理／視覺化層（目錄、血緣、notebook），由 driver 送出的
OpenLineage 事件餵入 —— 而不是作為編排器。

**研究中改變我判斷的五個事實（決定了設計）：**

| # | 事實（已驗證） | 影響 |
|---|---|---|
| 1 | S3 Tables **預設 120 小時後讓快照過期**；只要有 Iceberg **tag 或 branch，整張表的快照管理就會失敗** | 訓練無法日後靠 time-travel 回到它的快照。每次 run 要凍結一份帶雜湊的匯出檔。 |
| 2 | SageMaker 訓練作業只讀 S3/EFS/FSx —— **沒有 Iceberg 輸入通道** | 同樣的解法：匯出釘選的快照（Athena `UNLOAD … FOR VERSION AS OF`）。現有 trainer 不必改。 |
| 3 | 原生 **OpenAI 相容端點路徑支援 vLLM DLC / SGLang DLC / 自訂容器 —— 不含 LMI** | 推理容器從 LMI 改為 SageMaker vLLM DLC（仍是 vLLM，仍屬 managed-OSS）。 |
| 4 | **Model Monitor 與 Clarify 已不對新客戶開放**（自 2026-07-30） | 監控 = Data Capture/應用日誌 + MLflow + Evidently + Athena；評測 = `mlflow.genai.evaluate` + Bedrock judge。 |
| 5 | 現在 deploy agent **先部署、後註冊**；核准並沒有閘控端點 | 顛倒過來：註冊 → 核准 → **核准事件觸發**部署。這是治理面的核心修正。 |

**Demo 費用（非全天候，使用完即拆除）：約每月 $150–400，另加每次 pipeline run 約 $5–15。**
具生產規模的串流層本身約每月 $4.5–5.5k（§8）。

---

## 2. 用例與模擬數據（全新設計）

擁有者的參考 repo（`databrick-sagamker-full-stack-self-learning`）實際上是**兩套技術棧上的零售
需求預測**，樣本資料為手寫，沒有 LLM、QLoRA、vLLM、對話或 CRM 數據。它的**模式**可以沿用 —— 刻意
埋設的資料缺陷、每一步的 `schema.json` 契約、跨步驟的數字對帳檢查、可互動的逐階段 pipeline 圖 ——
但**它的資料不能用**。因此 LLM 用例在此重新設計。

**建議情境 ——「東南亞零售客服副駕駛（copilot）」**（刻意沿用參考教材的同一家虛構零售商，讓兩個
自學網站可以串起來）：

| 來源 | 模擬方式 | 落入 Bronze 為 |
|---|---|---|
| 客服對話紀錄（聊天/郵件） | 由現有 Bedrock teacher 產生的多輪對話（繁中 + 英文），含人物設定、意圖（訂單狀態、退貨、庫存、促銷）與結果 | `bronze.conversation_events`（每一輪一列） |
| CRM（客戶、工單、CSAT） | 一個小型 Aurora/RDS PostgreSQL schema，由產生器做 insert/update/delete | 經 Debezium 的 CDC 列 → `bronze.crm_cdc` |
| ERP（訂單、庫存） | 同一個 DB；沿用參考教材的 SAP 樣式（類 `MARD` 庫存、訂單） | `bronze.erp_cdc` |
| 業務系統 / 網頁日誌 | Lambda/ECS Kafka producer 重播 JSONL | `bronze.web_events` |
| 推理日誌 + 使用者回饋（飛輪） | 由推理 gateway 寫入 | `bronze.inference_logs`、`bronze.feedback` |

**刻意埋設的缺陷**（借用概念、內容全新）：自由文字中的 PII（姓名、電話、email、卡號片段、台灣身分證
格式）、重複重送、輪次亂序、schema 漂移（串流中途新增欄位）、有毒/辱罵語句、外洩的 `<think>` 推理、
近似重複對話、評測集污染。每一種缺陷都有一條 Silver/Gold 規則必須攔住它，並有一個測試證明那條規則
確實攔得住。

**Gold 契約**（擁有者指定的 schema）：`instruction, input, output` + `split`（train/eval）+ `pair_id`
+ 來源資訊。現有 trainer 讀的是聊天格式 `messages` JSONL（`pipeline/training/distill/train_qlora.py`），
所以匯出步驟會把 `instruction/input/output → messages` —— 一個純函式，附自己的測試。

---

## 3. 目標架構 —— 第 1 套（AWS）

```
 資料來源（模擬）              攝取                       湖倉：S3 Tables（Iceberg v2）
 ─────────────               ──────                     ───────────────────────────────
 客服對話 ─────────┐        MSK（Kafka）  ────────────► Bronze 原始對話 / CDC / 日誌
 網頁/業務日誌 ────┼─────►  + Glue Schema Registry      │  Glue Detect-PII · 去重 · DQDL
 CRM/ERP DB（CDC）─┘        MSK Connect + Debezium      ▼
                            [第 3 階段：Managed Flink]  Silver 指令對（以 pair_id 做 MERGE）
                                                        │  品質過濾 · 樣本平衡 · 去污染
                                                        ▼
                                                        Gold   qlora_train / qlora_eval + dataset_manifest
                                                        │
                    ┌──── Athena UNLOAD … FOR VERSION AS OF <snapshot> → 凍結 JSONL + sha256（S3）
                    ▼
  QLoRA 訓練作業（PEFT+bnb nf4）─► MLflow App（run、評測、註冊 = 唯一真實來源）
                    │                     │ 同步
                    │                     ▼
                    │               SageMaker Model Registry ── PendingManualApproval
                    │                     │  人工 KMS 簽章核准 → Approved
                    │                     ▼（EventBridge：package 已核准）
                    └────────────►  部署：vLLM DLC 基礎推理元件
                                      + 每個核准版本一個 LoRA adapter 推理元件
                                          │  /openai/v1/chat/completions
                                          ▼
  客戶端（agent、客服系統）─► Gateway（認證/授權、租戶配額、Guardrails、PII 遮罩記錄器）
                                          │ 非同步
                                          └──► MSK topic inference.logs ─► Bronze（飛輪）

 跨所有層的治理與視覺化：
   Step Functions + harness driver ─ OpenLineage 事件 ─► SageMaker Unified Studio（目錄、血緣）
   Console（單一視窗）：Pipeline · Data · Models · Inference · Lineage · Cost
```

### 元件表（標籤 · 選它的理由 · 第 3 套對應物）

| 層 | 元件 | 標籤 | 理由 | S3→ |
|---|---|---|---|---|
| 攝取 | **Amazon MSK**（demo 用 Provisioned Standard；生產/streaming tables 用 Express） | managed-OSS | Kafka 是開放契約；可重播、多消費者 | Strimzi 上的 Kafka |
| CDC | **MSK Connect + Debezium** | managed-OSS | 唯一保留 before/after 影像的開源路徑；Firehose 的 CDC 已於 2025-09-24 移除；Aurora zero-ETL 落在 Redshift 儲存而非 S3 Tables | Strimzi KafkaConnect + Debezium |
| Schema | Glue Schema Registry | AWS-native（免費） | MSK streaming tables 必需；producer 的契約 | Apicurio / Karapace |
| Bronze 落地 | **MSK streaming tables**、Iceberg Kafka Connect sink 或 Firehose | managed-OSS 邊緣 / AWS-native | 見 §4.2 —— 成本下限與開放性的取捨 | Kafka Connect Iceberg sink |
| 湖倉 | **S3 Tables，Iceberg v2** | managed-OSS（開放格式） | 託管壓縮與快照；Glue/Athena/Spark/PyIceberg 皆可讀 | S3/MinIO 上的 Iceberg + Polaris/Lakekeeper |
| 轉換 | **Glue 5 Spark / Athena MERGE**、Glue Data Quality（DQDL） | managed-OSS 引擎 | 訓練需要的正是批次；Glue 5 起自動回報血緣給 Unified Studio | Spark + Great Expectations |
| PII | Glue Detect-PII（繁中加上 Bedrock Guardrails ANONYMIZE） | AWS-native | Comprehend PII 只支援英文/西班牙文 —— 不足以處理繁中 | Presidio |
| 串流處理 | **Managed Flink**（第 3 階段，原型驗證後） | managed-OSS | 串流中的 PII 遮罩、對話 sessionization、CDC upsert | Flink K8s operator |
| 訓練 | 以 SageMaker 訓練作業執行**現有的** `train_qlora.py` | AWS-native 運算 + 開源函式庫 | 唯一能控制 bnb 4-bit 的選項；已建好並驗證過 | K8s 上的 torchrun / Ray Train |
| 追蹤/註冊 | **MLflow App**（serverless，不另收費）→ 同步到 Model Registry | managed-OSS → AWS-native 鏡像 | MLflow 是可攜的唯一真實來源；Registry 提供核准、model card 與 IAM 條件鍵 | 自建 MLflow |
| 推理 | **SageMaker vLLM DLC** + 基礎推理元件 + **adapter 推理元件** | managed-OSS 引擎跑在 AWS-native 託管上 | 原生 OpenAI 路徑；換 adapter 不必重新部署基礎模型 | KServe / llm-d 上的 vLLM |
| Gateway | AgentCore Gateway inference target，或 API Gateway + Lambda | AWS-native，開放協定 | 租戶 JWT、TPM 限流、Cedar 政策 + Guardrails | LiteLLM / Envoy AI Gateway |
| 護欄 | Bedrock `ApplyGuardrail`（明確呼叫） | AWS-native | 組織層級的 guardrail 強制只涵蓋 Bedrock 呼叫，不涵蓋自建端點 | NeMo Guardrails / Llama Guard |
| 評測 | `mlflow.genai.evaluate` + Bedrock judge（BYOI） | managed-OSS + AWS-native | Clarify 不對新客戶開放 | lm-eval-harness |
| 監控 | 應用日誌 + Data Capture → Iceberg → Athena；Evidently；CloudWatch | managed-OSS | Model Monitor 不對新客戶開放 | Evidently + Prometheus |
| 治理 | **SageMaker Unified Studio**（IAM 網域）、Lake Formation（需要細粒度權限時） | AWS-native | 目錄、詞彙表、訂閱、血緣圖、notebook | OpenMetadata/DataHub + Marquez + Ranger |
| 血緣 | OpenLineage 事件 → DataZone `PostLineageEvent` | 開放標準 | 沒有原生的模型/端點血緣節點 —— 由我們送出 | Marquez |

---

## 4. 逐層設計

### 4.1 資料來源與產生器
新增部署腳本（`deploy/10_lakehouse.py`，可重入、`--dry-run`、SSM `/llmops/lakehouse/*`）以及產生器
套件（`pipeline/lakehouse/generate/`），以固定亂數種子產生五種來源，讓每次 demo 都可重現、每個埋設的
缺陷都可計數。對話由現有的 Bedrock teacher 路徑產生，不引入新的模型依賴。

### 4.2 串流與 CDC —— 三種落地方式，一個建議
| 方式 | 每月下限（demo） | 開源？ | 限制 |
|---|---|---|---|
| A. MSK Standard（2× t3.small）+ **Iceberg Kafka Connect sink** 寫 Bronze | 約 $150（MSK 約 $70 + 1 MCU 約 $80） | 全開源 | 只能 append（對 Bronze 剛好）；exactly-once；可選 schema 演進 |
| B. MSK **Express** + **streaming tables** | 約 $900（3 broker 下限） | Kafka 原生、投遞為專有 | 唯讀表；不支援 schema 演進/回補/upsert；只收 JSON + GSR；限同帳號同區域 |
| C. Firehose（自 producer 或 MSK） | 約 $20–60 | AWS-native | Iceberg 1.5.2；schema 須完全一致、多餘欄位被丟棄；每張表一個寫入者 |

**建議：demo 用 A，生產規模的 Bronze 用 B。** CDC：Debezium（1 個 task —— MySQL connector 只能單
task）→ MSK → Bronze append；Silver 以主鍵做批次 `MERGE INTO` 套用 upsert。**MSK Provisioned 無法
暫停** ——「demo 模式」指的是 `--teardown` 把叢集與 connector 刪除；每天 8 小時 × 22 天的話，串流層
約每月 $75。

### 4.3 S3 Tables 上的 medallion
兩個 table bucket：`llmops-raw`（Bronze；含 PII；客戶自管金鑰；短保留期）與 `llmops-curated`
（Silver/Gold；已去識別化）。所有名稱一律小寫（否則 Lake Formation/Glue/Athena 看不到）。**維持
Iceberg v2** —— Athena 讀不了 v3、升級是單向的、Firehose 內建 Iceberg 1.5.2。

| 表 | 主要欄位 | 寫入者 |
|---|---|---|
| `bronze.conversation_events` | event_id, conversation_id, turn_idx, role, content, raw_meta（JSON 字串）, ingest_ts —— `day(ingest_ts)` 分區 | Kafka sink |
| `bronze.crm_cdc` / `erp_cdc` | Debezium envelope（op, before, after, ts_ms, source） | Kafka sink |
| `bronze.inference_logs` / `feedback` | request_id（關聯鍵 = `InferenceId`）, adapter_ic, model_version, 已遮罩的 prompt/response, tokens, latency, rating | gateway → Kafka |
| `silver.instruction_pairs` | pair_id（決定性雜湊 → MERGE 可重入）, instruction, input, output, lang, source_ref, pii_status, redaction_method, dq_run_id | Glue Spark |
| `gold.qlora_train` / `gold.qlora_eval` | instruction, input, output, split, pair_id | Glue/Athena |
| `gold.dataset_manifest` | dataset_version, table, **snapshot_id**, export_uri, **sha256**, row_count, dq_score, pii_report_uri, run_id | 匯出步驟 |

閘門：Silver/Gold 上的 Glue DQ 規則（`IsComplete instruction/output`、`Uniqueness pair_id = 1.0`、
`RowCount > N`、output 中不得出現 `<think>`）。DQDL 無法評估 list 欄位 —— `pii_entities` 需攤平。

### 4.4 從 Gold 到訓練（可重現性的關鍵）
1. data-prep 階段執行 Athena `UNLOAD (SELECT … FROM gold.qlora_train FOR VERSION AS OF <id>)` 到
   `runs/<run_id>/distillation/`（一般用途 bucket，開啟版本控制）。
2. 把資料列轉成 `messages` JSONL、計算 sha256，寫入 `gold.dataset_manifest` 與 run manifest
   （`{table_arn, snapshot_id, extract_sha256}`）。
3. **Macie 掃描這份匯出檔**（Macie 不能掃 table bucket —— 把 `deploy/03_storage.py` 現有的覆蓋檢查
   延伸到這個 prefix）。
4. 訓練**照舊**進行（launch-and-release、EventBridge 回呼）。

為什麼不在作業裡直接讀 Iceberg（某個子代理的建議）？因為快照可能在 120 小時後就不見了、在 VPC/網路
隔離下的作業可能連不到 `s3tables`、Macie 也看不到它。這份匯出檔同時也是「被遺忘權」刪除必須涵蓋的
產物（`DELETE` 要等快照過期 + `nonCurrentDays` 之後才會真正實體刪除）。

### 4.5 訓練
保留現有 QLoRA trainer（nf4、double quant、LoRA r16/α32），**只輸出 adapter**（`--merge_adapters
false`），讓 adapter 成為一等公民。記錄到 MLflow App（`mlflow==3.10.x` 加上對應版本的
`sagemaker-mlflow`，釘選到 App 的版本），並把 Gold 快照記為輸入資料集。機型：1.7B 用 ml.g6.xlarge/
2xlarge（L4，每小時 $1.13–1.22）；8B 用 g6e。

值得知道的替代方案：**SageMaker serverless model customization** 支援 Qwen3 1.7B/8B 的 LoRA（bf16，
非 QLoRA），約每百萬 token $0.50，並自動記錄到 MLflow。如果 4-bit 不是硬性需求，它可以完全省去
GPU 機型的選擇。

### 4.6 註冊與核准（治理核心）
1. `mlflow.register_model` → 自動在「每個基礎模型一個群組」中建立 Model Package，狀態為
   `PendingManualApproval`；`CustomerMetadataProperties` = {run_id, snapshot_id, extract_sha256,
   mlflow_run_id, adapter_uri, base_model@revision}。
2. 評測階段以 `sagemaker_mlflow.evaluate` 記錄閘門指標 → 寫入 model card 的評測細節；另加一個
   Bedrock judge 作業（BYOI）做安全與品質評估。
3. **人工核准沿用現有的 KMS 簽章、雜湊鏈核准**，之後呼叫 `UpdateModelPackage(Approved)` —— 注意：
   MLflow 的 lifecycle alias **本身不會**把狀態設為 Approved。用 IAM 條件鍵
   （`sagemaker:ModelLifeCycle/stage`）讓 agent 角色無法自行升級到 production。
4. **部署由核准事件觸發**，絕不由訓練階段觸發。

### 4.7 推理服務
- SageMaker **vLLM DLC**（釘選有日期的 tag），端點上掛一個**基礎推理元件**
  （`SM_VLLM_ENABLE_LORA=true`、`SM_VLLM_MAX_LORAS`、`SM_VLLM_MAX_LORA_RANK`、
  `VLLM_ALLOW_RUNTIME_LORA_UPDATING=true`）；CUDA 13 的映像需要指定 `InferenceAmiVersion`。
- **每個核准版本一個 adapter 推理元件**（`BaseInferenceComponentName`、`ArtifactUrl` →
  `adapter.tar.gz`，檔案放在壓縮檔根目錄）。升版 = `CreateInferenceComponent`；原地替換 =
  `UpdateInferenceComponent` 並設自動回滾警報；回滾 = 指回上一個 `ArtifactUrl`。基礎模型永遠不必
  重新部署。
- 客戶端呼叫 `…/endpoints/<ep>/inference-components/<ic>/openai/v1/chat/completions`，使用由 gateway
  簽發的短效 bearer token；租戶永遠不持有 AWS 憑證。
- 以 bf16 基礎模型提供服務，並**在這個服務精度上評測 QLoRA adapter**（訓練時基礎模型是 nf4、服務時
  是 bf16 —— 這是真實存在的落差，必須由評測閘門量測，而不是假設它不存在）。

### 4.8 回饋迴圈（資料飛輪）
Gateway 以非同步方式把 `{request_id, tenant, adapter_ic, model_package_arn, 已遮罩的 prompt/response,
tokens, latency}` 寫到 Kafka `inference.logs`；評分寫到 `feedback`，以 `request_id`（= `InferenceId`）
為鍵。Data Capture 作為次要的稽核副本（磁碟使用率高時會停止擷取；串流回應是否涵蓋尚未驗證）。排程的
Silver 作業把日誌與回饋關聯起來、過濾（評分、PII、毒性、去重、對評測集去污染），並提出下一版 Gold ——
這一版同樣要經過 DQ 與人工核准，才能拿去訓練。

---

## 5. 治理（企業級）

| 面向 | 本 repo 現況 | 要補上 |
|---|---|---|
| 資料存取 | IAM、`customer-data/` 唯讀 | 先用 S3 Tables 的 IAM 模式；多團隊時再上 Lake Formation（列/欄權限、LF-tags `classification/pii/tenant`） |
| PII | Macie 掃一般用途 bucket + 正規表示式掃描 | Bronze→Silver 用 Glue Detect-PII；繁中加 Guardrails ANONYMIZE；Macie 掃凍結匯出檔 |
| 資料品質 | readiness audit（由 prompt 驅動） | Glue DQ 規則集作為硬閘門；結果回寫目錄 |
| 血緣 | PROJECT_STATE 中的手寫表 | harness driver 每個階段送 OpenLineage START/COMPLETE → Unified Studio；ML 節點（作業、adapter、package、推理元件）由我們送出 |
| 模型核准 | KMS 簽章的 plan 接受；registry 永遠 Pending | 由註冊閘控部署；只有核准者能用的 IAM 條件鍵；model card |
| 推理 | 無 | Gateway：Cognito JWT、每租戶 RPM/TPM、Cedar 政策 + Guardrails（先 LOG_ONLY）、PII 遮罩後才記錄 |
| 成本 | Cost 分頁、預算、ON/OFF 開關 | 由 `inference_logs` 做「租戶 × adapter」的 token 歸屬；`cost_model.py` 新增 SKU |
| 稽核 | stage events、對話紀錄 | MLflow（`CallMlflowApp`）與 InvokeEndpoint 的 CloudTrail data events |
| 帳號/網路 | 單一帳號、公網 harness | 第 5 階段：治理 / 資料 / ml-dev-stage-prod 分帳號；全 VPC + PrivateLink |

---

## 6. 視覺化 —— 單一視窗

原則：**console 呈現治理狀態，但從不擁有它。** 核准存在 registry、血緣存在 DataZone、指標存在
MLflow/CloudWatch。Console 是以 run 為中心的狀態與決策平面，深連結到各服務自己的頁面；不重做 MLflow
UI 或血緣編輯器（AWS console 通常無法被 iframe 嵌入）。

新的 console 介面（全部由 API 推導，符合 repo「推導、不要複製」的原則）：

| 介面 | 顯示 | 資料來源 API |
|---|---|---|
| **Pipeline**（擴充） | 新階段：Ingest、Bronze、Silver、Gold、Export、Register、Approve、Promote | Step Functions history、`STAGE_FLOW` 新增項 |
| **Data**（新） | 表、快照、列數、DQ 通過率、PII 處置、新鮮度、Macie 覆蓋 | Glue/S3 Tables、Glue DQ、`GetTableMaintenanceJobStatus` |
| **Models**（新） | package 群組、版本、核准/生命週期、閘門指標 vs 門檻、核准/駁回（KMS 簽章） | `ListModelPackages`、`DescribeModelPackage`、MLflow REST |
| **Inference**（新） | 租戶 × adapter 的 token/成本、限流、guardrail 攔截、adapter→package 對應 | gateway 指標、經 Athena 查 `inference_logs` |
| **Lineage**（新的 run 視圖） | Gold 快照 → 匯出檔 → run → adapter → package → 推理元件 → 租戶，每個節點可深連結 | DataZone `QueryGraph`、`GetLineageNode` |

參考 repo 的互動圖（點一個階段 → 看到它的資料、schema、服務；`#step=…` 深連結）很適合作為
Architecture 分頁中生命週期視圖的範本。

---

## 7. 與現有平台的整合

| 接縫 | 變更 |
|---|---|
| 部署腳本 | `deploy/10_lakehouse.py`、`11_streaming.py`、`12_mlflow.py`、`13_serving_gateway.py` —— 可重入、`--dry-run`、`--teardown`、SSM `/llmops/*` |
| 狀態機 | 新狀態（Export、Register、ApproveWait、Promote）；`PipelineModeChoice` 新增 `lakehouse` 模式；主幹順序與狀態數的文件 guard 會觸發（中英文） |
| Harness | data-prep 新增 `export_gold`；deploy 新增 `promote_adapter`（adapter 推理元件、不合併）；eval 新增 MLflow 記錄；可能新增一個 `llmops_lakehouse` harness 處理 medallion 作業 |
| 契約 | `events.py`（例如把 `MODEL_REGISTERED` 加入 `STAGE_EVENT_MAP`、`MODEL_APPROVED` 需要規則 + 轉譯器）、manifest schema（`dataset{table_arn,snapshot_id,sha256}`、`adapter_ic`）—— 順便修正 schema 的 `^llmops-` run id 樣式與程式實際產生的 `run-…` 不一致 |
| IAM | 新增範圍受限的陳述（s3tables、glue catalog、MLflow `CallMlflowAppApi`、推理元件動作、kafka-cluster）；**不用 `*FullAccess`**，即使 AWS 文件建議；每一條都以執行期角色跑 `simulate_principal_policy` 驗證 |
| 網路 | MSK 與 Flink 只能在 VPC 內 → 碰到它們的 Lambda/harness 需要 VPC 模式；interface endpoint 按 AZ 計費（每個 endpoint 每 AZ 約每月 $7） |
| Repo 規則 | 公開 repo 去識別化、雙語文件、SVG 由產生器產生、`PROJECT_STATE.md`、evidence 檔、每個階段的 SDLC intent/spec/plan、不堆疊 PR |

---

## 8. 費用（us-east-1、隨需，來自 AWS Price List；demo 假設會拆除）

| 項目 | Demo | 生產規模 |
|---|---|---|
| S3 Tables 儲存 + 維護（約 250 GB） | 約 $10 | $350–700 + 壓縮約 $130 |
| MSK（2× t3.small，非全天候）/（3× Express xlarge） | 約 $20–70 | 約 $1,800 + 資料約 $870 |
| MSK Connect Debezium（1 MCU）/（2×2 MCU） | 約 $25–80 | 約 $320 |
| Managed Flink（第 3 階段；1+1 KPU）/（8+1 KPU） | $0–160 | 約 $760 |
| Glue/Athena 批次 medallion | 約 $5–20 | 隨資料量成長 |
| MLflow App | $0 | $0 |
| 一次訓練（1.7B QLoRA，g6.xlarge，約 2–4 小時） | 每次約 $3–5 | 每次計 |
| 推理（g6.2xlarge，只在 demo 時開） | 每小時 $1.22 | 每台 24×7 約每月 $892 |
| VPC interface endpoint（約 6 個 × 2 AZ）+ NAT | 約 $90–120 | 按 AZ 計 |
| Unified Studio 網域 | **未驗證** —— 啟用前先查價 | — |

Demo 合計約**每月 $150–400**，另加每次訓練/推理費用。兩個成本陷阱：MSK Provisioned 無法暫停（要刪除），
推理端點在 InService 期間持續計費（現有的 teardown 階段必須保留）。

---

## 9. 批判性審查（紅隊）

### 9.1 假設 —— 由最脆弱的開始
| 假設 | 為什麼可能是錯的 | 緩解 |
|---|---|---|
| **A1. 這個生命週期需要即時串流。** | 微調是以批次（以天/週計）消費資料。串流換來的新鮮度訓練用不到，卻是整個設計中成本與風險最高的部分。 | 保留 MSK（**來源**本身是串流、CDC 天生是串流、飛輪需要日誌匯流排），但 Silver/Gold 用批次；有串流消費者時才加 Flink。 |
| **A2. Managed Flink 能以現行版本寫入 S3 Tables。** | AWS 官方範例寫著 Flink 1.20「尚不支援 S3 Table Sink」；REST catalog 路徑沒有官方文件。 | 先做原型（§10 spike 1）；備案是 Kafka Connect Iceberg sink + Glue 批次。 |
| **A3. QLoRA 是正確的技術。** | 1.7B 用 4-bit 省不了多少記憶體；反而帶來訓練/服務精度落差與 bnb 依賴（vLLM ≥ 0.28 把 bnb 移到外掛）。 | 保留 QLoRA（擁有者指定、已建好），但在評測中量測服務精度落差；serverless LoRA 作為已估價的替代方案。 |
| **A4. 推理層是「LMI」（擁有者的架構圖）。** | 原生 OpenAI 路徑沒有列出 LMI。 | 改用 SageMaker vLLM DLC —— 同一個引擎，PagedAttention、continuous batching、多 LoRA 都保留。 |
| **A5. PII 工具能處理繁中。** | Comprehend PII 只支援英文/西班牙文；Glue Detect-PII / Guardrails 的中文效果未經量測。 | Spike 4：在埋設的繁中 PII 上先量 recall，再決定信任哪個偵測器；多層偵測。 |
| **A6. Unified Studio 可以當整合中樞。** | 沒有單一的「執行」API；ML 訓練不會自動記錄血緣；網域 KMS 金鑰建立後不可改；增加 IAM 面。 | 作為治理/視覺化層，由我們的 OpenLineage 事件餵入；Step Functions 仍是主幹。 |
| **A7. 一個 AgentCore Gateway 就能接到 SageMaker 前面。** | Gateway provider target 對 SageMaker runtime 的 SigV4/bearer token 簽章未經驗證；bearer token 最長 12 小時。 | Spike 3；備案是 API Gateway + Lambda authorizer，每次請求簽發 token。 |

### 9.2 對抗情境
- **悄悄失去可重現性：** 三週後要稽核某個模型；它的 Gold 快照早在 120 小時就過期了。→ 凍結匯出檔 +
  manifest 中的 sha256（§4.4）。
- **繞過核准：** 有 registry 權限的 agent 自己把自己的模型升到 production。→ 只有核准者能用的 IAM
  條件鍵 + 只在核准事件時部署 + KMS 簽章紀錄。
- **PII 進入權重：** Silver 漏掉一支電話號碼，被 adapter 記住了。→ 多層偵測、Gold 上的 PII 閘門、
  Macie 掃匯出檔、評測時做 canary 字串測試。
- **回饋投毒：** 某個租戶對錯誤答案狂按讚，想左右下一輪。→ 每租戶上限、judge 重新評分、去污染、每個
  新版 Gold 都要人工核准。
- **成本失控：** demo 叢集忘了關。→ `--teardown` 腳本、Cost 分頁的預算巡檢、15 分鐘告警（已建好）。
- **多寫入者衝突：** 兩個寫入者寫同一張 Iceberg 表，樂觀並行提交失敗。→ 設計上每張表只有一個寫入者。

### 9.3 證據品質
- **強（親自驗證，§11）：** S3 Tables 快照預設值與 tag/branch 導致失敗；OpenAI 相容容器清單（不含
  LMI）；MSK streaming table 的限制。
- **強（兩個獨立子代理一致）：** Macie 不掃 table bucket；Model Monitor/Clarify 不對新客戶開放；
  訓練作業沒有 Iceberg 輸入。
- **中（單一子代理、引用官方文件）：** 價格、配額、版本號、LMI 版本對應、DataZone 血緣節點類型。
- **弱 / 未定：** §10 中所有標為 UNVERIFIED 的項目 —— 它們變成 spike，而不是假設。
- 但書：AWS docs MCP 伺服器對每個子代理都失敗（DNS）；它們改用 HTTP 讀取同樣的頁面。許多功能的日期
  是 2026 年，晚於分析模型的訓練資料 —— 所以證據是文件，不是記憶。

### 9.4 考慮過的替代方案
- **全 AWS-native（Firehose + Glue + serverless customization + Bedrock CMI）：** 最便宜、最快，但失去
  managed-OSS 的偏好，也失去多 LoRA（Bedrock CMI 需要合併後的權重，且 Qwen3 不支援 Converse）。不作為
  主方案，保留作為成本基準線。
- **Unified Studio 當編排器（MWAA Serverless）：** 與 Step Functions + harness 重疊，丟掉平台已驗證的
  主幹。不採用。
- **訓練時直接在作業內讀 Iceberg：** 因可重現性而不採用（§4.4）。

---

## 10. 分階段計畫與待決事項

**第 0 階段 —— spike（約 2–3 天，< $50）：** 每個 spike 以一次實測回答一個 UNVERIFIED 項目，並留下
evidence 檔。
1. Managed Flink 1.20 → 經 Glue Iceberg REST 寫入 S3 Tables（或確認備案）。
2. vLLM DLC + 基礎推理元件 + adapter 推理元件：*adapter* 推理元件上的 OpenAI 路徑、串流下的 Data
   Capture、`InferenceComponentName` 指標維度、是否需要 bnb 外掛。
3. AgentCore Gateway → SageMaker OpenAI 路徑（認證），否則 API Gateway + Lambda。
4. 繁中 PII recall：在埋設資料上比較 Glue Detect-PII、Guardrails、privacy-filter。
5. MLflow App → Model Registry 同步、EventBridge 事件、核准 alias 的行為。

**第 1 階段 —— 湖倉基礎（批次）：** S3 Tables bucket/表、產生器、Glue Bronze→Silver→Gold 加 DQ +
PII、Gold 匯出 → 現有訓練。Console **Data** 分頁。
**第 2 階段 —— 註冊 + 推理：** MLflow App、由註冊閘控的部署、vLLM DLC 多 LoRA、gateway + guardrails +
OpenAI API。Console **Models** + **Inference** 分頁。
**第 3 階段 —— 串流 + 飛輪：** MSK、Debezium CDC、Kafka sink 寫 Bronze、推理日誌 topic、回饋關聯；
spike 1 通過就加 Flink。Demo 模式的拆除。
**第 4 階段 —— 治理層：** Unified Studio IAM 網域、OpenLineage 送出、目錄資產、必要時 Lake Formation。
Console **Lineage** 視圖。
**第 5 階段 —— 企業強化：** 多帳號、全 VPC/PrivateLink；之後再做第 2、3 套技術棧。

**需要擁有者決定：**
1. 階段順序 —— 湖倉優先（建議：訓練消費的是批次資料），還是串流優先（live demo 時更有看頭）？
2. Bronze 落地 —— Kafka Connect sink（開源，下限約 $150）還是 Express streaming tables（下限約 $900，
   零維運）？
3. 保留 QLoRA，還是另外提供 serverless bf16 LoRA 作為第二條訓練路徑？
4. 對話語言組合（繁中 + 英文？）—— 決定 PII 工具的選擇。
5. Gateway —— AgentCore Gateway（spike 3 通過的話）還是 API Gateway + Lambda？
6. Unified Studio 放在第 4 階段，還是為了 demo 可見度提前？

---

## 11. 關鍵論點的親自驗證（2026-10-13）

| 論點 | 直接閱讀的來源 | 結果 |
|---|---|---|
| S3 Tables 快照預設 1 / 120 小時；tag、branch 或 `history.expire.*` 會讓整張表的快照管理失敗 | docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-maintenance.html | **確認** |
| OpenAI 相容路徑支援 vLLM DLC、SGLang DLC、自訂容器（不含 LMI）；bearer token 最長 12 小時；`CallWithBearerToken` 必須 `Resource: *` | docs.aws.amazon.com/sagemaker/latest/dg/realtime-endpoints-openai-compatible.html | **確認**（注意：該頁的前置條件使用 `AmazonSageMakerFullAccess` —— 本 repo 會改用範圍受限的政策） |
| MSK streaming tables：只支援 Express、每個 channel 建新表、不回補、JSON + GSR、限同帳號同區域、5–15 分鐘新鮮度、每 TB $10 | docs.aws.amazon.com/msk/latest/developerguide/msk-data-delivery-iceberg.html | **確認** |

## 12. 主要來源（除另註外，日期為 2026-10-12/13）
- S3 Tables：s3-tables.html、-maintenance.html、-integrating-aws.html、-integrating-glue-endpoint.html、-integrating-open-source.html、-VPC.html、working-with-apache-iceberg-v3.html；Athena gdc-register-s3-table-bucket-cat.html、unload.html
- Unified Studio：adminguide supported-regions、iam-based-domains-overview、supported-blueprints、network-isolation、associated-accounts；userguide datazone-data-lineage-apis、datazone-support-matrix、use-mlflow-experiments
- 串流：MSK msk-data-delivery-iceberg*.html、limits.html、msk-connect.html；Managed Flink release-version-list.html、flink-2-2.html、how-pricing.html；Firehose apache-iceberg-considerations.html、history.html；Iceberg flink-writes、kafka-connect、multi-engine-support
- 訓練/推理：SageMaker customizing-models*.html、mlflow.html、mlflow-track-experiments-model-registration.html、realtime-endpoints-openai-compatible.html、realtime-endpoints-adapt.html、model-monitor-availability-change.html；aws/deep-learning-containers docs/vllm；aws/model-hosting-container-standards 05_lora_adapter_management.md；docs.djl.ai LMI release notes；docs.vllm.ai lora/bnb
- 治理：bedrock guardrails-use-independent-api.html、guardrails-enforcements.html、evaluation-judge.html；bedrock-agentcore gateway-target-inference-provider.html、gateway-rate-limits*.html、policy-guardrails-in-policies.html；Macie data-classification.html；Glue detect-PII.html、glue-data-quality.html；Comprehend how-pii.html
- AWS What's New（S3 Tables 時間軸、MLflow Apps 2025-12-02、OpenAI 相容端點 2026-05-21、2026-06-30 服務可用性變更）；AWS Price List API（S3、MSK、Kinesis、SageMaker；2026-09 發布）

---

## 13. 擁有者決策（2026-10-13）

以下決策取代前文中以成本為考量的預設；研究發現本身不變。

| # | 問題（§10） | 決策 |
|---|---|---|
| 1 | 階段順序 | **湖倉優先** —— 第 0 階段 spike → 第 1 階段批次湖倉 → 第 2 階段註冊 + 推理 → 第 3 階段串流 + 飛輪 |
| 2 | Bronze 落地 | MSK Connect 上的 **Iceberg Kafka Connect sink**（開源、支援 schema 演進）—— 跑在 **MSK Express** broker 上 |
| 3 | QLoRA vs serverless LoRA | 預設 QLoRA；量測服務精度落差；第 2 階段加入 `lora_bf16` 路徑 |
| 4 | 對話語言 | **繁體中文 + 英文**（PII 工具由 spike S4 決定） |
| 5 | Gateway | 由 spike S3 決定（AgentCore Gateway vs API Gateway + Lambda） |
| 6 | Unified Studio 時機 | **第 1 階段** —— IAM 網域，提供目錄/查詢/notebook 可見度與 Glue 自動血緣；完整 OpenLineage + 模型血緣在第 4 階段 |
| 7 | 成本 | **不是限制 —— 以最佳企業級架構設計**：MSK Express 3 AZ、Managed Flink 為核心元件（只受 spike S1 可行性閘控）、第 1 階段起全 VPC + PrivateLink、生產級推理且 teardown 為開關、明文記錄的 demo vs 常駐成本姿態 |
| 8 | Spike | 視為部署類腳本（`tools/spikes/`）：直接建立、打標籤、`finally` 中拆除、留 evidence 檔。真正的訓練/評測/部署工作仍一律走 pipeline |

第 0 階段現在有**七個** spike：§10 的五個，加上 **S6**（無 NAT 的 VPC 模式 harness）與 **S7**（S3 Tables
基礎：Iceberg v2、可重現的 `UNLOAD … FOR VERSION AS OF`、Glue 5 IAM 模式寫入、Unified Studio 可見度與血緣、
網域價格）—— 因為 VPC 與 Unified Studio 移到了第 1 階段，所以兩者都需要。
