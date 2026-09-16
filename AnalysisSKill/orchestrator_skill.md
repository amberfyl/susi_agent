# Harness Orchestrator Skill

## Purpose
負責 A 機總控（Harness Agent）流程編排、跨 skill 決策與狀態管理。

## Source of Truth
1. 使用者提供的平台型號（product_name）
2. target board 動態產生的 `susi_board_probe_report.txt`
3. AMD-only target board 動態產生的 `susi_spd_idx_probe_report.txt`
4. `{project}-spec.json`
5. `config_new.db`

> `susi_board_probe_report.txt` 是執行時動態產物，不可假設預先存在。
> `susi_spd_idx_probe_report.txt` 只在 AMD SPD idx 路線使用；遠端固定路徑為 `C:\Users\susiaa\Desktop\suto\V7\run_susi_spd_idx_probe.bat` / `C:\Users\susiaa\Desktop\suto\V7\susi_spd_idx_probe_report.txt`。

## 圖片判讀引擎策略（已拍板）
- 圖片判讀 gate 一律使用 Hermes `vision_analyze`（線上模型直接看像素）。
- 不使用 offline OCR engine（不依賴 tesseract/rapidocr/paddleocr 等本地 OCR 安裝）。
- 圖片輸入範圍固定包含：`bios*.png` + `circuit*.png`。
- 若 `circuit*.png` 判讀證據不足（解析度/裁切上下文不夠），可追加分析同案電路圖 PDF；檔名通常為 `circuit*.pdf`。
- PDF 用途：補 pin 編號與網名對位，不用來硬推超出圖證據的結論。
- 若判讀證據不足，維持既有 pending/ambiguous status 流程，不做硬推。

## 推理強度策略（已拍板）
- 僅「電路圖/BIOS 圖片判讀與歧義判斷」使用 **high reasoning**。
- 其餘流程（probe、extract、understand、query、generate、verify）使用 Hermes 預設 reasoning。
- 不在 `susi_gen.py` 內硬編碼 reasoning 等級；此策略屬 orchestrator/skill 執行層。
- 當 Hermes 全域設定改回 default 後，依本策略即可達成「只有判圖 high，其它 default」。

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

### SMBus decision precedence
- **Spec gate（最高）**：以 `{project}-spec.json` 為準。
  - `features.smbus=false` → `SECTION_EMPTY`，不進行 SMBus 產生流程。
  - `features.smbus=true` 才進入下列 routing。
- **EC 描述 gate**：讀 `feature_details.smbus.chip`；若文字含 `EC` 視為 EC-related SMBus，才允許 Channel2+ 擴展邏輯。
- 在需要 SMBus 時，規則依下列優先序執行：
  1. **SMBus source chip rule**：若實際 `smbus_function_source` 命中 chip-family 特例，依 SMBus chip rule 處理；不可用 GPIO/HWM 的其他 chip 名稱觸發。
  2. **CPU-family channel route**：Intel 走 full board probe report；AMD 走 SPD idx probe。
  3. **Probe evidence**：只在已選定 route 內提供 bus existence 或有效 SPD idx；不得反過來覆寫 chip rule。
  4. **Template composition**：最後才將已確認的 channel/index 與指定 HWID/template 組合成 INI。

### Intel SMBus Channel2+ mapping（使用 full board probe report）
- Intel 固定先產生 `Channel1=0x00000001,0,0,0xA0000000,`。
- 只有在 `features.smbus=true` 且 `feature_details.smbus.chip` 為 EC-related 時，才解析 full board probe 的 `SMBUS_OEMn ... EXISTS` 產生 `Channel2+`。
- 映射規則：`OEM0 -> Channel2`、`OEM1 -> Channel3`、`OEM2 -> Channel4`、`OEM3 -> Channel5`。
- 對應 channel 欄位為 `0x80000000 + n`（n 為 OEMn 的 n）。
- `Channel2+` 的 HWID 取 DB `ProductChip.hardware_id`；`io_port=0`、`option=0xA0000000`。
- 若無 OEM EXISTS 或非 EC-related，則只保留 Channel1（Channel2+ 留空）。

