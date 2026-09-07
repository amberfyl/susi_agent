# Candidate Query Skill

## 1. Purpose
以 `(product_name, chip_name)` 對 `config.db` 做粗匹配，輸出 section 候選。

## 2. Inputs
- `product_name`（例：SOM）
- `chip_name`（由 orchestrator 提供；預設為 probe/spec 取得的 EC chipname）
- `section`（目標 section table 名稱，例如 `WDT` / `VGA.Backlight`）
- `config.db` path

## 3. Preconditions
- 本 skill 不自行判讀 probe report；是否執行由 orchestrator gate 決定。
- orchestrator 需先提供 `is_ec` 判定結果（來自 probe `BOARD_EC_FW_STR`）。
- 無論 `is_ec=True/False`，`query_config_db.py` 都可執行 deterministic query。
- 差異只在 `HWM.Voltage` 的後續處理路徑：
  - `is_ec=True`：走 EC 融合/alias 回填規則（由 orchestrator 執行）。
  - `is_ec=False`：先查 `HWM.Voltage` 候選，再走 SuperIO 圖證路徑（R-014/R-015）或標記 pending。
- 若 probe 缺失導致 `is_ec` 未定，可先查 DB；但裁決需標記 pending，不能硬判。

## 4. Chip key 說明（資料面）
- `config.db` 的 `ProductChip` 可能存在多種配對：
  - `platform + EC chip`
  - `platform + PCH chip`
  - `platform + SuperIO chip`
- 本 skill 只負責照 orchestrator 傳入的 `(platform_name, chip_name)` 做 deterministic query，不自行改鍵或擴大猜測。

## 5. Query Rule
1. 查 `ProductChip`：`UNIQUE(product_name, chip_name)`
2. 命中 `prod_chip_id` 後，查各 section table

### 5.1 I2C
- 只有 `{project}-spec.json` 的 `features.i2c == true` 才進行 I2C query/generate；false 或缺值時維持 `SECTION_EMPTY`。
- query 只查 `config.db` 中實際存在的 `I2C` row；沒有對應 row 的組合不做 fallback，維持 `SECTION_EMPTY`。
- 目前 `I2C` table 由 `prod_chip_id` join `ProductChip` 後的四種組合就是候選全集：`MIO/EIO-201`、`MIO/IT-8528`、`ARK/IT-8528`、`MIO/NCT6694B`。
- `I2C.id` 是 DB row primary key，不能當作 INI channel 或 full probe 的 I2C id。
- `io_port`、`options` 一律取命中的 DB row。
- DB row 的 `channel` 有值時直接使用 DB channel；DB row 的 `channel` 為空時，交由 generate 路徑讀 full probe 的有效 `I2C_OEMn`，以 OEM 的邏輯 `n` 計算 `channel = 0x80000000 + n`。
- `I2C_OEM0`、`I2C_OEM1` 等邏輯 channel 分別落在 INI `Channel2`、`Channel3`；I2C INI 最大支援 `Channel5`，超出範圍不可硬塞。
- candidate query 不解析 probe，也不在 query 層決定最終 channel；probe ID 組合由 orchestrator/generate 路徑執行。

## 6. Spec-assisted Cross-check (R-008 migrated)

### 6.1 Purpose
`-spec.json` 的 GPIO 結構資訊（人工填寫來源）作為 DB 粗匹配後的比對/過濾/偵錯依據。

### 6.2 前提
- 已有 request-form 轉出的 `spec.json`，且包含 `gpio.count` 與 `gpio.pins[]`。

### 6.3 可比對項目
- pin 總數（count）
- 每 pin direction（input/output）
- 所屬 chip / location（若有）

### 6.4 可執行動作
- 與 DB query 結果做「結構一致性比對」：一致者保留優先，不一致者降權或標記 `GPIO_SPEC_DB_MISMATCH`。
- 作為 debug 線索：幫助定位是 query key 問題、資料缺漏，或 spec 填寫偏差。

### 6.5 不可執行動作
- 不可僅憑此結構資料直接產生最終 channel/hwid/io_port。
- 不可把 `spec.json` 結構比對結果當作實接證據（最終仍需電路圖/實測）。

## 7. Output Contract
- `NO_SUCH_PRODUCT_CHIP`：ProductChip 未命中
- `SECTION_EMPTY`：命中 product/chip 但該 section 無資料
- `FOUND`：section 有候選
- `INVALID_SECTION`：輸入 section 不在目前 `config.db` table 列表

