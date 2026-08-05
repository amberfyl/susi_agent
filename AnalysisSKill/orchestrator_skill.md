# Harness Orchestrator Skill

## Purpose
負責 A 機總控（Harness Agent）流程編排、跨 skill 決策與狀態管理。

## Source of Truth
1. 使用者提供的平台型號（product_name）
2. target board 動態產生的 `susi_board_probe_report.txt`
3. `{project}-spec.json`
4. `config.db`

> `susi_board_probe_report.txt` 是執行時動態產物，不可假設預先存在。

## 圖片判讀引擎策略（已拍板）
- 圖片判讀 gate 一律使用 Hermes `vision_analyze`（線上模型直接看像素）。
- 不使用 offline OCR engine（不依賴 tesseract/rapidocr/paddleocr 等本地 OCR 安裝）。
- 圖片輸入範圍固定包含：`bios*.png` + `circus*.png`。
- 若判讀證據不足，維持既有 pending/ambiguous status 流程，不做硬推。

## Master Flow (v1)
1. 取得平台型號
2. 觸發 target board probe，收回 `susi_board_probe_report.txt`
3. 檢查 probe/spec 可用的 chip 線索（不寫死 EC-only）
   - 若 `EC version` 有值：優先走 `platform + EC chipname` query flow
   - 若平台無 EC 或 EC 線索不足：允許改走 `platform + PCH/SuperIO chipname` query flow
   - 若三者皆無可用 chipname：`PENDING_NO_CHIP_KEY`（暫不進 DB query）
4. 依當前可用鍵（EC/PCH/SuperIO）呼叫 `candidate_query_skill`
5. 呼叫 `diagram_filter_skill` 做 BIOS/電路圖過濾
6. 呼叫 `config_builder_skill` 產生自動化測試 cfg
7. 收集結果交給 `verdict_report_skill`
8. 成功則回寫考古 DB；失敗則回歸校正（不直接覆蓋 DB）

## 14-Section 單一真相（Orchestrator 固定清單）
- SMBus
- I2C
- VGA.Backlight
- VGA.Brightness
- HWM.Voltage
- HWM.Current
- HWM.Temperature
- HWM.Fan
- HWM.Fan.Control
- HWM.CaseOpen
- WDT
- GPIO
- StorageArea
- ThermalProtect

## 14-Section 執行與產物規則
- 每輪總控必須對上述 14 個 section 全部完成「評估流程」並產出 status（不可跳過任何 section）。
- `每個 section 都要有 status` 不等於 `每個 section 都必須產生 ini`。
- 僅當 section 內容存在至少一個 non-empty key/value 時，才輸出 `{project}_{section}.ini`。
- 若 section 無值/無內容，標記 `SKIPPED_EMPTY_SECTION`，且不輸出該 section ini。
- 對 machine B 的交付物是「有內容的 section ini 集合」；空 section 只保留於流程狀態記錄中。

## Probe 判讀用途（除 status 之外）
- `status`：確認該項是否可讀/可用（SUCCESS vs UNSUPPORTED/ERR）。
- `string`（如 `BOARD_NAME_STR` / `BOARD_BIOS_REVISION_STR` / `BOARD_EC_FW_STR`）：
  - 用於 Information 回填與來源優先序裁決（probe > BIOS > PDF）。
  - 用於 query 前置 gate（例如 EC version 有效性）。
- `value`（如 HWM 溫度/電壓/風扇即時值）：
  - 可與 BIOS 可見項目做交叉核對，判讀 query 候選是否過量或缺項。
  - 作為後續 machine 驗證的 baseline/reference（容差比對）。
- 邊界：probe 的數值/字串可做「項目存在性與合理性」判讀；不可直接推導最終 channel/hwid/ioport 編碼。

## Information 回填契約（spec.json）
- orchestrator 必須確保 `{project}-spec.json` 具備：
  - `information.PlatformVersion`
  - `information.BIOSVersion`
  - `information.ECVersion`
- 建議保留來源追蹤欄位（可選）：
  - `information.source.PlatformVersion`
  - `information.source.BIOSVersion`
  - `information.source.ECVersion`
- 欄位型別統一為 string（缺值用空字串 `""`，不可用 null）。

## Information 回填優先序（使用者拍板）
- Priority 1（最高）: probe report (`susi_board_probe_report.txt`)
- Priority 2: BIOS
- Priority 3（最低）: PDF/form analysis

