# 案例研究 —— 讓 agent 獨立跑完一條 LLMOps 管線，到底需要什麼

[English](CASE_STUDY.md) · [架構](ARCHITECTURE.zh-TW.md) · [測試結果](TEST_RESULTS.zh-TW.md)

本文中的一切都發生在真實 AWS 帳號上，記錄於
`deploy/evidence/VERIFICATION_phase*.md`。沒有一段是 demo 逐字稿；失敗都保留在內，
因為失敗正是重點。

## 目標

在一個完整生命週期內替代人類 LLMOps 工程師：teacher 大模型（Bedrock 上的
DeepSeek-R1）生成訓練數據，student 小模型（Qwen3-1.7B）在 SageMaker 上做 QLoRA
微調，對照質量門檻評估，部署到 endpoint，冒煙測試，再回收 —— **只有 agent 呼叫
`escalate_human` 時才需要人類介入**。不是「一個會建議命令的助手」，而是六個
真正值班背 pager 的 agent。

**本文全篇的「六」指的是 v1 當時的 fleet。** FinOps 審計員（`llmops_finops`）在 Phase 6
之後才加入，今天是七個 —— 所以 README 寫七、這份記錄寫六，各自對自己那個時間點都是對的。
把它改成七會讓本文與它自己引用的證據互相矛盾（`VERIFICATION_phase5.md`：「All six
harnesses currently run Opus 5」），並且會宣稱審計員參與了一個它其實不在場的建置。

證明這一切的總帳單 —— 六個 agent、一個訓練完成的模型、一個部署又回收的
endpoint、五輪端到端迭代 —— 約 **$12–15**，大約等於一位人類工程師一小時。

## 論點：三層疊加，而不是單一模型

最清晰的單一事件來自 Phase 3。finetune agent 被指派啟動 QLoRA 訓練作業；下載訓練
腳本時遭遇 S3 403。全程無人干預，它：探測兩個 prefix 並*歸納*出自己的 IAM role 是
prefix 範圍限定（`runs/*` 可讀、`code/*` 不可讀），而不是無腦重試；按優先級搜索
備選（本地 workspace → skill 目錄 → 歷史作業的 sourcedir）；發現 sandbox 沒有
`tar`，改用 Python `tarfile` 重建 `sourcedir.tar.gz`；上傳到自己**有**寫權限的
prefix；提交作業；確認 `InProgress`；然後呼叫 `job_launched` 釋放 session。
訓練首次無人嘗試即啟動。

這種行為不屬於任何單一組件。它是三層能力的乘積：

1. **模型能力** —— 每次失敗都產出有設計的假設（兩點權限探測 →「role 是 prefix
   限定」）、有先驗的搜索排序、零猶豫的工具替換。弱模型會重試同一個 403 或直接放棄。
2. **真實執行環境** —— AgentCore microVM（shell、文件系統、code interpreter）讓
   探測 S3、構建 tarball、調 SageMaker 成為真實動作，而不是聊天窗口裡的建議。
3. **工程化的授權** —— 每個任務 prompt 都明確授予自我修復預算（「診斷、修正、
   重試 —— 最多 3 次；然後 `escalate_human`」），掛載的 skills 提供正確修復的
   領域形態。沒有這個授權的保守對齊模型，在第一個 403 就會停下來問人。

拿掉任何一層，同一事件的結局就是 `escalate_human: S3 403`，而不是一個跑起來的
訓練作業。

## 補救連環戰 —— 6 次訓練迭代

讓一個訓練作業跑到 `Completed` 用了六次迭代，每次失敗都是自我診斷（Phase 3 證據）：

| # | 失敗 | 診斷 |
|---|---|---|
| 0 | `ImportError: torch>=2.1.1` | 2023 年的 HF DLC 對 Qwen3 太舊 |
| 1 | CUDA OOM，第 0 步就差 7.31 GiB | 151k 詞表 × 14k 上下文 → fp32 logits ≈ 8 GiB，超出 24 GB A10G → Liger fused CE（棄用截斷方案，為保住最長的已驗證軌跡） |
| 2 | liger 需要 transformers ≥ 4.52 | pin 下限衝突 → 提高 pin |
| 3 | transformers *內部*拋 `NameError: torch` | 靜默降級：torch 低於其下限時 transformers 能正常 import 卻視 torch 為不存在 → 換 torch 2.6 DLC |
| 4 | 要求 bitsandbytes ≥ 0.46.1 | 精確 pin 是病根而非症狀 → **策略轉變**：floors-only requirements |
| 5 | —— | **Completed**（計費 431 秒；train_loss 0.5013、eval_loss 0.5199） |

