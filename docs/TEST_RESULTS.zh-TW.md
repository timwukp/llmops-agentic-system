# 測試結果 —— 證據彙總

[English](TEST_RESULTS.md) · [架構](ARCHITECTURE.zh-TW.md) · [案例研究](CASE_STUDY.zh-TW.md)

下方每一條主張都可追溯到 `deploy/evidence/` 中的驗證文件 —— 每份都是真實 AWS 帳號上
真實調用的記錄（識別符按 SECURITY.md 遮蔽）。「宣稱成功之前必先真實調用」是本 repo
的規矩；本頁是它的帳本。

| 階段 | 門檻 | 結果 | 證據文件 |
|---|---|---|---|
| 0 — 腳手架 | preflight + 配置驗證 + 單元測試 + 離線 dry-run | ✅ | CI + [PROJECT_STATE.md](../PROJECT_STATE.md) |
| 1 — 主幹驗證 | data-prep harness 真實調用驗證 | ✅ PASSED | [VERIFICATION_phase1.md](../deploy/evidence/VERIFICATION_phase1.md) |
| 2 pilot — 數據生成 | 自主蒸餾循環 + 自我補救 | ✅ PASSED | [VERIFICATION_phase2_pilot.md](../deploy/evidence/VERIFICATION_phase2_pilot.md) |
| 2 main — 數據集 | curated.jsonl + 統計落入 S3 | ✅ PASSED | [VERIFICATION_phase2_main.md](../deploy/evidence/VERIFICATION_phase2_main.md) |
| 3 — 訓練 | launch-and-release 產出 ModelTrained | ✅ PASSED | [VERIFICATION_phase3.md](../deploy/evidence/VERIFICATION_phase3.md) |
| 4 — 評估 + 部署 | 門檻判決；endpoint 冒煙 + 回收 | ✅ 管線 PASSED（模型本身門檻 FAILED —— 見下） | [VERIFICATION_phase4.md](../deploy/evidence/VERIFICATION_phase4.md) |
| 5 — 自主運行 | 全程無人 e2e：觸發 → 狀態機 → agents → 誠實終態 | ✅ PASSED | [VERIFICATION_phase5.md](../deploy/evidence/VERIFICATION_phase5.md) |
| FinOps — 成本治理 | 第七個 runtime 上線；每個費率都帶來源；不發表任何猜測 | ⚠️ 部分通過 —— runtime 與算術已驗證；rate card 卡在一次 IAM apply | [VERIFICATION_finops.md](../deploy/evidence/VERIFICATION_finops.md) |

## 靜態與離線檢查（可重複，CI 強制）

| 檢查 | 結果 | 復現方式 |
|---|---|---|
| 單元測試（契約、成本模型、driver 迴圈、Lambda、狀態機文檔） | **565/565 通過** | `.venv/bin/python -m pytest tests/ -q --ignore=tests/golden` |
| Harness 配置驗證（5 個專家 + 指揮家 + 審計員） | **7/7 `RESULT: OK`** | `python deploy/validate_config.py --config agents/<a>/harness.json` |
| 架構 SVG 幾何檢查（虛線零交叉、零穿框） | **CLEAN** | `python tests/check_svg_geometry.py docs/architecture-*.svg` |
| 遮蔽掃描（帳號 ID、憑證、帶帳號的 ARN） | CLEAN | `.github/workflows/redaction-check.yml` |

## 各階段的真實調用

