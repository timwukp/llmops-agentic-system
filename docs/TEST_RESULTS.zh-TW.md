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
| 單元測試（契約、成本模型、driver 迴圈、Lambda、狀態機文檔） | **1371/1371 通過** | `.venv/bin/python -m pytest tests/ -q --ignore=tests/golden` |
| Shell 測試套件 —— N 路 capacity race guard（`tests/test_capacity_race_guard.sh`） | **10/10 斷言** | `bash tests/test_capacity_race_guard.sh` |
| 反向控制 —— 逐一破壞每道 guard，確認它會失敗 | **357/357 反向控制** | `.venv/bin/python tests/negative_controls/monitor_dispatch.py` |
| Harness 配置驗證（5 個專家 + 指揮家 + 審計員） | **7/7 `RESULT: OK`** | `python deploy/validate_config.py --config agents/<a>/harness.json` |
| 架構 SVG 幾何檢查（虛線零交叉、零穿框） | **CLEAN** | `python tests/test_svg_geometry.py docs/architecture-*.svg` |
| 遮蔽掃描（帳號 ID、憑證、帶帳號的 ARN） | CLEAN | `.github/workflows/redaction-check.yml` |

### 實測探針 —— triage 生命跡象迴圈（#37），打的是 Lambda 現在真正在跑的東西

刻意不做成 CI 也不做成單元測試：resurrector 的「非 run」那一半，是整個系統裡**健康狀態
與壞掉的狀態長得一模一樣**的唯一一條路徑。一個 triage 大概一個月才死一次，所以
`checked_liveness: 0` 在幾乎每一天都是正常讀數 —— 而「Query 打錯 partition」、「少了
`dynamodb:Query` 授權」、「兩個各自打包的副本對 `__liveness__` 這個字串不一致」，這三種
壞法會產生**完全相同**的讀數。所以這支探針下載**已部署**的 driver 與 resurrector bundle，
對著**真實**的 `llmops-stage-events` 表把整條迴圈跑一遍。

| 檢查 | 結果 | 復現方式 |
|---|---|---|
| 非 run（triage）生命跡象 beat → sweep → 認領 → 復活 → 上限升級 → 終態刪除 | 對已部署 bundle **18/18 項檢查通過**（2026-08-12），對本 checkout 亦 18/18 | `python tools/probe_liveness_resurrection.py --region us-east-1` |

沒有任何 agent turn（假的 AgentCore client 在第一次 invoke 前就拋錯，所以真正的心跳會跑，
計費的那半根本沒開始）、沒有真的 `lambda:invoke`、沒有真的 `events:PutEvents`；
resurrector 處理 run row 的那一半被餵一個空的 stub，因為把 `STALE_MINUTES` 壓成 0
（探針要讓自己剛寫的 beat 看起來過期，這是必要的）否則會去認領並復活帳號裡每一個活的 run。
唯一一筆真實寫入，是專用 `__liveness__` partition 裡一個合成 subject 的項目，離開前刪掉。

**Mutation 檢查，6/6 全殺**：resurrector 讀錯 partition（11/18）· beat 把 `params`
從 payload 裡漏掉，而那正是復活後 triage 唯一的工作依據（15/18）· 終態時用「標記」取代
「刪除」（17/18）· 上限路徑對 triage 自己升級、而不是對它原本在調查的那個 run（17/18）·
beat 失去 `attribute_exists(run_id)` 條件、於是鑄出一個幽靈 run row（4/6）·
sweep 失去新鮮度守衛、把活著的 triage 也復活（16/18）。

第五個 mutant 正是探針為什麼「不只報告、還要刪掉」幽靈 row 的原因：它留下的那一行
**把下一次探針整個反轉了** —— runs 表裡有那一行時，帶條件的 beat 會**成功**，於是往
`__liveness__` 的交接根本不會發生，底下每一項檢查都以完全錯誤的理由失敗。

## 各階段的真實調用