### 回填判定細則
- 高優先值存在時，不可被低優先覆蓋。
- `[ERR]`、缺值、明顯 placeholder 視為無效值，不可覆蓋既有有效值。
- 可正規化但不可改語意（例如去除 BIOS 版本外層括號）。
- 發生來源衝突時：採高優先值，低優先值僅記錄於 trace/source，不寫入主值。

## Information 回填時機（流程位置）
- 在 Step 2（probe 回收）之後、Step 4（candidate_query）之前執行。
- 回填完成後，先落盤更新 `{project}-spec.json`，後續 skill 一律讀取回填後版本。

## Information 回填責任邊界
- orchestrator：負責來源整合、優先序決策、衝突處理、寫回 spec。
- `extract_pdf.py`：只做 PDF 萃取，不做 probe/BIOS 優先序決策。
- `understand.py`：只保證 spec schema 可承載 `information`，不做總控回填決策。
- `query_config_db.py`：只做 deterministic DB 查詢，不混入資料融合邏輯。

## HWM.Voltage（EC 流程）融合與回填規則

### 適用條件
- 僅限 EC 情況（由 orchestrator gate 判定）。
- 已有 `(platform_name, chip_name)` 查得的候選資料，且可取得 `HWM.Voltage.Channels` 對照表。

### 固定模板（初始輸出）
- `HWM.Voltage` 每列固定尾段：
  - `[IOPort/Device Address],[option],[R1],[R2],[Name],[Offset] = 0,0x80000000,0,0,,0`
  - 注意：`Name` 留空必須保留雙逗號 `,,`，不可省略。
- `HW` 欄位：由 `(platform_name, chip_name)` 從 DB 取得 `hardware_id` 回填。
- `Channel` 欄位：依候選列而定（非固定值）。

### Name(alias) 回填時機
- 初始模板生成時 `Name` 一律留空。
- 後處理階段再回填 alias（BIOS/probe/mapping 融合後）。

### 消歧規則（重點）
- BIOS 可直接明確區分時，直接回填：
  - 例如 `+12V -> V120`、`VBAT -> VBAT`。
- BIOS 名稱有歧義時（例：`+5V` 可能是 5V 或 5VSB；`+3.3V` 可能是 3V3 或 3VSB）：
  1. 以該列 `Channel` 查 `HWM.Voltage.Channels.channel_id`
  2. 取得對應 `report_name`（對齊 probe `HWM_VOLTAGE_*`）
  3. 依 `report_name` 決定 alias 回填目標列
- probe 為 `SUCCESS` 且 mapping 命中時，`channel_id` 以 DB mapping 為預設依據。

### 失配處理
- 若 BIOS 名稱、probe 狀態、mapping 三者無法收斂：
  - 標記 `AMBIGUOUS_HWM_VOLTAGE_ALIAS`
  - 保留模板列，不做硬判。
- 若出現同一 `channel_id` 對應多個 item（`CHANNEL_DUPLICATE`）：
  - 命中 SuperIO 圖證規則適用條件時，交由 `diagram_filter_skill`（R-014/R-015）做判圖修正。
  - 非 R-014/R-015 適用情境時，不自動修正，標記 `PENDING_CHANNEL_DUPLICATE_NEED_EVIDENCE`。

## What MUST stay in Orchestrator
- probe 觸發與回收
- EC gate（是否可走 DB query）
- 技能呼叫順序與 retry/fallback
- BLOCKED/PENDING 決策
- DB 回寫策略（何時可寫回）

## What MUST NOT be pushed into child skills
- 各 skill 不可自行重跑 probe
- 各 skill 不可自行決定是否回寫 DB
- 各 skill 不可覆寫總控狀態機

## Common Status Codes
- `READY_FOR_QUERY`
- `PENDING_NO_EC_RULE`
- `NO_SUCH_PRODUCT_CHIP`
- `SECTION_EMPTY`
- `FOUND`
- `AMBIGUOUS_BIOS_ITEMS`
- `GPIO_SPEC_DB_MISMATCH`
- `BLOCKED_PARAMETER`
- `BLOCKED_FIXTURE`
- `FAIL_ALL_CANDIDATES`
- `INFO_BACKFILLED`
- `INFO_CONFLICT_RESOLVED`
- `INFO_MISSING_REQUIRED`
