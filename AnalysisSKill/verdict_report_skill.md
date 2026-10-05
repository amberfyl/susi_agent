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

2. `sw_verdict`：L1–L4 任一 `FAIL*` → `FAIL_SW`（exit code 1），否則 `PASS_SW`（exit code 0）。
3. `dqa_verdict`（`N_A_DQA` / `PENDING_DQA` / `PASS_DQA` / `FAIL_DQA`）保留在報告中供參考；`CONDITIONAL`、`PENDING_DQA` 不影響 SW 結論，也不視為失敗。
4. `--all` 預設不開任何控制/fixture/寫入開關，以下狀態代表「刻意沒跑實體測試」，不是失敗，SW 結論仍看 `sw_verdict`：
   - `result=CONDITIONAL`：API 讀取完成，但沒有實體刺激/fixture（所有 runner 都可能出現）。
   - `result=BLOCKED_SAFETY`：`HWM.Fan.Control` 未帶 `-AllowControl`，控制測試未執行。
   - `reason` 含 `BLOCKED_FIXTURE`（SMBus）或 `L5_functional=PENDING_FIXTURE`（I2C）：未帶 fixture 開關，傳輸測試未執行。
5. 判定邏輯以程式為準：`targetB_task/machineB_validation/common_susi.ps1` 的 `Apply-VerdictPolicy`。

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
