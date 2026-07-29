# 觸發器 —— 啟動一次運行的四種方式

[English](TRIGGERS.md) · [架構](ARCHITECTURE.zh-TW.md) · [測試結果](TEST_RESULTS.zh-TW.md)

四個觸發器全部匯聚到同一個入口：`llmops-start-pipeline` Lambda。它鑄造 `run_id`、
播種 S3 manifest（每個階段都讀的唯一事實來源）、把運行記進 DynamoDB、發出
`PipelineStarted` 事件、啟動 Step Functions 執行。觸發負載中的 `params` 會覆蓋
manifest 默認值（dataset、sample_count、gates、實例類型、`max_iterations`、模型）。

由 `deploy/08_triggers.py` 佈建（冪等、支持 `--dry-run`）。截至 Phase 5 的實測狀態：

| 觸發器 | 機制 | 認證 | 驗證狀態 |
|---|---|---|---|
| EventBridge Scheduler | 每夜 cron，**默認 DISABLED** | scheduler role | 已建立 |
| Webhook | API Gateway HTTP API `POST /webhook` | HMAC-SHA256 | 實測：壞簽名 → 403，好簽名 → 202 + 運行啟動 |
| Admin API | 同一 HTTP API 的 `POST /runs` | AWS_IAM | 路由已上線 |
| GitHub Actions | `workflow_dispatch` → OIDC → Lambda 調用 | OIDC role | workflow 已在 repo（`.github/workflows/run-pipeline.yml`）；需一次性 OIDC role + `AWS_OIDC_ROLE_ARN` secret |

## 1. EventBridge Scheduler（每夜 cron）

排程 `llmops-nightly`：`cron(0 3 * * ? *)`（每夜 03:00 UTC，15 分鐘彈性窗口），以
`{"trigger_source": "scheduler"}` 調用 `llmops-start-pipeline`。建立時即為
**DISABLED** —— 一個每次運行都花真金白銀的平台，不應該在佈建完成的那一刻就開始
給自己計費。

真的需要每夜運行時再啟用：

```bash
# 經部署腳本（冪等的建立或更新，狀態設為 ENABLED）
python deploy/08_triggers.py --region us-east-1 --enable-schedule

# 或直接
aws scheduler update-schedule --name llmops-nightly --state ENABLED \
  --schedule-expression "cron(0 3 * * ? *)" ...
```

Scheduler 假扮一個專用 role（`llmops-scheduler-invoke`），其唯一權限是對
`llmops-start-pipeline` 的 `lambda:InvokeFunction`。

## 2. Webhook（HMAC-SHA256）

`llmops-triggers` HTTP API 上的 `POST /webhook`（endpoint URL 發布在 SSM
`/llmops/triggers/api_endpoint`）。路由本身不設 API Gateway 認證 —— 驗證發生在
`llmops-webhook` Lambda 內部：呼叫方必須帶上
`X-Signature-256: sha256=<原始 body 的 hmac-sha256 hex>`，密鑰是 Secrets Manager
中的共享 secret（`llmops/webhook`，由 `08_triggers.py` 自動建立）。

計算簽名並發送請求：

```bash
ENDPOINT=$(aws ssm get-parameter --name /llmops/triggers/api_endpoint \
  --query Parameter.Value --output text)
SECRET=$(aws secretsmanager get-secret-value --secret-id llmops/webhook \
  --query SecretString --output text)

BODY='{"params": {"task_count": 24, "note": "webhook demo"}}'
SIG=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

curl -s -X POST "$ENDPOINT/webhook" \
  -H "Content-Type: application/json" \
  -H "X-Signature-256: sha256=$SIG" \
  -d "$BODY"
# → 202 {"run_id": "run-...", "manifest_uri": "s3://.../manifest.json"}
```

行為（Phase 5 實測驗證）：

- **簽名無效或缺失 → 403** `{"error": "forbidden"}`。恆定時間比較
  （`hmac.compare_digest`）；響應不洩露任何「為什麼被拒」的信息。
- **簽名有效 → 202**，返回已啟動運行的 `run_id` 與 `manifest_uri`。body 中的
  `params` 以 `trigger_source: "webhook"` 轉發給 start-pipeline。
- 簽名有效但 body 不是合法 JSON → 400。

注意 HMAC 是對**原始請求 body** 計算的 —— 重新序列化 JSON（鍵序、空白）會改變
簽名，直接換來一個 403。

## 3. Admin API（`POST /runs`，AWS_IAM）

同一 HTTP API 上的 `POST /runs` 直連 `llmops-start-pipeline`，採 **AWS_IAM**
授權 —— 呼叫方必須用具備 `execute-api:Invoke` 權限的憑證做 SigV4 簽名。供 ops
console 與運維人員使用；命令行最簡單的是
[awscurl](https://github.com/okigan/awscurl)：

```bash
ENDPOINT=$(aws ssm get-parameter --name /llmops/triggers/api_endpoint \
  --query Parameter.Value --output text)

awscurl --service execute-api --region us-east-1 \
  -X POST "$ENDPOINT/runs" \
  -H "Content-Type: application/json" \
  -d '{"trigger_source": "admin-api",
       "params": {"task_count": 24, "sample_count": 2000}}'
# → {"run_id": "run-...", "manifest_uri": "s3://...", "execution_arn": "..."}
```

對同一路由發未簽名的 `curl`，會在 Lambda 運行之前就被 API Gateway 拒絕。

## 4. GitHub Actions（`workflow_dispatch`，OIDC）

`.github/workflows/run-pipeline.yml` 讓你從 GitHub UI（或 `gh workflow run`）
啟動一次運行，**不需要任何長期 AWS 密鑰** —— job 經 GitHub 的 OIDC provider
假扮一個 IAM role。

一次性設置：

1. 在 AWS 帳號中建立（或復用）GitHub OIDC 身份提供者
   （`token.actions.githubusercontent.com`）。
2. 建立一個信任該提供者、**範圍限定到本 repository** 的 IAM role，唯一權限：
   對 `llmops-start-pipeline` 的 `lambda:InvokeFunction`。
3. 把該 role 的 ARN 存入 repo secret **`AWS_OIDC_ROLE_ARN`** —— 帳號 ID 永遠
   不出現在 workflow 文件裡（本 repo 公開）。

執行：

```bash
gh workflow run run-pipeline.yml \
  -f task_count=24 -f sample_count=2000 -f note="release candidate"
```

語義是 **fire-and-monitor（發射後監控），不是 fire-and-wait（發射後空等）**：
Step Functions 運行比任何 GitHub job 都長命，所以 workflow 以
`trigger_source: "github-actions"` 調用 start-pipeline、把 `run_id` 印進 job
summary、然後綠色退出。進度請看 ops console 或 Step Functions 控制台 ——
Actions 顯示綠色只代表「已啟動」，不代表「已通過」。

狀態：workflow 已隨本 repo 提供（`.github/workflows/run-pipeline.yml`）。
剩餘的一次性設置在 AWS 側：建立 OIDC role 並把其 ARN 存入 repo secret
`AWS_OIDC_ROLE_ARN`（步驟見上）。