### AMD SDRAM SPD idx 遠端探測流程（固定路徑）
- CPU family 判定為 AMD 時，透過 SSH 連線到 target board 執行 `run_susi_spd_idx_probe.bat`。
- 固定流程：先 SSH 觸發遠端 BAT，完成後以 SCP 將 report 拉回機器 A（不可改成手動搬檔）。
- 固定遠端 BAT：`C:\Users\susiaa\Desktop\suto\V7\run_susi_spd_idx_probe.bat`
- 固定遠端 report：`C:\Users\susiaa\Desktop\suto\V7\susi_spd_idx_probe_report.txt`
- Channel1 仍採 SPD 動態 idx。
- 當 `features.smbus=true` 且為 EC-related：
  - 若 Channel1 idx 在 `0..3`，則 `Channel2` 固定填 `0x80000004`。
  - 若 Channel1 idx 為 `4`，則不產生 Channel2（留空）。
- 其餘欄位規則：Channel2 的 HWID 取 DB `ProductChip.hardware_id`，`io_port=0`、`option=0xA0000000`。
- `SMBUS_SUPPORTED` 或 bus `EXISTS` 只能用來篩選 candidate；不能取代實際 SPD 讀取結果。
- 若沒有有效 SPD、出現多個有效 `idx`，標記 `PENDING_AMD_SPD_PROBE_PATH_OR_RESULT`，不可猜測。

### I2C decision precedence
- **Spec gate（最高）**：只有 `{project}-spec.json` 的 `features.i2c=true` 才查詢與產生 `[I2C]`；false 或缺值標記 `SECTION_EMPTY`，不執行 DB query。
- **DB candidate gate**：依 `(product_name, chip_name)` 查 `ProductChip`，再依 `prod_chip_id` 查 `I2C`；目前只有四筆實際 DB row 是候選，其他組合保持 `SECTION_EMPTY`，不可 fallback。
- `I2C.id` 是資料庫 row primary key，不是 full probe 的 I2C id。
- `io_port`、`options` 取命中的 `I2C` row；DB `channel` 有值時也直接取 DB channel。
- DB `channel` 為空時，使用 full probe report 中有效的 `I2C_OEMn`。公式中的 `n` 是 `I2C_OEMn` 的邏輯編號；報告括號內的 raw API `Id` 只保留作追蹤，不直接代入公式。
- channel 組合為 `0x80000000 + n`，並映射為 INI `Channel(n+2)`；因此 `I2C_OEM0 -> Channel2`、`I2C_OEM1 -> Channel3`，最多產生到 `Channel5`。
- `I2C_EXTERNAL` 不作為上述 EC DB template 的 OEM channel；沒有可用 `I2C_OEMn` 時標記 `PENDING_I2C_PROBE`，不可產生空 channel 值。

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
- SuperIO 首版種子策略（NCT6694B / NCT61xxD / NCT6776D）：
  - 不以 `report_name` 成功匹配作為前置條件。
  - 直接把 DB 查到的 `channel_id` 列全部輸出到 `HWM.Voltage`（v1 種子 INI），先上機驗證。
  - v1 階段不強制完成 item 精準對位；以 target 回報成功通道為主，之後再用 BIOS 名稱/電壓值回推 item，產生 v2。
  - v2 必須做兩件事：
    1. 只保留 BIOS 畫面有顯示的電壓 item。
    2. 將 BIOS 顯示名稱回填到 INI `Name(alias)` 欄位（例如 `+12V/+5V/+3.3V/+5VSB`）。
  - 第二輪硬性規則（/susiagent 自動收斂）：
    - 對 `NCT6106D/NCT6116D/NCT6126D`（61**D）平台，`HWM.Voltage` 必須走兩輪：
      1) 第一輪先產 v1 種子並部署上機
      2) 重抓 probe 後第二輪再產生 v2
    - 未跑第二輪不得宣稱完成（除非有明確 blocker：SSH/driver/probe/BIOS 證據不足）。
- 若後續仍有同一 `channel_id` 多 item 衝突（`CHANNEL_DUPLICATE`），再交由圖證流程（R-014/R-015）做收斂；無證據時標記 pending。

### 驗證層級定義（Non-EC Voltage）
- `PASS_MAPPING`：通道可讀 + item 對位 + Name(alias) 回填完成（對應 A+B）。
- `CONDITIONAL_CALIBRATION_PENDING`：mapping 成功但數值比例仍需校正（如 R1/R2/scale 未收斂）。
- `FORMAL_PASS`：完成正式條件（重複性/容差/交付標準）驗證。

## HWM.Temperature（Non-EC SuperIO）v1 / v2 規則

### 適用條件
- 非 EC（`is_ec=False` 或未知）且 chip 屬 NCT6694B / NCT61xxD / NCT6776D。

### v1 種子輸出
- 不以 probe report name 對位作前置條件。
- 先將 DB 查到的 `HWM.Temperature` rows 依序全部輸出成 v1 INI（保留 channel_id）。
- v1 階段允許 item/key 為暫定，不要求一次到位。

