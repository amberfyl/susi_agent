# Candidate Query Skill

## 1. Purpose
以 `(product_name, chip_name)` 對 `config_new.db` 做粗匹配，輸出 section 候選。
- 本 skill 只負責 deterministic DB query；不判讀 probe/BIOS/電路圖，不做最終候選勝出判定。
- DB schema、欄位與資料關係見 `querydb.md`；跨來源融合、gate 與最終輸出規則見 `orchestrator_skill.md`。

## 2. Inputs
- `product_name`（例：SOM）
- `chip_name`（由 orchestrator 提供，已正規化為 `ProductChip.chip_name` 的 canonical key）
- `section`（目標 section table 名稱，例如 `WDT` / `VGA.Backlight`）
- `config_new.db` path

## 3. Preconditions
- 是否執行 query 由 orchestrator gate 決定；本 skill 不自行判讀 probe report。
- orchestrator 需先提供 `is_ec` 判定結果（來自 probe `BOARD_EC_FW_STR`）。`is_ec` 只影響 query 之後的處理路徑，不影響 query 本身：`is_ec=True/False` 都照常執行 deterministic query。
- 若 probe 缺失導致 `is_ec` 未定，可先查 DB；但裁決需標記 pending，不能硬判。

## 4. Chip key（資料面）
- `ProductChip` 可能存在多種配對：`platform + EC chip`、`platform + PCH chip`、`platform + SuperIO chip`。
- 本 skill 只照 orchestrator 傳入的 `(product_name, chip_name)` 查詢，不自行改鍵、不做簡寫補全或擴大猜測。
  - 簡寫正規化（例：`6126` -> `NCT6126D`）與複合晶片 section routing（例：`EIO-300 / NCT6694B`）由 orchestrator 在 query 前完成。
  - 查不到時回 `NO_SUCH_PRODUCT_CHIP`，不以近似 chip name 重試。

## 5. Query Rule
1. 查 `ProductChip`：`UNIQUE(product_name, chip_name)`，取得 `hardware_id`。
2. 以 `section.hardware_id = ProductChip.hardware_id` 查目標 section table（舊 `prod_chip_id` join 已淘汰）。
3. 只回傳 DB 中實際命中的 rows；未命中維持 `SECTION_EMPTY`，不做 fallback。

### 5.1 DB rows = maximum set（通用原則）
- section table 的 rows 代表該 `(product_name, chip_name)` 可能的**最大拓樸**，不等於某塊板子實際啟用的 channel。
- 依 full probe 裁切 channel 數量屬 generate/orchestrator 職責（目前適用 `I2C`、`VGA.Brightness`、`VGA.Backlight`）；query 層一律回傳完整命中 rows。
- 不可因某塊板子沒有某條 bus/channel 就修改或刪除 DB rows。

### 5.2 I2C
- 只有 `{project}-spec.json` 的 `features.i2c == true` 才進行 I2C query/generate；false 或缺值時維持 `SECTION_EMPTY`，不執行 query。
- `I2C.id` 是 DB row primary key，不是 INI channel，也不是 full probe 的 I2C id。
- `io_port`、`options` 一律取命中的 DB row。
- 值的來源只有 DB：`channel_id`、`io_port`、`options` 全部取命中的 DB rows。DB query 沒有 I2C rows 即代表該平台沒有 I2C（`SECTION_EMPTY`），不由 probe 補值或生出 channel。
- generate 階段再以 full probe（`*susi_board_probe_report.txt`）實際成功的 I2C bus 過濾多餘的 DB rows（`report_name` 對齊 probe bus name）；probe 只做刪減，不做新增。
- INI channel 編碼（使用者拍板，與 DB `channel_id` 一致）：`channel = 0x80000000 + Id`，INI key 為 `Channel(Id+1)`，最多到 `Channel5`：

  | Probe bus | Id | INI key | channel |
  |---|---|---|---|
  | `I2C_EXTERNAL` | 0 | `Channel1` | `0x80000000` |
  | `I2C_OEM0` | 1 | `Channel2` | `0x80000001` |
  | `I2C_OEM1` | 2 | `Channel3` | `0x80000002` |
  | `I2C_OEM2` | 3 | `Channel4` | `0x80000003` |

- query 層不解析 probe，只回傳完整命中 rows；probe 過濾由 generate/orchestrator 執行。