| 階段 | 在真實 AWS 上跑了什麼 | 關鍵驗證事實 |
|---|---|---|
| 1 | data-prep harness 建立 → memory → 可觀測性 → invoke-verify | 從 **git** 掛載列出技能 —— 2026-07-28 當天每一個掛載都是 git；19 個在 v1.2.0 全部搬到 `s3`，所以這行記錄的是當時跑了什麼，不是現在跑什麼；`aws sagemaker list-training-jobs` exit 0；S3 寫入經 orchestrator 側確認（`head_object`，80 bytes，時間差 1 秒）；memory 活躍（2 sessions、10 條提取記憶、0% 錯誤）；日誌 + X-Ray 投遞正常。同一循環中發現並修復 6 個實測缺陷（含 Claude ≥ 4.7 的 `temperature`/`top_p` 棄用 —— 只在 INVOKE 時暴露） |
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
endpoint、五輪 e2e 迭代 —— 花費比這個帳戶單日花在一個沒人看著的閒置 endpoint 上的錢
還少（每天 $36.36，見 [COST.zh-TW.md](COST.zh-TW.md) §4）。

## FinOps —— 第七個 runtime,以及兩次失敗驗證了什麼(2026-07-31)

完整記錄:[VERIFICATION_finops.md](../deploy/evidence/VERIFICATION_finops.md)。

| 檢查 | 結果 | 重現方式 |
|---|---|---|
| 單元測試(全部套件,含 `test_cost_model.py` + `test_finops.py`) | **1371 passed** | `.venv/bin/python -m pytest tests/ -q` |
| Harness 配置驗證,7 個 agent | **`RESULT: OK`** | `python deploy/validate_config.py --config agents/finops/harness.json` |
| 線上艦隊 | **7 個 harness READY** | 用 repo 內建 boto3 呼叫 `list_harnesses` |
| 正典模組有散佈路徑 | 印出 `would upload 4 contract files` | `python deploy/03_storage.py --region us-east-1 --account-id 123456789012 --dry-run` |

本次新增的每一道 guard 都做過 **mutation check**:逐一還原被斷言的行為,確認測試會
失敗 —— **357/357 反向控制**,308 個 mutation 斷言 357 組（guard, mutation）配對,runner 各印
一行 PASS。一個「有這行為也過、沒這行為也過」的測試,不是測試。

這個數字是刻意寫進句子裡的。「做過 mutation check」是一個形容詞,而形容詞不會過期:
刪掉一個 control、或是加了一道 guard 卻沒配 control,這句話照樣讀起來是真的。
`tests/test_docs_claims.py::test_the_documented_negative_control_count_matches_the_runner`
現在從 runner 自己的 `case(...)` 註冊推導出這兩個數字。

但推導出來的數字仍然只是一個數字,而這些 control 裡有一個並沒有通過。一次完整跑完的結果發現
**m189 UNCAUGHT** —— 那個 control 會把 eval prompt 裡 val split 那句話的優先順序拿掉,讓客戶
的驗收集和 10% val split 讀起來一樣有資格被評分。它的 guard 是在整個 bullet 裡搜 `/fall back/`,
而同一個 bullet 為了 `eval_only` 的 `model_artifact_uri` 寫著*「never fall back to the newest
artifact you can find in the bucket」* —— 一句和評分集完全無關的話,不管 prompt 怎麼寫都能讓那條
regex 通過。所以先前那個數字被報成全數通過,實際上有一隻 mutant 活著:這個數字能證明的是「每道
guard 都有一個 control」,不是「那個 control 殺得死」。現在 guard 只在提到 val split 的句子裡搜,
m189 也如它該有的樣子失敗了。任何能被「為別的理由寫下的句子」滿足的東西都不是 guard,而只有完整
跑一次才知道哪些是。