| 階段 | 在真實 AWS 上跑了什麼 | 關鍵驗證事實 |
|---|---|---|
| 1 | data-prep harness 建立 → memory → 可觀測性 → invoke-verify | 從 git 掛載列出技能；`aws sagemaker list-training-jobs` exit 0；S3 寫入經 orchestrator 側確認（`head_object`，80 bytes，時間差 1 秒）；memory 活躍（2 sessions、10 條提取記憶、0% 錯誤）；日誌 + X-Ray 投遞正常。同一循環中發現並修復 6 個實測缺陷（含 Claude ≥ 4.7 的 `temperature`/`top_p` 棄用 —— 只在 INVOKE 時暴露） |
| 2 pilot | 經 DeepSeek-R1（`us.deepseek.r1-v1:0`）蒸餾 8 個 ARC-AGI-2 任務 | agent 從 `stop_reason` 自我診斷 token 截斷（8k → 32k：格式有效率 1/8 → 8/8）；`pilot_raw.jsonl` 213 KB 經 S3 驗證；2 次斷流同 session 搶救 |
| 2 main | 24 任務生成 + 5 階段整理 | `main_stats.json` 從 S3 回讀：24 題解出 8 題、74 次嘗試、best-of-4 提前停止（省約 40% token）；curation 對每個 grid 重新對照 ground truth 驗證，剔除 16 條錯答記錄；最終 6 train / 2 val |
| 3 | QLoRA 訓練（ml.g5.2xlarge），launch-and-release | 作業 Completed，計費 431 秒；train_loss 0.5013 / eval_loss 0.5199；產物（adapter + merged bf16 + metrics.json）經 tarball 驗證；EventBridge → resume Lambda 鏈路觀察到兩次（1.5 秒、0 錯誤）；14336 上下文長度下配合 Liger fused CE 零 OOM |
| 4 | deploy → 冒煙 → 質量門檻 → 回收 | endpoint v5 在 4 個已定位根因的失敗後 InService；冒煙測試經 HTTPS 正確解答旋轉任務；門檻評估兩次、誠實 FAILED（見下）；回收零孤兒（刪除 5 個 model + 5 個 endpoint-config） |
| 5 | 指揮家 + 觸發器 + 5 輪無人 e2e | 指揮家從自然語言目標產出帶成本的 5 階段計劃（估算 $29.09、三級成本護欄）；webhook 實測（403/202）；最終 e2e 穿越 7 個狀態、零人工干預、誠實終態 |

## e2e 連環戰 —— 5 輪迭代，每輪恰好一個真實缺陷（Phase 5）

| # | 到達 | 發現的缺陷 | 修復 |
|---|---|---|---|
| 1 | DataPrepGenerate | Lambda role 缺自定義 bus 上的 `events:PutEvents` | 擴充 3 個 role |
| 2 | FinetuneLaunch | InvokeHarness 收 `harnessArn` 而非 `harnessId`（單元測試假件測不出 API 契約） | driver 增加 SSM 名稱→ARN 解析 |
| 3 | DataPrepGenerate | 換模型途中的 harness 版本傳播窗口隱藏了 inline function | 運行前穩定配置；整組單一模型 |
| 4 | Deploy（7 個狀態） | driver Lambda 900 秒 vs harness 回合 840 秒 = 一次調用只裝一輪；`Sandbox.Timedout` 殺掉了「幹完活但沒來得及彙報」的回合。另外：`gate_passed=null` 被 fail-open 默認值晉升 | 輪間自我重調（續接負載）；**門檻 fail-closed**（僅 `is True`）+ 回歸測試 |
| 5 | RemediateFinetune → 誠實的 EscalateFail | —— 無 —— | —— |

第 5 輪的終態序列正是平台按設計工作的樣子：eval 回報 `FAIL_CLOSED_NO_INPUT`
（2 樣本 mini-run 不存在質量信號），狀態機正確武裝補救迴路，而 finetune agent 回答
`REMEDIATE_PREMISE_INVALID — no quality signal to remediate` → `escalate_human`，
拒絕空燒迭代。零孤兒 endpoint；DynamoDB 中 4 條 `stage_complete` 事件；訓練成本 $0.14。

## 誠實失敗的質量門檻（Phase 4）

16 個留出 ARC-AGI-2 任務（訓練任務 25–40，訓練中從未見過）；teacher 基線
DeepSeek-R1 同題：3/16（18.75%）。

| 迭代 | 預算 | Student 解題 | 格式有效率 | 門檻 |
|---|---|---|---|---|
| 0 | 2,048 tokens（同步） | 0/16 | 18.75% | FAILED |
| 1 | 7,000 tokens（串流） | 0/16 | 18.75% | **FAILED —— 終判** |

為什麼這個判決可信（eval agent 自設的對照）：對輸出的寬鬆重掃仍是 0 解
（不是提取產物的問題）；對照 prompt 走完全相同的客戶端路徑返回了連貫且格式良好的
grid（管線與解析器沒問題）；而解釋一切的診斷指標是 —— **`closed_think_rate` 0%**：
沒有任何一條輸出閉合過它的 `<think>` 塊，生成中位數 5,831 tokens，16 條中 12 條被
上下文上限截停。Student 學會了*開始*推理，卻從未學會*收斂* —— 這正是 6 條訓練軌跡
遠低於「ARC 推理遷移到 1.7B student 的地板」所記載的後果。管線判決 PASSED；模型
判決 FAILED；兩者都沒有為了討好對方而被調整。

## 成本

