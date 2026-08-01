# 費用 — 估算、審批閘門與對帳

**English: [COST.md](COST.md)**

這條流水線在沒有人盯著的情況下花真錢：SageMaker GPU 時數、Bedrock teacher token、
AgentCore runtime 與 memory，以及承載它們的那些小服務。在 v1.1.0 之前，這個 repo
裡沒有任何東西會在開跑前估算費用、沒有任何東西在事後對帳、也沒有任何東西能攔下一個
很貴的 run。本文說明補上了什麼 —— 更重要的是說明**一個費用數字可以錯在哪裡**，因為
這裡每一個設計決定，都是從其中某一種錯法推導出來的。

**前提：估算可以是猜的，但「實際費用」絕不能是猜的。** 一個看起來很有信心卻是錯的費用
數字，比一個誠實承認的未知更糟 —— 因為有人會拿它去批准真實的支出。所以系統回報的每一
個數字，都帶著它的來源、以及它是否已經結算。

---

## 1. 實測案例：$10.77

以下所有內容，都以一個**費用來自帳單、而非來自模型**的 run 作為校準基準。

2026-07-31，一個 QLoRA fine-tune 在單張 **ml.g5.2xlarge** 上處理了 **16,550 筆**資料、
耗時 **24,924 秒**、帳單 **$10.77**。

```
費率（Price List API，us-east-1，ml.g5.2xlarge training）：  $1.515 / 小時
計費時間：                                                  24,924 秒  ->  6.923 小時
                                                            6.923 × 1.515 = $10.49
加上 SageMaker 一併計費的約 670 秒啟動／收尾：              7.109 小時 × 1.515 = $10.77
```

估算器只憑 plan 就能重現這個數字：

```
$ estimate_run({"sample_count": 16550, "train_rows": 16550, "endpoint_hours": 0}, card)

category:  sagemaker_training
quantity:  7.109444 小時
basis:     "(16,550 rows / 0.664 rows/s + 670 s setup) / 3600"
cost_usd:  10.770808          實測: $10.77          誤差: 0.0%
```

吞吐量常數（**0.664 rows/s**）與啟動成本（**670 秒**）都不是猜的：它們就是
16,550 ÷ 24,924，以及同一個 run 的殘差。這正是這個實測案例的意義所在 —— 模型是
**從一次真實測量播種出來的**，所以它在 training 這條線上的準確度是事實，不是希望。

`tests/test_console_cost.py::test_estimate_matches_the_measured_e3_run_within_one_percent`
斷言這個誤差維持在 1% 以內。依照計畫自訂的規則：**對一個已知實際費用的 run 誤差超過
20%，那是模型的 bug，不是雜訊。**

### 同一個估算的另外一半

同一次呼叫也回傳了：

```
unpriced: ['agentcore:memory:short-term-events',
           'agentcore:runtime:gb-hours',
           'agentcore:runtime:vcpu-hours',
           'bedrock:global.anthropic.claude-fable-5:input-tokens',
           'bedrock:global.anthropic.claude-fable-5:output-tokens',
           'bedrock:us.deepseek.r1-v1:0:input-tokens',
           'bedrock:us.deepseek.r1-v1:0:output-tokens']
```

有 7 個 SKU 沒有費率，所以它們對總額的貢獻是 **$0**。因此這個總額是一個
**下限（floor），不是估算** —— 而估算本身會說出這件事：寫在 `assumptions` 裡、顯示在
畫面上、列在 `unpriced[]` 裡。否則「因為缺費率而是 $0」和「因為免費額度而是 $0」在畫面
上長得一模一樣 —— 而這正是一個 teacher 模型會被靜靜地算成不用錢的原因。

---

## 2. 費率從哪裡來，以及為什麼最顯然的那個來源不夠用

| 來源 | 優先序 | 實測行為 |
|---|---|---|
| `ce_realized` —— 我們自己的帳單：單價 = 費用 ÷ 用量 | **第 1** | 得出 `USE1-DeepSeek-R1-input-tokens` 為 **$0.00135/1K**、output 為 **$0.0054/1K** |
| `price_list` —— AWS Price List API | 第 2 | ml.g5.2xlarge 給出精準的 **$1.515/小時**，DeepSeek-R1 也與我們的實際費率相差 **<0.001%**。但 `AmazonBedrock` 裡所有 `provider=Anthropic` 的條目都是 Claude 3 或更舊 |
| `fallback_static` | 第 3 | 手動輸入，一律標記來源 |