一個 control 還有第三種「回報 PASS 卻什麼都沒驗到」的方式,而下一次完整跑就在它被寫下的一個 case
之後抓到了:**m236 是用 `IndentationError` 殺掉它自己的 guard 的。** 它從一個 `try:` 區塊裡刪掉兩
行、留下空的 body,於是每一個 import console 的測試都在 collection 階段就 error —— 而 pytest 對
collection error 和斷言失敗回的都是 `1`,所以 runner 自己的檢查(`rc == PYTEST_TESTS_FAILED`)分不
出這兩者。唯一的痕跡是印出來那一行的殺法(`1 error in 0.28s`,而其他每個 case 都是 `1 failed`),
而沒有任何東西讀它。現在 runner 與
`test_every_negative_control_still_matches_the_code_it_mutates` 都會對每一個 `.py` 檔的 mutation 做
`compile()`,不能 parse 的就拒收 —— 只限 Python 原始碼,因為把 JSON 或 Markdown 的 parser 弄壞,
常常正是某個 control 要斷言的那個 break。一個 mutation 必須真的走到那段程式碼,才能證明 guard 在看它。

上面那些數字這次往上走,理由值得單獨說,因為它和平常的理由相反:**最新這幾個 control 裡有兩個之所以
存在,是因為原本的 guard 在替一個 bug 站崗。** eval prompt 對「沒有報區間的純量 gate」是用固定的
±0.05 band 判定的 —— 距門檻在這個距離之內(含剛好在門檻上)就算 borderline,要上報而不是自己下判斷。
console 那條分支上根本沒有 band,所以它把整個 band 都畫成 PASS,**包含剛好落在門檻上**那一點 ——
距離是 0、按規則是最 borderline 的位置;而且有兩條既有的斷言說這是對的。更關鍵的是:對線上 plan 真正
帶著的那道 gate 來說,band 本來就是錯的量尺 —— `format_validity ≥ 0.95` 的天花板是 1.0,於是**整個**
通過區間都落在 borderline band 裡面,任何過了門檻的值都不可能拿到決定性通過 —— 97 題裡 96 題合格,
是一次該上報的 escalation,而頁面把它畫成通過。正確的修法是給這個比例它應得的區間(97/97 → Wilson
下界 0.9619,決定性通過;96/97 → [0.9439, 0.9982],誠實的 borderline),所以 eval prompt 現在要求
每一個它回報的比例都要帶 `<metric>_ci_low/_ci_high` 與 `<family>_n`,band 只留給「沒有分母可以算區間」
的指標當 fallback —— 並加上一條:當 `bar + band` 觸到指標天花板時,agent 必須把這件算術講出來、上報一次。
一道把「現在的行為」寫進斷言裡的 guard,並不是「這個行為是對的」的證據。

最新的十二個控制講的是一個**狀態**，而不是一個數字。一個卡住的 run **可以被回答，也可以被看見
—— 但不能兩者兼得**：`checkpoint` 讓 run 保持可被回答卻不通知任何人，`escalate_human` 會通知卻讓
run 變成不可回答，而 eval gate 的 *borderline* 判決 —— 唯一一種人類回答能拍板的 gate 結論 —— 被導
向了後者。修法的一半是第三條通道（`page_human`：通知，同時讓 run 活著），另一半是一個 stage 的
page **絕對不能**結束這一輪，因為這個 invocation 正握著一個 `_ack_terminal` 不會結清的 task token：
`EvalGate` 會抱著它等滿 `TimeoutSeconds: 86400`，於是一個完全照 prompt 做事的 agent 會把自己的 run
掛住一整天。兩半都做過 mutation check，圍繞它們的每一個上下界也都有 —— m245 讓等待輪數在每個
Lambda 邊界重新計數（真實的等待必然跨好幾個，於是數字永遠讀作 1），m249 改成數存活的列、而不是讀
每一列自己的 `waiting_turn`（過了 driver 的 12 列上限之後，數列數連下界都不算，而且對最長的等待
低報最多），m250 讀最舊的列而不是最新的（那個提示就會剛好在等最久的長 run 上消失），m251 把
console 的前綴比對換成狀態完全相等（一個更詳細的終止狀態會畫出一個「driver 拒絕停放其答案」的提
示）。**操作者看不到的狀態，就是系統沒有的狀態** —— 這句話這個 repo 已經付了第三次錢。

