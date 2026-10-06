# Verdict Report Skill

## 1. Purpose
彙整 Machine-B 驗證結果，輸出可回饋總控的結論與建議。

## 2. Inputs
1. 本次 run 新產生的 section report JSON（`<report_prefix>_<timestamp>.json`）
2. `run_machineB_full_validation.py` 產生的 manifest 與 summary
3. candidate trace（query/filter/build chain）

## 3. 目前判定範圍（此階段）
1. 完整驗證模型有六層（L1–L6），但**此階段只以 SW API 層（L1–L4）為正式判定**：

   | Layer | 意義 | 歸屬 |
   |---|---|---|
   | L1_configuration | INI/JSON 設定與 channel 對應正確 | SW |
   | L2_capability | GetCaps 等能力查詢成功 | SW |
   | L3_api | SUSI API 呼叫回傳成功 | SW |
   | L4_readback | 讀回值有效 | SW |
   | L5_functional | 實體功能（需 fixture/刺激） | DQA（此階段不作完成條件） |
   | L6_recovery | 功能測試後復原 | DQA（此階段不作完成條件） |

2. **第一階段 = SW API 讀寫通道是否通**（使用者 2026-10-05 拍板）：有 Set API 的功能一律做「讀原值 → Set → 讀回比對 → 還原原值」；只有讀取 API 的功能（HWM.Voltage、HWM.Temperature：SDK 無 Set）讀到有效值即可。
3. section 判定（程式：`run_machineB_full_validation.py` 的 `_normalize_report_verdict`）：
   - `PASS`：L1–L4 全部實際通過（`PASS`/`N_A`/`NOT_REQUIRED`）。缺治具、缺刺激、DQA 未做（L5）**不影響**；`FAIL_FIXTURE`/`FAIL_FUNCTIONAL` 也不影響。
   - 例外：`VGA.Backlight`、`VGA.Brightness`、`GPIO`、`StorageArea` 的 L5 就是「寫入 → 讀回 → 還原」本身，算第一階段：L5 失敗判 `FAIL`，L5 沒跑判 `CONDITIONAL`。
   - `FAIL`：L1–L4 任一 `FAIL*`、L6 還原失敗、或其他 `FAIL_*` result。
   - `CONDITIONAL`：L1–L4 有 `PENDING`/`CONDITIONAL`，表示 API 通道**沒有被實際走過**（例：WDT 未執行 Start；SMBus 掃描無任何裝置回應；I2C 不支援 SetFrequency 且無裝置回應）。
   - `sw_verdict`（`Apply-VerdictPolicy`）同理：全部通過才是 `PASS_SW`，有未執行的層為 `PENDING_SW`。
4. 寫入值一律選「與原值不同、但無害」的值，讀回才有意義：ThermalProtect 只改觸發溫度（caps 範圍內；原本有保護動作時只往上調，調不了就不改並判 `CONDITIONAL`），SourceId/EventType 不動；WDT reset time 等於原值時改用最大值減一個單位。
5. 預設開啟的寫入測試（皆會還原）：`HWM.Fan.Control -AllowControl`、`ThermalProtect -EnableSetConfigTest`、`VGA.Backlight`/`VGA.Brightness`/`GPIO -EnableFunctionalTest`、`StorageArea -EnableWriteTest`。I2C 做頻率 Set→讀回→還原；SMBus 做唯讀 ReceiveByte 掃描（**不寫入**，避免寫壞 SPD）。WDT 做 Start（reset time 用硬體最大值、event type=NONE）→ 讀回 reset time → Trigger → 立即 Stop（`finally` 內最多重試 3 次），不等逾時、不重開機；Start 回 `SUSI_STATUS_RUNNING` 表示 WDT 已被其他程式使用，完全不動並判 `CONDITIONAL`；Stop 失敗判 `FAIL` 並警告目標機可能重開。
   I2C 頻率測試由 `-EnableSetTest`、WDT Start/Stop 由 `-EnableStartStopTest` 控制，同樣預設開啟。
   預設不開：`SMBus -EnableFixtureTest`（需治具）、`HWM.Fan -EnableStimulus`（需刺激源）。
   唯讀模式：`run_machineB_full_validation.py --no-write-tests` 不傳任何開關，所有寫入都不做；有 Set API 但沒測到的 section 判 `CONDITIONAL`，summary 的 Scope 行標示 `READ-ONLY`。