過程紀律全程未破：每次迭代恰好只改一個變量，附書面理由；完整
`remediation_history` 在 manifest 中只追加不改寫。第 1–3 次由 finetune agent 在其
3 次診斷預算內自我診斷；第 4–5 次由指揮家 triage —— 升級協議被遵守，而非繞過。
而當一次 Bedrock 短暫故障使 agent 不可達時，確定性主幹自己提交了最後那個作業
（標記 `launched_by: orchestrator-fallback-bedrock-5xx`）。

回報在 Phase 5 兌現：finetune agent 下一次發起訓練作業**首次嘗試即成功**，用的
正是它記入共享 Memory 的 floors-only + torch-2.6 配方。六次失敗變成了一條
組織級學習。

## 誠實的門檻 —— 模型失敗了，這正是證明

Phase 4 是多數宣傳文案會藏起來的一章。管線把蒸餾出的 student 部署了五個 endpoint
版本（四個不同根因：配置環境變量被當成模型 URL 解析、legacy handler 路由、
訓練/服務兩側 transformers 版本錯位、打包佈局失誤 —— 最後把 `serving.properties`
放到 tarball 根目錄，InService）。冒煙測試通過：student 經 HTTPS 正確解答了旋轉
任務。

然後質量門檻在 16 個留出任務上跑了兩次 —— 模型**兩次都是 0/16**。eval agent 沒有
粉飾。它自跑對照（寬鬆重掃仍是 0；對照 prompt 走完全相同路徑返回格式良好的 grid，
證明管線本身沒問題），並給出解釋一切的診斷：**`closed_think_rate` 0%** —— 沒有任何
輸出閉合過它的 `<think>` 塊；生成中位數 5,831 tokens；16 條中 12 條撞上上下文
上限。student 學會了*開始*推理，卻從未學會*收斂*：這正是用 6 條軌跡訓練 ——
遠低於 ARC 推理遷移到 1.7B 模型的地板 —— 所記載的後果。

判決站住了。沒有任何東西被「修」成通過。而在 Phase 5 的無人運行中，這份誠實還在
複利：當門檻在 2 樣本 mini-run 上失敗（`FAIL_CLOSED_NO_INPUT` —— 那個規模不存在
質量信號），狀態機武裝了補救迴路，而 finetune agent *拒絕了前提本身*：
`REMEDIATE_PREMISE_INVALID — no quality signal to remediate` → `escalate_human`。
它拒絕在不可修復的前提上燒掉迭代預算。一個自主工程平台的價值，恰好等於它的門檻
有多難被說服放行 —— 這道門檻連自己建造者的模型都擋住了。

門檻在機制上也是 fail-closed：一個實測缺陷（agent 發出 `gate_passed: null` 被舊
默認值晉升為通過）被修復為只有字面 `true` 才算通過，並加了回歸測試。

## Agent 自發的韌性升級

沒有人要求這些；agent 在運行中途遭遇惡劣環境，並圍繞它重新設計（Phase 2 main 證據）：

- **逐任務 S3 checkpoint** —— 一次 microVM 回收毀掉了 9 個只存在本地的結果。
  agent 自行改為把每個任務結果 checkpoint 到 S3，並把它記入 manifest 作為標準
  實踐。下次輪詢時 24 個 checkpoint 中 23 個已在 S3；改動之後零工作損失。
- **冪等並行 worker** —— sandbox 沒有 `ps`/`pgrep` 且禁用 `kill`，進程管理無從
  談起。agent 把 worker 改成「做過即跳過」，而非受管進程。
- **自我診斷 token 截斷** —— pilot 中 8 條輸出有 7 條格式驗證失敗。agent 讀
  `stop_reason`，判定失敗是截斷而非錯答，建議提高 `maxTokens`（8k → 32k），
  格式有效率升到 8/8。

## 供應商配額 failover

Phase 5 確立了一個硬性運維事實：模型供應商的限流連 AWS 內部帳號都約束，而六
harness 平台本身就是 token 洪流製造機。單日內 Fable 5 的 5xx 爆發重現約 12 次 ——
從不以顯式限流出現，總是 `InternalServerException`/`ServiceUnavailableException`，
而同一模型的單發直測卻成功。應對是把 failover 提升為設計層（AGENTS.md）：每個
harness 一條同家族後備鏈（Fable 5 → Opus 5，零 prompt 改動，經 `UpdateHarness`
約 15 秒熱切換，session 存活）、混合模型配置分散配額壓力、driver 層對 5xx 特徵
自動切換。