**D13 —— 在排序之前取的窗口，就是雜湊順序上的窗口（十二個控制，m255-m266）。** console 裡每一個
列表都是先 `scan(Limit=N)` 再按時間排序，於是 `Limit` 砍掉的並不是最舊的列：Scan 的項目順序在文件
上就是未定義的，它砍掉的是剛好落在分頁邊界之後的那些。在 live `llmops-tasks` 表上量到（35 列，
`Limit=25`）：**最新的 25 個 consultation 有 6 個不在列表上**，其中一個狀態是 `error`，另一個是等
待人類簽名的 `drafting`，而 6 個更舊的被擺在它們的位置上。同一形狀還有四處，其中三處目前仍是潛伏
的：run 列表（超過 60 個 run 之後是任意窗口 —— 操作者剛啟動的那個 run 可能不在裡面，而找不到的 run
會被再啟動一次），`list_optimizations`（窗口取在 `opt-` 過濾之前，於是草稿讀起來像從未存在），成本
估算的 GSI fallback（docstring 承諾 newest-first，實際給雜湊順序），以及 `_timeline` —— 它的 events
那一半把預算花在**正向**，而 directives 那一半早就是反向的：同一個函式，兩個方向。最後這一項是值得
點名的交互作用：在 run 只有約 16 個事件時它無害，而 D12 把上限抬到約 150（`WAIT_ROW_CAP` 是每個
stage invocation 12 列），所以**是上一個修正把真實事件數推過了這一個的窗口**。控制盯住的正是「一個
數字」與「一個錯的數字」之間的差別：m261 用 `len == limit` 推斷截斷、而不是多讀一列（剛好 100 個事
件的 run 會聲稱有沒人讀到的歷史），m262 送出反向窗口卻不還原時間順序（於是前端 `slice(-25)` 畫的是
最新 100 筆裡最舊的 25 筆 —— 兩端都不對的窗口），m256/m257/m263/m264/m265 各自在「從查詢到畫面」
的路上丟掉一個截斷標記。一個不說自己被封頂的列表讀起來就是完整的，那是同一個缺陷往上一層。

**D14 —— 一個只到達七個 harness 中五個的修正、它「顯而易見」的修法會燒掉的 43 條記憶，
更早一次部署其實已經燒掉的 63 條，以及 9 條這個 repo 擁有的任何拼法都叫不出名字的
（二十二個控制，m267-m288）。** 共享 BYO memory 由 `deploy/04_wire_memory.py` 接線，而它的
harness 名單是手寫的：五個管線 worker。有兩個 harness 接在同一份 memory 上、卻不在名單裡，
於是 #83 的提取收緊 —— semantic `topK` 10 → 5、`relevanceScore` 0.2 → 0.6，也就是那個讓
「另一個 run 的事後檢討」不再以裸事實注入的修正 —— **從未到達它們**。2026-08-13 線上實測：
`llmops_finops` 與 `llmops_orchestrator` 仍停在 **10 / 0.2**，正是修正前那個設定，而名單上
五個都是 5 / 0.6。什麼都沒有失敗；那條通道只是一直保持它本來的鬆，而且偏偏是在兩個提示詞
**建立在記憶之上**的 agent 上（finops：「估算準確度只有在每次對帳的發現存活到下一次估算時
才會提升」；orchestrator：「你的記憶與專家們共享」）。而 `deploy/05_harnesses.py` 早就寫著
一句正好講這個失敗模式的註解 —— *「磁碟上沒有任何腳本點名的配置，就是一個安靜地從未存在過的
harness」* —— 這個教訓被寫在一支腳本裡，卻沒有被套用到它的兄弟腳本上。