**Price List API 無法為 harness 艦隊自己的模型定價。** 2026-07-31 實測查詢：us-east-1 裡
所有 `provider=Anthropic` 的條目只有 `Claude 2.0 · Claude 2.1 · Claude 3 Haiku ·
Claude 3 Sonnet · Claude Instant` —— 沒有 Fable 5、沒有 Opus 5。那正是七個 harness 自己
運行所用的模型，也是帳單裡 AgentCore 最大的一條線。一個只建立在 Price List 上的
pricing refresh 會回報「成功」，然後把整個 agent 艦隊靜靜地定價為 $0。

它*可以*為 teacher 定價：DeepSeek-R1 回傳 $0.00135/$0.0054 per 1K，與我們的實際費率相差
**<0.001%**。本文件早先的版本說法相反，而它錯的原因值得留下：`model` 這個 attribute 的值
是裸的 **`R1`**（搭配 `provider=DeepSeek`），所以肉眼掃過 84 個 model 值、找「含
DeepSeek-R1 的名字」必然找不到，然後誤判為不存在。**要用明確的
`Field=model,Value=R1` 過濾器去查，不要用眼睛看。** 每次 refresh 都會重新探測覆蓋率，
因為「這個 API 收錄了哪些模型」正是兩次 refresh 之間會變的東西。

因此優先序是：**實際帳單的費率高於公開價目表**，因為我們自己的發票是唯一保證涵蓋「我們
真的用了什麼」的來源。Price List 是留給「從未使用過、因此不可能有實際費率」的資源的
後備。

費率快取在 `s3://<bucket>/finops/rates/rate_card_latest.json`，並保留按日期的歷史。
這份歷史不是整理癖：估算會蓋上 `rate_card_as_of`，所以幾個月後被質疑的差異，可以拿
**當時實際生效的費率**重新推導。只存「最新」會讓舊的誤差無法解釋 —— 明明只是費率變動
了，看起來卻像是估算錯了。

### 費率卡的健康度，是對照「plan 需要什麼」來衡量的

`rate_card_health(plan)` 回報的缺漏，是相對於 `required_skus_for(plan)`，而不是相對於
費率卡自己的內容。有 40 條不相關的費率、卻沒有 teacher 的價格，這**不是**健康的費率卡
—— 而一個只數筆數的檢查會說它很健康。

---

## 3. $2000 閘門

**任一**門檻被跨越就觸發審批：

| 門檻 | 預設 | 理由 |
|---|---|---|
| 單次 run | `APPROVAL_LIMIT_USD` = **$2000** | 一個很貴的 run |
| 累計 | `CUMULATIVE_LIMIT_USD` = **$2000** | 專案至今實際支出 **+** 本次估算 |

二十個 $150 的 run 和一個 $3000 的 run 是同樣的曝險，而那二十個每一個單獨看都能通過
單次檢查。只有第一個門檻的閘門，是一個防得住一種超支形狀、卻對另一種完全看不見的閘門。

### 它看的是 `worst_case_usd`，永遠不是 `total_usd`

自我修復迴圈最多可以重跑 finetune `max_iterations` 次（預設 3）。所以一份估算有兩個
數字：

```
sample_count 2,000,000、max_iterations 3：
    total_usd       $1,268.32     （預期 —— 只跑一次）
    worst_case_usd  $3,803.95     （三次修復迭代全部發生）
```

用 `total_usd` 把關會在 $1,268 放它過去，然後允許 $3,804 的支出。批准一個 $2000、卻
可能靜靜變成 $6000 的東西，不叫閘門。估算同時回報兩者；閘門讀最壞情況，而回應中的
`gating_basis` 會說出是哪個欄位做的決定。

### 閘門在啟動時重新推導，不信任儲存的結果

一份在「專案至今支出很低」時定價的估算，一週後是不同的曝險。所以 `start_run` 在啟動時
重新計算判定，只要**儲存的判定或新算的判定**任一要求審批，就要求審批。一個信任過期判定
的閘門，不是閘門。