| 項目 | 成本 |
|---|---|
| Phase 2（teacher tokens：pilot $0.69 + main $5.60） | **$6.29** |
| Phase 3（成功訓練 431 秒 ≈ $0.14 + 失敗啟動分鐘 ≈ $0.50） | **≈ $0.64** |
| Phase 4（5 版本弧線約 3.9 個 endpoint 小時 + 評估 teacher tokens） | **≈ $4** |
| Phase 5 mini-runs | **≈ $1** |
| **全階段總計** | **≈ $12–15** |

整套 test-proven 記錄 —— 六個 agent、一個訓練完成的模型、一個部署又回收的
endpoint、五輪 e2e 迭代 —— 花費大約等於一位人類 LLMOps 工程師一小時的成本。

## FinOps —— 第七個 runtime,以及兩次失敗驗證了什麼(2026-07-31)

完整記錄:[VERIFICATION_finops.md](../deploy/evidence/VERIFICATION_finops.md)。

| 檢查 | 結果 | 重現方式 |
|---|---|---|
| 單元測試(全部套件,含 `test_cost_model.py` + `test_finops.py`) | **565 passed** | `.venv/bin/python -m pytest tests/ -q` |
| Harness 配置驗證,7 個 agent | **`RESULT: OK`** | `python deploy/validate_config.py --config agents/finops/harness.json` |
| 線上艦隊 | **7 個 harness READY** | 用 repo 內建 boto3 呼叫 `list_harnesses` |
| 正典模組有散佈路徑 | 印出 `would upload 4 contract files` | `python deploy/03_storage.py --region us-east-1 --account-id 123456789012 --dry-run` |

本次新增的每一道 guard 都做過 **mutation check**:逐一還原被斷言的行為,確認測試會
失敗。一個「有這行為也過、沒這行為也過」的測試,不是測試。

### 兩次失敗比通過更有價值

**帳單讀取權被拒。** Auditor 回報*「已定價 SKU:0。未定價:全部」*,把既有 card 的
新鮮度標為**未知**而非假設其新鮮,**拒絕**呼叫 `update_rate_card`,並指名了缺少的
確切權限。

**S3 與自己的算術模組都被拒。** 它推導出一張**完整的 37-SKU rate card,`unpriced: []`**,
然後拒絕發表 —— 把自己的產出蓋上 `v1-DRAFT-noncanonical`,理由是 `fallback_static`
那一層就住在它拿不到的模組裡面,並把這件事寫在回報的第一行。

真正要命的失敗模式是:一張看起來很有信心、但下個月沒人能重現的 card。
**有人會依據這些數字批准一筆 $2000 的支出。** Fail-closed 在無人設計過的條件下守住了。

### 費率來源,實測

CE 實現費率與 Price List 在兩邊都有的 5 個 SKU 上一致到 **<0.001%** —— 實現費率作為
主要來源是可信的。但 us-east-1 的 Price List 裡所有 Anthropic 條目只有
`Claude 2.0 · Claude 2.1 · Claude 3 Haiku · Claude 3 Sonnet · Claude Instant`:
**沒有 Fable 5、沒有 Opus 5 —— 那正是 harness 艦隊自己的 LLM 用量、AgentCore 最大的
一條線。** 只讀 Price List 的 refresh 會靜默地把它定價為 $0。

這裡**修正**了一個規劃階段的結論:Price List *可以*定價 DeepSeek-R1。`model` 這個
attribute 的值是裸的 **`R1`**(`provider=DeepSeek`),所以在 84 個值裡找「含
DeepSeek-R1 的名字」必然找不到,然後誤判為不存在。

### 四個只有真的去部署才找得到的缺陷

每一個在 repo 裡都完整、有文件、有單元測試 —— 而且永遠不會被建立出來:
harness 配置不在 `AGENTS` 名單裡;被排程的函式在 `LAMBDAS` 裡沒有條目(每天一次
`ResourceNotFound`,只在 scheduler 自己的 metrics 裡看得到);
`update_function_configuration` 從未傳入 `Role`,導致 role 變更只對「還不存在的函式」
生效(**實測**:回報 `"updated"`,線上函式卻仍保留出生時的 role);以及執行角色
沒有 `finops/*` 授權。

**尚未發表:** rate card 與「用實測 **$10.77** 校驗估算器」都卡在一次 IAM apply。
上述一切依 CE 自己的 `Estimated: true` 旗標都屬 `provisional` —— 把它當作已結算發表,
正是 prompt 明令禁止的一件事。