顯而易見的那個修法是破壞性的，所以它是先被量出來、才被寫下來的。`actorId` 是每個 namespace
的**分區鍵**（`/users/{actorId}/facts`），而那兩個 harness 是由較舊的 `deploy/wire_memory.py`
接的，它的 `--actor-id` 吃的是完整 harness ID。線上實測：
`/users/llmops_finops-eDJtU9PvKh/facts` 有 **13** 條記錄、
`/users/llmops_orchestrator-GsIqHZ4viJ/facts` 有 **30** 條，而每一個裸名字的分區都是 **0**。
把名單推導出來、再讓腳本自己偏好的拼法勝出，會在一次呼叫裡拋棄全部 43 條 —— 而
`UpdateHarness` 兩種情況都回成功，於是那個終於套用提取修正的部署，就會同時是那個丟掉它存在
目的的部署。所以現在**已經上線的 `actorId` 勝過腳本想選的那一個**，要搬動它必須逐一指名
（`--repartition <harness>`），而且在 data plane 說得出代價之前那個搬動會直接拒絕 —— 一個
未知的記錄數讀起來和 0 完全一樣（m272）。控制把這兩半分開盯住：m270 讓重新部署改寫已上線的
`actorId`，m271 把 `--repartition` 變成全艦隊生效，m273 讓計數停在第一頁（正好對大到有意義的
分區低報最多），m275 把 semantic 通道鬆回 episodic 的設定 —— 而那個設定只對 episodic 安全，
因為 `{sessionId}` 把它限制在 agent 自己的 session 裡，而事實那一邊沒有任何東西限制它。本來
該抓到這一切的那道 guard 斷言的是 `wired == {data-prep, finetune, eval, deploy, monitor}`：
**它同意了那個遺漏**，和 console 那個 gate band 是同一個形狀。改成從配置推導之後，它立刻失敗，
並且點名了第三個沒人找過的缺口 —— orchestrator 的提示詞同樣沒有記憶優先序規則，finops 也沒有
（m276、m277），而這兩個正是會發表 rate card、以及對人類報價的 agent。

**接著，支撐那個修正的量測本身被發現量得太窄，而且窄在要命的方向上。** 當時只數了兩個
full-harness-ID 分區 —— 13 + 30 —— 因為只有那兩個 harness 還「指著」這樣的分區。把七個
都數過一遍：

| 分區 | 記錄數 | 已上線的 `actorId` |
|---|---|---|
| `/users/llmops_data_prep-KuSKXUaxyP/facts` | 2 | 裸名字 |
| `/users/llmops_finetune-xXl7jsACZO/facts` | **25** | 裸名字 |
| `/users/llmops_eval-iuIIs96fFM/facts` | **16** | 裸名字 |
| `/users/llmops_deploy-nLLNWairTc/facts` | **11** | 裸名字 |
| `/users/llmops_monitor-YCXC5hcXzu/facts` | **9** | 裸名字 |
| `/users/llmops_finops-eDJtU9PvKh/facts` | 13 | 就是這個分區 |
| `/users/llmops_orchestrator-GsIqHZ4viJ/facts` | 30 | 就是這個分區 |
| `/users/<每一個裸 harness 名字>/facts` | **0** | —— |

**semantic 一共 106 條，其中 63 條寫下它們的那個 agent 現在已經取不回來了** —— 而 episodic
通道以同樣的方式斷掉，五個 worker 的 `/episodes/<full id>` 底下還有 105 條。`llmops_monitor`
最新的那條孤兒記錄的日期是 **2026-08-08**，所以這次搬動只發生在幾天前，是這支腳本自己更早
一次執行做的，機制正好就是上面那一個。那兩個倖存的 harness 之所以倖存，**只**因為手寫名單漏掉
了它們：缺陷本身就是資料活下來的原因。所以「保留已上線 `actorId`」這道 guard 是真的，但來晚了
—— 它守住的是那 43 條；而沒有任何 API 能把記錄在 namespace 之間搬動，所以那 63 條是一份回報，
不是一次修復。