### 每一條失敗路徑都朝「關閉」的方向倒

| 失敗 | 行為 | 為何不採另一種做法 |
|---|---|---|
| `cost_model` 匯入失敗 | **要求**審批 | 「我們無法檢查限額」必須落在「要求審批」這一側，絕不能落在「放行」那側 |
| 沒有費率卡 | **拒絕估算（503）** | 一個「$0 附帶警告」的總額會被拿去引用；一個明確的拒絕不會 |
| Cognito 群組查詢失敗 | **拒絕** | 一次被限流的 API 呼叫，不該變成一次批准 |
| 沒有 `sample_count`／`train_rows` | **400** | 兩者皆無時 training 這條線就是 $0，那個總額不是估算 |

### 職責分離

審批需要 Cognito 群組 `llmops-approver` 的成員身分，且**每次呼叫都在伺服器端檢查** ——
在 UI 上藏掉按鈕不是控制措施。送審者不能批准自己的請求：自我批准是**以 403 拒絕**，
不是只標記一下。

有兩個事實讓這件事比看起來難：`cognito-idp:GetUser` 會驗證 token 並回傳使用者名稱，
但**不會**回傳群組成員身分；而 bearer *access* token 也不帶 `cognito:groups` claim。
所以成員身分需要第二次呼叫（`AdminListGroupsForUser`）—— 而它可能失敗，失敗時使用者
拿到空的群組清單並被拒絕。

終態就是終態。被 `rejected` 的估算不能重新啟動（否則一次拒絕可以被安靜地重試到有人批准
為止），已經 `launched` 的也不能再啟動一次（否則兩個 run 掛在同一次批准上，並在差異報告
裡被重複計算）。這兩條**不論該估算是否需要審批都適用** —— 便宜的 run 才是沒人想到要防
的那個。

### 在瀏覽器裡，401 和 403 是不同的事

`401` 是 token 不見了；`403` 是 token 沒問題、但這個使用者沒有這個權限。console 把兩者
分開。混為一談會把一個審批者從一個仍然有效的 session 登出，並把「你不在審批群組裡」
藏在「session 過期」後面。

---

## 4. 歸帳：按資源，絕不按服務

這是整個設計裡影響最大的一個決定，而它來自一次測量。

2026-07-31，這個 AWS 帳戶當月至今的總支出是 **$27,491**。本專案自己的份額是
**SageMaker 約 $3.50 加上 Bedrock teacher 約 $6.29 —— 大約 $10–15。** 其餘全部屬於同一
帳戶裡不相關的工作，包括 SageMaker Canvas 的 session 時數（約 $296）與一個 JumpStart
Whisper endpoint（約 $18/天）。

因此，一個按**服務**過濾的彙總會把**數千美元別人的支出報成我們的** —— 並且會在第一次
評估時就觸發 $2000 閘門。歸帳採白名單制：明確比對本專案自己建立的資源。

```
ce get-cost-and-usage-with-resources  按 RESOURCE_ID 分組
  -> training-job/llmops-qlora-run-phase2-main-0001-r3   $0.1035
  -> endpoint/llmops-student-run-phase2-main-0001-v5     $1.0203
```

按 run 歸帳**完全不需要 tag**，因為 `run_id` 本來就在 job 與 endpoint 的名稱裡。這點很
重要，因為 tag 這條路是不通的：

- `ce list-cost-allocation-tags` 顯示 `project` 與 `Project` 都是 **Inactive**；帳戶上
  Active 的 tag 數為零。
- 一個以 `Tags project=llmops-agentic-system` 過濾的 CE 查詢，對 2026-07-30 回傳
  **$0.00** —— 而那天有真實支出。

Cost allocation tag 也**不會回溯**：那個 $10.77 的 run 永遠不會帶上 tag。啟用這個 tag
是作為未來的交叉驗證，絕不是主要的歸帳方式。

AgentCore 的 token 支出透過 CloudWatch `aws/spans` 歸帳，其中每個 span 都帶著
`attributes.session.id` 以及 `gen_ai.usage.input_tokens`／`output_tokens` —— 而 console
本來就是用 `(run_id, stage, task)` 組出 `session_id` 的。CloudWatch 的**指標**
`bedrock-agentcore/gen_ai.client.token.usage` 不能用來做這件事：它的維度是
`server.address`、`gen_ai.request.model`、`gen_ai.token.type` —— 沒有 run、沒有 session。
那是一個帳戶層級的數字。

