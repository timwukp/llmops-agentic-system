# llmops-agentic-system

**由 AWS Bedrock AgentCore Harness 自主運行的端到端 LLMOps 平台。**

[English](README.md) · [架構](docs/ARCHITECTURE.zh-TW.md) · [安全](SECURITY.md) · [Agent 指引](AGENTS.md)

五個 AI agent —— 數據準備、微調、評估、部署、監控 —— 無人干預地執行完整 LLMOps 生命週期：
**teacher 大模型（Bedrock 上的 DeepSeek-R1）**生成訓練數據，**student 小模型（Qwen3-1.7B）**
以 SageMaker 訓練作業做 QLoRA 微調，通過質量門檻評估後部署到 SageMaker endpoint 並持續監控 ——
只有 agent 呼叫 `escalate_human` 時才需要人類介入。

> **TEST-PROVEN（以測試為證）**：下方每個階段門檻都是在真實 AWS 帳號上的真實調用，
> 證據文件在 `deploy/evidence/`，結果彙總在 `docs/TEST_RESULTS.md`。

## 以兩個 Agent Skill 構建

本 repo 是「skill 驅動工程」的參考實現：

| Skill | 在此扮演的角色 |
|---|---|
| [agentcore-harness-builder](https://github.com/timwukp/agent-skills-best-practice/tree/main/skills/skills/agentcore-harness-builder) | **平台構建** —— 其規定工作流（preflight → 設計 → 撰寫配置 → 建立 → 記憶 → 可觀測性 → 調用驗證 → 版本/釘選 → 評估）構建了每一個 harness；其腳本直接複用於 `deploy/` |
| [MLOps-agent-skills](https://github.com/timwukp/MLOps-agent-skills)（LLMOps 鏈） | **Agent 能力** —— 每個 harness 在 session 啟動時通過 harness `skills` 來源掛載其階段所需的技能（開發用 git、生產用 S3 鏡像） |

## 架構

<!-- 架構圖由 docs/gen_architecture_svg.py 生成；佈局由 tests/check_svg_geometry.py 驗證（虛線零交叉、零穿框） -->

![高層架構](docs/architecture-high-level.svg)

Worker harness 內部（依照真實配置繪製）：

![低層架構](docs/architecture-low-level.svg)

**模型**：teacher DeepSeek-R1（Bedrock serverless）· student Qwen3-1.7B（SageMaker QLoRA → endpoint）·
harness 主迴圈 `global.anthropic.claude-fable-5`。
**狀態**：S3 `runs/<run_id>/manifest.json` · DynamoDB · 共享 AgentCore Memory。
**控制台**：bedrock-agentcore-agent-ops-console（直接復用，環境變數接線）。

核心設計決策（完整論證見 [docs/ARCHITECTURE.zh-TW.md](docs/ARCHITECTURE.zh-TW.md)）：

- **5 個按階段拆分的 harness，而非巨型 agent** —— 各階段獨立掛載技能、獨立版本與 endpoint
  釘選、獨立評估；爆炸半徑小。
- **確定性主幹 + 智能 worker** —— 階段 DAG 不需要 LLM 判斷，因此編排用 Step Functions；
  智能封裝在每個階段內部。
- **訓練採 launch-and-release** —— harness 絕不空等數小時的作業：發起作業後呼叫
  `job_launched` 釋放 session，管線經 `waitForTaskToken` + EventBridge SageMaker 狀態變化
  規則在全新 session 中恢復。狀態存在 S3 manifest，絕不存在 session 裡。
- **企業級態勢** —— 生產環境的 harness 與 Lambda 都在 VPC 內隔離運行（interface endpoints，
  無互聯網出口）；全程最小權限 IAM；生產技能從 S3 鏡像掛載。

## 為什麼這些 agent 能替代 LLMOps 工程師：三層疊加，而不是單一模型

Phase 3 的真實事件（完整記錄見
[`deploy/evidence/VERIFICATION_phase3.md`](deploy/evidence/VERIFICATION_phase3.md)）：
finetune agent 被指派啟動 QLoRA 訓練作業，下載訓練腳本時遭遇 S3 403。**全程無人干預**，它：
探測了兩個 prefix 並歸納出自己的 IAM role 是 prefix 範圍限定（`runs/*` 可讀、`code/*` 不可讀），
而不是無腦重試；按優先級搜索備選（本地 workspace → skill 目錄 → 歷史作業的 sourcedir）；
發現 sandbox 沒有 `tar`，改用 Python `tarfile` 重建 `sourcedir.tar.gz`；上傳到自己**有**寫權限
的 prefix；提交作業；確認 `InProgress`；然後調 `job_launched` 釋放。訓練首次嘗試即啟動，零人力。

這種行為不屬於任何單一組件 —— 它是三層能力的乘積：

| 層 | 提供什麼 | 缺了它會怎樣 |
|---|---|---|
| **模型能力**（harness 主迴圈用 Claude Fable 5） | *每一跳恢復的推理質量* —— 每次失敗都產出有設計的假設（兩點權限探測 →「role 是 prefix 限定」）、有先驗的搜索排序、零猶豫的工具替換。弱模型會重試同一個 403 或直接放棄 | 診斷繞圈或過早升級人類 |
| **Harness 運行時**（AgentCore microVM：shell、文件系統、code interpreter） | *行動能力* —— 探測 S3 權限、構建 tarball、調 SageMaker 都是真實環境裡的真實動作，不是聊天窗口裡的建議 | 診斷正確，但沒有手 |
| **授權的工程設計**（任務 prompt + 掛載的 skills） | *有邊界的行動許可* —— 每個任務 prompt 都明確授予自我修復預算（「診斷、修正、重試 —— 最多 3 次；然後 `escalate_human`」），掛載的 skills 提供正確修復的領域形態（script-mode sourcedir 長什麼樣） | 保守對齊的模型在第一個 403 就停下來問人 |

這個 repo 要證明的論點：**替代人類 LLMOps 工程師的不是某個模型，而是「強模型 × 真實執行環境 ×
明確工程化的授權邊界」這個系統。** 拿掉任何一層，同一事件的結局就是 `escalate_human: S3 403`,
而不是一個跑起來的訓練作業。

同樣的三層還在無提示的情況下產出了：microVM 回收毀掉本地狀態後,agent 自行採用逐任務 S3
checkpoint 並把它記入 manifest 作為標準實踐；發現 sandbox 禁用 `kill` 後改為冪等並行 worker；
數據生成階段從 `stop_reason` 證據自我診斷出 token 截斷並修復（8k → 32k）。

## 蒸餾管線

1. **data-prep** —— 種子提示詞（self-instruct 模式）→ 經 `bedrock-runtime converse` 調 DeepSeek-R1
   → 剝離 `<think>` 推理鏈、保留最終答案 → 去重、PII 清洗、LLM 評審過濾 →
   `distillation/curated.jsonl`（90/10 切分）
2. **finetune** —— Qwen3-1.7B QLoRA SFT（TRL + PEFT，4-bit），SageMaker 訓練作業（ml.g5.2xlarge）
3. **eval** —— student 對 teacher 的留出集對比：LLM 評審勝率（temperature 0）+ ROUGE + 安全檢查。
   **門檻**：student ≥ 0.80 × teacher、安全通過、p50 延遲達標
4. **deploy** —— 合併 adapter、SageMaker endpoint（ml.g5.xlarge）、冒煙測試、Model Registry、資源回收
5. **monitor** —— CloudWatch 指標、成本追蹤、漂移信號、閒置 endpoint 巡查

## Repo 結構

```
agents/           5 個 harness 配置（開發 + 生產雙版本）+ 提示詞
orchestration/    狀態機 + 4 個 Lambda（driver / start / resume / webhook）
deploy/           編號冪等部署腳本 + 最小權限 IAM + 驗證證據
pipeline/         訓練入口 + 契約（manifest schema、事件、報告）
tests/            單元測試 · golden agent 測試 · e2e 驅動 · SVG 幾何檢查
docs/             架構（中/英）· 觸發器 · 測試結果 · 案例研究 · 成本
```

## 快速開始

```bash
python -m venv .venv && .venv/bin/pip install "boto3>=1.43.51" pytest
.venv/bin/python deploy/preflight.py --region us-east-1        # 門檻：READY
.venv/bin/python deploy/01_iam.py --region us-east-1 --dry-run # 先審查，再實際執行
.venv/bin/python deploy/03_storage.py --region us-east-1 --dry-run
# 02_network.py 僅生產環境需要（VPC + endpoints；按小時計費 —— 見 --destroy）
```

完整執行順序：[deploy/README.md](deploy/README.md) · 觸發器：`docs/TRIGGERS.md`（Phase 5）

## 進度

| 階段 | 門檻 | 狀態 |
|---|---|---|
| 0 — 腳手架 | preflight + 配置驗證 + 單元測試全過 | ✅ |
| 1 — 主幹驗證 | data-prep harness 真實調用驗證 | ⏳ |
| 2 — 蒸餾數據 | 經 DeepSeek-R1 產出 curated.jsonl | 待開始 |
| 3 — 訓練 | launch-and-release 產出 ModelTrained | 待開始 |
| 4 — 評估 + 部署 | 門檻通過、endpoint 冒煙測試 | 待開始 |
| 5 — 自主運行 | EventBridge 觸發的全程無人 e2e | 待開始 |
| 6 — 運維 | 控制台上線、回滾演練、雙語文檔 | 待開始 |

## 授權

Apache-2.0