真正缺的東西比接線缺陷更小、更無聊：**從來沒有人去數另一種拼法**，於是「這個 agent 沒有記憶」
和「這個 agent 的記憶在 25 條之外的另一個分區」在部署日誌裡印出來一模一樣。現在每一次 attach
都會去讀那些掛在「它沒有被接上的任何 `actorId` 拼法」底下的分區，而控制逐句盯住這件事：m278
讓這份回報靜音，m279 拿掉 episodic namespace（於是只報損失較小的那一半），m281 只檢查完整
harness ID（對七個裡的六個都對，正好在值得抓的那個案例上瞎掉），m282 讓檢查只在有人要求
repartition 時才跑 —— 而那五個已經丟掉記錄的 harness 永遠不會有人去 repartition，m283 只數
第一頁，而 m280 把一個**沒被讀過**的分區重新變回看起來是空的，也正是這個等價讓 63 條記錄在
沒有任何一次失敗呼叫的情況下離開。

**而那道檢查還是少了一個假設。** 它比對的是**這個 repo 產得出來的**兩種拼法 —— 裸 harness
名字，和完整 harness ID。這份 memory 上 `ListActors` 回了 **16** 個 actor，其中兩個兩者都不是：
`monitor` 有 **3** 條 semantic 記錄、`monitor-agent` 有 **6** 條，而這兩個 `actorId` 在這個 repo
的任何檔案裡都找不到（較舊的 `deploy/wire_memory.py` 的 `--actor-id` 吃的是自由文字）。所以有
9 條記錄，對一道**自己的 docstring 聲稱涵蓋了「一個兩者都不是的 `actorId`」**的 guard 而言是
看不見的 —— 它涵蓋的是「**已上線的** id 是第三種拼法」，不是「存在第三個**分區**」。一份從某個
repo 的命名慣例寫出來的候選清單，不可能包含這個 repo 從來沒有過的拼法，所以這份掃描現在改成從
data plane 自己的列舉推導，每次執行一次：

| 檢查 | 推導來源 | 線上結果 |
|---|---|---|
| 逐 harness 的 `stranded_partitions` | 這個 repo 的兩種拼法 | 63 條 semantic + 105 條 episodic，可歸屬到某個 harness |
| memory 層級的 `unreachable_actors` | `ListActors` | **9 個孤兒 actor，72 條 semantic + 108 條 episodic = 180 條** |

兩者是重疊的、**不能相加** —— 前者說的是**哪一個 harness** 掉了分區，後者說的是這份 memory 上
到底有沒有東西成了孤兒。五個控制盯住它：m284 用這次執行自己的 attach 清單去建可達集合，於是
`--harness llmops_eval` 會把另外六個 harness 健康的分區宣告成掉了（七次裡有六次在喊狼來了的
警告，就是沒有人會讀的警告）；m285 對一份還不存在的 memory 回報孤兒數為零；m286 只數 semantic
通道，於是 actor `finops` —— 0 條 facts、1 條 episodic —— 讀起來是乾淨的；m287 只讀第一頁
actor，正好只在 memory 還小的時候是對的；m288 直接把呼叫從 `main()` 拿掉，也就是一道 guard
存在於 repo 裡、卻不在部署路徑上的那種情況。

### 兩次失敗比通過更有價值

**帳單讀取權被拒。** Auditor 回報*「已定價 SKU:0。未定價:全部」*,把既有 card 的
新鮮度標為**未知**而非假設其新鮮,**拒絕**呼叫 `update_rate_card`,並指名了缺少的
確切權限。

**S3 與自己的算術模組都被拒。** 它推導出一張**完整的 37-SKU rate card,`unpriced: []`**,
然後拒絕發表 —— 把自己的產出蓋上 `v1-DRAFT-noncanonical`,理由是 `fallback_static`
那一層就住在它拿不到的模組裡面,並把這件事寫在回報的第一行。

真正要命的失敗模式是:一張看起來很有信心、但下個月沒人能重現的 card。
**有人會依據這些數字批准一筆五位數的支出。** Fail-closed 在無人設計過的條件下守住了。

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