## GPU 容量是另一條互不相干的隊列

上一節的供應商配額限制 harness「思考」的速度；GPU 容量限制訓練能否「開始」，
而且行為完全不同：對 `ml.g6.2xlarge` 發 `CreateTrainingJob` 會被接受，進入
`InProgress` / `Pending`，訊息是 `"Training job waiting for capacity"`，然後就
只是等 —— 可能幾分鐘，也可能幾小時，沒有錯誤可以反應，也讀不到排隊位置。v2
這次的 g6 與 g5 兩個嘗試都 Pending 超過 20 分鐘。

有兩個事實讓這件事可以低成本繞過。**Pending 期間不計費** —— 計費從 `Starting`
（真正分配到機器）開始。而 SageMaker 訓練配額是**按機型分開**的：本帳號對
`ml.g5.{xlarge…12xlarge}` 與 `ml.g6.{xlarge…12xlarge}` 每一種各有獨立的 1 個限額。
這些是互相獨立的抽獎，所以把同一個 job 排進 N 個 pool 等於買 N 張彩券，而在它們
都還在等的時候完全免費。

代價是彩券可能**同時中獎**，而兩個各 9 小時、每小時約 $1.2–2 的 QLoRA 跑出同一個
結果卻付兩倍錢。`pipeline/training/capacity_race_guard.sh` 每 60 秒輪詢一次，停掉
除了第一個離開隊列者以外的所有候選。它的不變量值得寫清楚，因為搞反的代價是金錢
而不只是「不對」：

- 只要過了 `Pending`（`Starting`/`Downloading`/`Training`/`Uploading`）就算在跑 ——
  若等到 `Training` 才算，會讓 `Downloading` 中的 job 悄悄計費。
- `describe-training-job` 失敗（限流、暫時性錯誤）**跳過該輪**，而不是判為「沒在
  跑」。把 API 錯誤當成沒在跑，正是 guard 停掉真正在訓練的那個 job 的原因。
- 永不停掉最後一個候選，也永不停掉贏家。

每個參賽者需要**自己的 checkpoint 前綴**。共用一個的話，輸家的部分 checkpoint 會
被贏家 resume，兩次運行就被靜默混在一起。

`tests/test_capacity_race_guard.sh` 用 `PATH` 上 stub 掉的 `aws` 驗證以上全部 ——
不連網、不需帳號 —— 覆蓋 10 種狀態組合，包含同時中獎、手足已被停掉、以及
describe 失敗這幾種。它立刻證明了自己的價值：抓到一個 `${losers[@]}` 在 `set -u`
下的 unbound variable 崩潰，而它恰好只在「贏家已經沒有輸家可停」時觸發 ——
也就是唯一必須正常運作的那種情況。

## v1 證明了什麼 —— v2 要做什麼

**已證明：** 完整的自主閉環是真的。觸發 → 計劃 → 生成 → 整理 → 訓練
（launch-and-release）→ 評估 → 部署 → 冒煙 → 回收，全程無人，誠實終態，經共享
Memory 的跨運行學習，明確預算內的自我補救，以及在 —— 且僅在 —— 前提要求時的
升級人類。每條鏈路皆經實測驗證，總計約 $12–15。

**誠實設計下未證明的：** 一個能*通過*門檻的蒸餾 student。6 條訓練軌跡教不會
1.7B 模型在 ARC 級推理上收斂；Phase 4 證據說得明白，補救階梯的下一級是設計變更，
不是重跑。

**v2 —— 已記錄的實驗：** 從兩個軸攻擊遷移地板。*Code-as-reasoning*：把 teacher 的
解法蒸餾成可執行的變換程序，而非自由形式的 `<think>` 散文 —— 程序要麼復現輸出
grid 要麼不能，這讓每條訓練樣本都可驗證、每條軌跡在構造上就收斂（`closed_think_rate:
0%` 度量的正是這個屬性的缺席）。*規模化增廣*：在所有者先前 ARC 工作的 849 條
triplet 數據集基礎上，以系統性變換擴充，用一個高於遷移地板的數據集取代 6 軌跡
數據集。平台側不需要新工程：數據規模只是 manifest 的一個參數，而第一次全規模
通關就是 v2 實驗的開場。