---

## 5. 已結算 vs 暫定

Cost Explorer 大約延遲 **24 小時**，並把近期期間標記為 `Estimated: true`。在
2026-07-31 當天查詢 2026-07-31 的資源層級費用，回傳 `Estimated: true` 且**零個分組**。

由此得出的結果，全部都有強制執行：

- 對帳是**非同步且可重跑的**，絕不與 run 同步進行。
- 每一筆實際費用列都帶 `settlement` ∈ `provisional | settled`。
- 彙總**分別**回報 `settled_usd` 與 `provisional_usd` —— 絕不合成單一總額，因為一個混合
  總額會引誘人去引用一個還沒落定的數字。
- 一個 **run** 只有在它的**每一列**都已結算時才算已結算。一列暫定就意味著總額還會變動，
  而把那種狀態稱為已結算，正是這條規則存在要防的錯誤。

---

## 6. 差異分析：是哪一條線估錯了，不是整體差了多少

`reconcile(estimate, actual)` 回傳各類別的差異、一個 `accuracy_ratio`，以及一個
**指名出主要肇因類別**的 `verdict`。一句總計「差了 40%」不能告訴任何人要修什麼；
「bedrock_teacher 是主要肇因」可以。

差異報告也會回報 `n_unestimated` —— 有多少個 run 根本沒有估算。不帶估算就啟動的 run
仍然合法，因為 v1.1.0 之前每一個 run 都是這樣跑的；而**保持它合法，正是讓報告能誠實
說出「有多少比例的支出從未被估算」的前提**。一份對此保持沉默的差異報告，會讓人以為它
的覆蓋率比實際更高。

---

## 7. `llmops_finops` runtime

第 7 個 AgentCore harness —— 財務審計員／統計員／報告員 —— 依 `params.task` 執行三項
職責：

| 任務 | 觸發 | 做什麼 |
|---|---|---|
| `reconcile` | 每日 09:00 UTC，或手動 | CE 資源層級 + spans → 各 run 實際費用、與估算的差異 |
| `pricing_refresh` | 手動 | 實際費率（主）+ Price List（備）→ 費率卡 |
| `report` | 手動 | 專案彙總 + 估算準確度趨勢 |

### 為什麼要第 7 個 runtime，而不是擴充 `monitor`

`monitor` 跑在狀態機**裡面**：每個 run 一次、在該 run 的生命週期內，回答「endpoint 現在
還活著嗎」。對帳是相反的形狀 —— 它在 run **結束之後**才跑（CE 延遲）、**橫跨多個** run、
而且它對專案負責而不是對某個 run 負責。一個昨天就結束的 run，沒有任何活著的 agent 能
去歸屬今天才結算的帳單。所以 `llmops_finops` 和 `llmops_orchestrator` 並列在狀態機
**之上**：指揮者決定*要花什麼*，審計員報告*花了什麼*。

它會出現在 console 的機隊視圖裡，但永遠不會出現在某個 run 的階段序列中。

### 審計員不能停掉 run

它的 IAM 對帳務是**唯讀的** —— `ce:Get*`、`pricing:*`、`budgets:ViewBudget` ——
而且沒有終止任何東西的權限。審計員絕不能有能力改變它所審計的對象，而支出控制的權限屬於
orchestrator（透過 `page_human`），不屬於一個職責是「觀察」的元件。一個有終止權的審計員
是另一種、而且風險更高的設計。

---

## 8. Cost 分頁

五個面板，順序就是錢自己的順序：

1. **估算一個 run** —— plan 輸入 → 逐列的明細表，每列帶 `basis` 與 `rate_source`，
   `total` 對照 `worst_case`，以及一個明確的紅色 UNPRICED 區塊。
2. **審批佇列** —— 待審請求與完整估算明細；駁回必須填理由。
3. **各專案實際支出（含明細）** —— 依類別、依服務、依 run、依期間，帶
   `settled`／`provisional` 標記。
4. **估算 vs 實際** —— 各 run 差異、主要肇因類別、以及 `n_unestimated`。
5. **費率卡健康度** —— 筆數、來源、最舊的 `as_of`，以及 plan 需要但費率卡缺少的項目。