### v2 收斂輸出
- 上機驗證後，使用 target probe 成功通道 + BIOS 可見溫度名稱/數值做交集收斂。
- 只保留「BIOS 可見」且「probe [OK]」的溫度 item。
- 將 BIOS 顯示名稱回填到 INI `Name(alias)` 欄位。
- 無法收斂者標記 pending/ambiguous，不做硬判。
- 第二輪硬性規則（/susiagent 自動收斂）：
  - 對 `NCT6106D/NCT6116D/NCT6126D`（61**D）平台，`HWM.Temperature` 必須走兩輪：
    1) 第一輪先產 v1 種子並部署上機
    2) 重抓 probe 後第二輪再產生 v2
  - 未跑第二輪不得宣稱完成（除非有明確 blocker：SSH/driver/probe/BIOS 證據不足）。

### option fallback（溫度全 ERR 時）
- 觸發條件：`[HWM.Temperature]` 初版上機後，probe 的 `HWM_TEMP_*` 在同一輪結果全為 `[ERR]`（0 個 `[OK]`）。
- 動作：改為候選組合迭代，針對 `[HWM.Temperature]` 同步調整 `io_port + option` 後重測。
  - 每輪：套用一組 `(io_port, option)` → 部署/重載 → 重抓 probe。
  - 任一輪出現 `HWM_TEMP_*` 非全 ERR（有 OK）即視為命中，停止迭代並保留該組。
- 邊界：
  - 不改 `channel_id/item_key`。
  - 若候選組合全部測完仍全 ERR，標記 `PENDING_TEMP_IO_OPTION_CANDIDATES_EXHAUSTED` 並回報各輪 probe 證據。

## What MUST stay in Orchestrator
- probe 觸發與回收
- EC gate（是否可走 DB query）
- 技能呼叫順序與 retry/fallback
- BLOCKED/PENDING 決策
- DB 回寫策略（何時可寫回）

## Function Source Reconciliation Contract

### Responsibility split
- `diagram_filter_skill`：從 BIOS/電路圖取得功能來源證據與 physical pin/net mapping；不自行覆寫 spec、DB 或總控狀態。
- `candidate_query_skill`：依 orchestrator 提供的 `(product_name, chip_name)` 做 deterministic DB query；不判定 GPIO/HWM/Fan 的最終 chip。
- `orchestrator`：整合 form/`-spec.json`、block diagram、net-level schematic 與實測結果，產生每個功能的 `*_function_source` 與衝突/歧義 status。

### Source precedence for function source
1. net-level schematic pin/wire/bridge evidence
2. measured/probe evidence that identifies the connected function path
3. schematic block diagram evidence
4. form/`-spec.json` chip field
5. chip-family historical assumption

> Block diagram、form/JSON 與歷史案例只能提供候選或架構方向；沒有 net-level pin/wire 證據時，不得宣稱 GPIO/HWM/Fan source 已確認。

### GPIO mapping source
- `EIO-201*`、`EIO-211*`、`IT-8528*`、`IT-5782*`：使用 target board auto report 的 group/pin。
- `NCT6106D*`、`NCT6116D*`、`NCT6126D*`：**純 SIO 路線**，使用 schematic 的 signal -> function label 判定 group/pin。
- `EIO-300 / NCT6694B` 複合規則：`GPIO / I2C / SMBus` 走 `NCT6694B`；其餘 section 走 `EIO-300`（EC side）。
- source 不明時標記 pending，不混用來源，也不自行猜測。

### GPIO mapping flow
- EC 路線：auto report 決定實際 GPIO 數量；8 個只輸出 `GPIO00`~`GPIO07`，4 個只輸出 `GPIO00`~`GPIO03`。
- EC 路線：`hwid` 由 DB 查詢；`[IOBase]`、`[IOPort/Device Address]`、`[Option]`、預設 `[Name]` 由 `GPIO.Defaults` 提供；`[Group],[Bit]` 由 `GPIO.GroupPins` 提供。
- EC 路線：若 `spec.json.gpio.pins[].name` 有使用者名稱，填入 `[Name]`；未填時留空，不自動命名。
- NCT/SIO 路線：`[Group],[Bit]` 只由 schematic 判定，依 diagram skill R-016/R-017；不套用 EC 的 auto report/template mapping。
- 任一路線缺少必要證據時標記 pending，不以另一條路線補猜，也不混合兩條 mapping。