6. 治具、硬體刺激、DQA 相關的待辦，一律寫在 summary 的「Phase 2 recommendations」段落，不降低第一階段判定。
7. 判定邏輯以程式為準：`targetB_task/machineB_validation/common_susi.ps1` 的 `Apply-VerdictPolicy`。

## 4. 結果判讀規則
1. **report JSON 為準**：section 結論看 report JSON；process exit code 只當診斷證據。
2. **只用本次報告**：只採本次 run 新產生的 report；舊報告不可當作修正後的結果。
3. **多通道 section**：任一 required channel 失敗時整體可為 `FAIL`，但必須寫明 `PARTIAL_FAIL` 或全通道失敗，並列出成功/失敗通道與 status code；不可把 `Backlight1=PASS、Backlight2=FAIL` 寫成「VGA.Backlight failed」。
4. **讀回成功 ≠ 功能確認**：API 讀回成功只證明 SW 路徑可用，不代表實體功能已驗證（例：GPIO 讀寫回不等於電氣 loopback；Brightness 讀到值不等於亮度真的有變）。
5. **`SUSI_STATUS_UNSUPPORTED`（`0xFFFFFCFF`）**：表示目前 runtime 不支援該 API；先確認部署的 INI 與 driver reload 狀態，不可直接判定 mapping 錯誤。
6. **GPIO 能力遮罩只缺部分 bit**（例：要求 `0x0000FFFF`，回報 `0x00009FFF`）：
   - 代表 route 正確：channel、IOPort、option 都對，所以 GetCaps 成功、其他 bit 都支援。
   - 缺的 bit 對應的 `GPIOnn`（bit n = `GPIOnn`）**是 group/pin 錯了**，也就是電路圖追線錯誤，最常見是追到隔壁腳。
   - 處理方式：**在報告中指出可疑腳位即可，不自動重新截圖追線**。每支列出：`GPIOnn`、外部訊號（例：`EC_P2_GPIO5`）、目前的 function label 與 group/pin，並註明「可能追錯腳（常見為隔壁腳），請人工確認」。
   - **禁止**：裁掉這些 GPIO、改 route／IOPort／option、判定為硬體不支援，或自行重新追線改值。
7. **execution_status**：
   - `COMPLETED`：runner 正常完成並取得報告（verdict 另看 report）。
   - `BLOCKED_DEPENDENCY`：依賴的 section 失敗而未執行（例：`HWM.Fan` 失敗 → `HWM.Fan.Control` 不跑）。
   - `ORCHESTRATOR_ERROR`：報告缺失/過期/格式錯誤或 runner crash，屬基礎設施問題，不算該 section 完成。
8. **整體結論**：個別 section 失敗只記入 summary；流程與 rollback 安全完成即回報 `completed` + summary 路徑（同 orchestrator 11.3）。

## 5. Outputs
1. per-section verdict（`sw_verdict` 為主，附 `dqa_verdict`、失敗通道與 status code）
2. overall summary
3. fail taxonomy（`FAIL_API` / `FAIL_READBACK` / `BLOCKED_*` / `ORCHESTRATOR_ERROR` 等）
4. 回寫建議（可回寫 / 不可回寫）

## 6. DB Feedback Rule
1. 成功案例：允許回寫考古 DB（由 orchestrator 執行寫回）。
2. 失敗案例：產生回歸校正建議，不直接改 DB。

## 7. Scope Boundary
1. 不直接操作 DB 寫入。
2. 不改 orchestrator 狀態機。
3. 不重寫程式已實作的契約：section JSON 與 API ID 對應由 `build_machineB_section_configs.py` 負責；各 section 的 API 呼叫、安全開關與報告欄位由 `targetB_task/machineB_validation/*.ps1` 負責；執行順序、部署與報告收集由 `run_machineB_full_validation.py` 負責。