第五個面板不是附加品。一個沒被注意到的過期費率，會讓前面四個面板都變成「很有信心地
錯」—— 它正是那個本來就該抓到 DeepSeek-R1 被定價為 $0 的面板。

### API

| 方法 | 路徑 | 認證 | 用途 |
|---|---|---|---|
| GET | `/api/cost-overview` | 公開讀取 | 彙總、已結算／暫定拆分、預算、費率卡健康度 |
| GET | `/api/cost-estimates` | 公開讀取 | 估算、審批佇列、差異 |
| POST | `/api/cost-estimate` | 需登入 | 為一份草稿 plan 定價 |
| POST | `/api/cost-approval-request` | 需登入 | 送出審批 |
| POST | `/api/cost-approval` | 需登入 **+ `llmops-approver`** | 批准／駁回，可選擇同時啟動 |
| POST | `/api/finops-run` | 需登入 | 觸發 `reconcile`／`pricing_refresh`／`report` |

閘門的算術本身只住在**一個**地方 —— `pipeline/contracts/cost_model.py`。console 委派給
它，而不是自己再實作一份，因為第二份拷貝就是會飄移的那份，而飄移的那份恰好會是守著啟動
按鈕的那份。

---

## 9. 儲存

| 表 | 鍵 | 存什麼 |
|---|---|---|
| `llmops-cost-estimates` | PK `id` | 完整逐列估算、`worst_case_usd`、`status` ∈ `draft｜pending_approval｜approved｜rejected｜launched｜reconciled`、`requested_by`、`approved_by`、`decided_at`、`rejection_reason`、`sfn_execution_arn`、`rate_card_as_of` |
| `llmops-cost-actuals` | PK `project`、SK `<period>#<run_id>#<category>` | 已歸帳的費用列，帶 `settlement` 與 `ce_estimated_flag`；另有保留的 `#audit#` 與 `#finding#` 列 |

審批狀態住在自己的表裡，而不是 console 的通用表：它是一筆稽核紀錄，需要自己的 schema
與保存期規劃。

`#audit#` 與 `#finding#` 列**被排除在所有費用加總之外**。它們是 agent 自己的筆記 ——
一個 finding 描述的是某個差異，把它加進去會把它所描述的那筆支出重複計算一次。

---

## 10. 這個系統不做什麼

- **沒有多帳戶或 Organizations 層級彙總** —— 目前是單一帳戶。
- **沒有 Savings Plans 或 Reserved Instance 模型** —— 只用 on-demand 費率，並在每份估算
  中明確列為假設。
- **不會在超出預算時自動停機** —— 審計員只報告與標記；停掉一個 run 是 orchestrator 的
  權限。見 §7。
- **只要 `unpriced[]` 非空，總額就是一個下限。** 今天這包含 teacher 與 harness 的 token
  兩條線，這不是小的遺漏 —— 所以它被寫在每一份估算上，而不是藏起來。

在這個系統的閘門之下，帳戶層級本來就已經有一道護欄：一個 AWS Budget，
`bedrock-monthly-dev`，**每月 $1000**。Cost 分頁把它顯示出來，而不是再做一個。

---

## 11. 測試

| 檔案 | 測試數 | 涵蓋 |
|---|---|---|
| `tests/test_cost_model.py` | 52 | 估算算術、費率優先序、歸帳、reconcile |
| `tests/test_finops.py` | 36 | harness 設定、reconcile Lambda、儲存、IAM 形狀 |
| `tests/test_console_cost.py` | 59 | HTTP 層、雙門檻閘門、職責分離、啟動防護 |

147 個測試全部不需要 AWS 憑證即可執行，使用注入的 client 與帳號 `123456789012`。

這套測試經過 **mutation check**（變異測試）：把每一道防護逐一破壞，重跑測試確認真的有
測試會失敗。它找出兩道「測試全綠卻沒有真正涵蓋」的防護 —— 一是啟動時的閘門若改讀
`total_usd` 而非 `worst_case_usd` 不會被發現，二是終態檢查從未在未超限的估算上被執行過。
兩者現在都有測試了。**一個不可能失敗的測試不是測試**，而只有變異測試能指出哪些是那種
測試。