## 8. Deterministic CLI（程式化查詢）
- 腳本：`/home/company2/AIagent_susi/query_config_db.py`
- 用法：
  - `python3 query_config_db.py <product_name> <chip_name> <section> [--db /path/to/config.db]`
- 輸出：JSON（固定包含 `query_key`, `status`, `prod_chip`, `rows`, `row_count`）
- 目前可查 section（依現況）：
  - `WDT`
  - `StorageArea`
  - `ThermalProtect`
  - `I2C`
  - `VGA.Backlight`
  - `HWM.Current`
  - `HWM.CaseOpen`

## 9. Scope Boundary
- 只做 DB 粗匹配
- 不做 BIOS/電路圖判讀
- 不做最終候選勝出判定

## 10. HWM.Voltage.Channels 資料契約（僅資料層）

### 10.1 目的
- 提供 `report_name <-> channel_id` 的靜態對照，供 orchestrator 在 EC 流程中做消歧使用。

### 10.2 契約
- `report_name`：對齊 probe report 的 `HWM_VOLTAGE_*` 列舉名稱。
- `channel_id`：對應 INI `HWM.Voltage` 中 `Channel` 欄位值。
- 本表可不覆蓋 probe 全列舉；未收錄者視為例外，不代表錯誤。

### 10.3 邊界（重要）
- 本 skill 不檢查 probe `SUCCESS/ERR`。
- 本 skill 不判定 BIOS 名稱應回填到哪一列（例如 `+5V` vs `5VSB`）。
- 本 skill 只回傳查詢結果與 mapping 資料，不做跨來源融合裁決。

## 11. EC / 非EC 的 HWM.Voltage Query 流程（總控對齊）

### 11.1 EC（`is_ec=True`）
1. query `HWM.Voltage` table（固定 key：`(product_name, chip_name)`）
2. 產出候選列（`FOUND`/`SECTION_EMPTY`）
3. alias 不在 query 層裁決；交由 orchestrator 依 BIOS + probe + `HWM.Voltage.Channels` 融合回填

### 11.2 非EC（`is_ec=False`）
1. 仍 query `HWM.Voltage` table（同固定 key，不跳過）
2. 以 query 候選作為 base
3. 進入 SuperIO 圖證路徑：
   - 命中 R-014 / R-015 的適用條件時，依圖證修正 channel/alias
   - 無足夠證據或無法收斂時，標記 pending（例：`PENDING_CHANNEL_DUPLICATE_NEED_EVIDENCE`）

### 11.3 邊界
- query skill 只提供 DB 候選與 mapping 資料，不做 R-014/R-015 判圖執行。
- R-014/R-015 的判圖修正屬 orchestrator / diagram filter skill 職責。

## 12. 程式化現況與未定規則（暫放）

> 註：本段是「現況註記」，不是最終規則定稿。

### 11.1 已程式化（目前已落地）
- 固定查詢鍵：`(product_name, chip_name)`。
- 流程：先查 `ProductChip` 取 `prod_chip_id`，再查目標 section table。
- section 存在性檢查：不存在則回 `INVALID_SECTION`（並附可用 sections）。
- 統一狀態輸出：`NO_SUCH_PRODUCT_CHIP` / `SECTION_EMPTY` / `FOUND` / `INVALID_SECTION`。
- 輸出契約：JSON（`query_key`, `status`, `prod_chip`, `rows`, `row_count`）。
- table identifier 安全檢查：僅允許 `[A-Za-z0-9_.]`，避免非預期 SQL 識別字輸入。

### 11.2 未定規則（僅列待確認項）
- 是否需要在 `query_config_db.py` 內建 `--format ini`（目前預設僅 JSON）。
- 若支援 INI 投影，欄位與來源優先序（尤其 `[Information]` 的 `PlatformVersion`/`BIOSVersion`）最終定義。
- Query 階段是否要承載任何 BIOS/spec 後處理結果（目前不承載，僅 DB 粗匹配）。
- 空值 section 的輸出策略是否需要在 query 層顯式標準化（目前由狀態碼 + row_count 表達）。
- `rows` 欄位 schema 未來若擴充（例如 evidence/source 標記）時的相容策略。