### 5.3 HWM.Fan
- Fan channel rows 只查 hardware-specific 的 `HWM.Fan` table。已退役的 `HWM.Fan.Defaults` / `HWM.Fan.Channels` 不是 query 目標，不得新增或恢復這兩個 CLI target。
- `load_hwm_fan_defaults()` 只提供共用 template 相容值（讀現行 `HWM.Fan` rows），不是另一張 defaults table。
- `HWM.Fan.Control` 沒有獨立 table，以 `HWM.Fan` rows 為基礎。
- DB rows 只是 baseline；FAN / FAN.Control 的最終 mapping 由 fan pairing 圖證決定（R-019 + orchestrator FAN 配對契約），不可把 DB rows 當作最終拓樸判定。

### 5.4 HWM.Voltage / HWM.Temperature
- query 欄位：`report_name`（對齊 probe `HWM_VOLTAGE_*` / `HWM_TEMP_*` 列舉名稱）、`channel_name`、`channel_id`（對應 INI `Channel` 欄位）。
- table 可不覆蓋 probe 全列舉；未收錄者視為例外，不代表錯誤。
- EC 與非 EC 都用相同 key 查詢，不跳過。查詢之後的處理（EC alias 融合；SuperIO v1 種子 / v2 收斂；R-014 分壓圖判讀）全部屬 orchestrator / diagram skill 職責，本 skill 不執行：
  - 不檢查 probe `SUCCESS/ERR`。
  - 不判定 BIOS 名稱應回填到哪一列（例如 `+5V` vs `+5VSB`）。
  - 不做跨來源融合裁決。

## 6. Spec-assisted Cross-check（GPIO）

### 6.1 Purpose
`-spec.json` 的 GPIO 結構資訊（人工填寫來源）作為 DB 粗匹配後的比對/過濾/偵錯依據。

### 6.2 前提
- 已有 request-form 轉出的 `spec.json`，且包含 `gpio.count` 與 `gpio.pins[]`。

### 6.3 可比對項目
- pin 總數（count）
- 每 pin direction（input/output）
- 所屬 chip / location（若有）

### 6.4 可執行動作
- 與 DB query 結果做結構一致性比對：一致者保留優先，不一致者降權或標記 `GPIO_SPEC_DB_MISMATCH`。
- 作為 debug 線索：幫助定位是 query key 問題、資料缺漏，或 spec 填寫偏差。

### 6.5 不可執行動作
- 不可僅憑此結構資料直接產生最終 channel/hwid/io_port。
- 不可把 `spec.json` 結構比對結果當作實接證據（最終仍需電路圖/實測）。

## 7. Output Contract
- `DB_NOT_FOUND`：指定的 DB path 不存在
- `INVALID_DATABASE`：DB 缺少必要的 `ProductChip` table
- `NO_SUCH_PRODUCT_CHIP`：ProductChip 未命中
- `SECTION_EMPTY`：命中 product/chip 但該 section 無資料
- `FOUND`：section 有候選
- `INVALID_SECTION`：輸入 section 不在目前 `config_new.db` table 列表（附可用 sections）

## 8. Deterministic CLI（程式化查詢）
- 腳本：`/home/company2/AIagent_susi/query_config_db.py`
- 用法：`python3 query_config_db.py <product_name> <chip_name> <section> [--db /path/to/config_new.db]`
- 輸出：JSON，固定包含 `query_key`, `status`, `prod_chip`, `rows`, `row_count`。
- table identifier 安全檢查：僅允許 `[A-Za-z0-9_.]`，避免非預期 SQL 識別字輸入。
- 可查 section：`SMBus`、`I2C`、`WDT`、`GPIO`、`StorageArea`、`ThermalProtect`、`VGA.Backlight`、`VGA.Brightness`、`HWM.Current`、`HWM.CaseOpen`、`HWM.Fan`、`HWM.Temperature`、`HWM.Voltage`。

## 9. Scope Boundary
- 只做 DB 粗匹配
- 不做 BIOS/電路圖判讀
- 不解析 probe、不依 probe 裁切 rows
- 不做最終候選勝出判定與跨來源融合

## 10. 未定規則（僅列待確認項）
- 是否需要在 `query_config_db.py` 內建 `--format ini`（目前預設僅 JSON）。
- 若支援 INI 投影，欄位與來源優先序（尤其 `[Information]` 的 `PlatformVersion`/`BIOSVersion`）最終定義。
- 空值 section 的輸出策略是否需要在 query 層顯式標準化（目前由狀態碼 + row_count 表達）。
- `rows` 欄位 schema 未來若擴充（例如 evidence/source 標記）時的相容策略。