### SMBus chip rule
- `EIO-300 / NCT6694B`：`SMBus` 走 `NCT6694B` 規則（DB query/template）；DB template 尚未完成時標記 pending，不自行填值。
- 此 chip rule 優先於一般 Intel/AMD channel route；其他 chip 維持對應 CPU-family 流程。

### Required reconciliation output
- `gpio_function_source`
- `hwm_function_source`
- `fan_function_source`
- `physical_pin_map`（由 diagram skill 提供或標記缺證據）
- `function_source_evidence[]`（選配除錯欄位；僅在衝突/歧義時建議填寫）

### Conflict and pending rules
- spec 與 schematic 不同：保留兩邊值，採 schematic 值作硬體判定，標記 `SPEC_SCHEMATIC_FUNCTION_SOURCE_CONFLICT`。
- 只有 spec/form 值：標記 `FUNCTION_OWNERSHIP_NEEDS_SCHEMATIC_PROOF`，不可寫成 confirmed。
- 同一 chip 的多個功能均有獨立 pin/net 證據：標記 `SAME_CHIP_MULTI_FUNCTION_CONFIRMED`，仍分開輸出各功能 mapping。
- GPIO 經過 expander/bridge：以實際直接連接功能的 expander/bridge 作為 GPIO source，保留上游 EC/SIO 作為 upstream context。
- 證據無法唯一收斂：保留候選並標記 `FUNCTION_SOURCE_AMBIGUOUS`，由 orchestrator 決定是否 BLOCKED/PENDING。

## FAN 配對輸出契約（HWM.Fan / HWM.Fan.Control）

### 決策時機
- 先完成 diagram skill 的 PDF/電路圖分析，再判定是否為一對一配對。
- 不做前置硬 gate（不是先假設 one-to-one 才分析）。
- 訊號主依據：`[HWM.Fan]` 看 `*FAN_TACH*` / `*FAN_SPEED*`；`[HWM.Fan.Control]` 看 `*FAN_PWM*`。
- `*FAN_SD#*`、`*FAN_MODE*` 僅作輔助證據，不可直接決定 pairing/idx。

### 固定 template（必須遵守）
- `[HWM.Fan]`
  - `key = hwid, channel_id, 0x2E, 0x80000000, 0, "alias"`
- `[HWM.Fan.Control]`
  - `key = hwid, channel_id, 0x2E, 0x20000000, "alias"`
- 有了固定 template 後，動態填值只剩：`hwid`、`channel_id`、`alias`。

### NCT61xxD（含 NCT6106D/NCT6116D/NCT6126D/NCT6776D）probe 全 FAIL 特例
- 若 fan probe 全 FAIL（無可用 fan key），先以 BIOS fan 名稱確認 fan key 集合（`FCPU`/`FSYS`/`FOEMx`）。
- 再以該 key 集合反查 `config_new.db` 的 `HWM.Fan` rows，取 DB 對應的 `channel_id/io_port/options/pulses`。
- 不用「連續 idx 預設值」覆蓋 DB row；只有 DB 無對應 key 時才回退到一般 fallback。

### 一對一成立時（`FAN_ONE_TO_ONE_CONFIRMED`）
- `[HWM.Fan]` 與 `[HWM.Fan.Control]` 輸出相同 key 集合（`FCPU`、`FSYS`、`FOEMx`）。
- 兩個 section 的 `channel_id` 同步使用 `base_channel_id + idx`。

### 一對多成立時（`FAN_ONE_TO_MANY_CONFIRMED`）
- `[HWM.Fan]` 與 `[HWM.Fan.Control]` 可維持相同 key 集合，但 `channel_id` 允許不同。
- `[HWM.Fan]` 的 `channel_id` 依 FANIN idx（`base_channel_id + fanin_idx`）。
- `[HWM.Fan.Control]` 的 `channel_id` 依 PWM 控制來源 idx（`base_channel_id + control_idx`）。
- 允許多個 key 在 `[HWM.Fan.Control]` 共用同一 `channel_id`（例如一對多）。

### 無法收斂時
- 不可硬套 `idx -> FCPU/FSYS/FOEMx`。
- 標記 `FAN_PAIRING_AMBIGUOUS` 或 `FAN_PAIRING_NEEDS_FANCONTROL_EVIDENCE`，保留候選待補圖證。

## What MUST NOT be pushed into child skills
- 各 skill 不可自行重跑 probe
- 各 skill 不可自行決定是否回寫 DB
- 各 skill 不可覆寫總控狀態機

## Common Status Codes
- `READY_FOR_QUERY`
- `PENDING_NO_EC_RULE`
- `SMBUS_PLATFORM_EXCLUDED`
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
