# Harness Orchestrator Skill

> **先看 9.1「晶片路線總表」**：每個 section 的資料從哪裡來、要不要看電路圖，一律以該表為準。

## 1. 職責與邊界

### 1.1 Purpose
負責 A 機總控（Harness Agent）流程編排、跨 skill 決策與狀態管理。

### 1.2 必須留在 Orchestrator
1. probe 觸發與回收
2. EC gate（是否可走 DB query）
3. 技能呼叫順序與 retry/fallback
4. BLOCKED/PENDING 決策
5. DB 回寫策略（何時可寫回）

### 1.3 不可下放給子 skill
1. 子 skill 不可自行重跑 probe
2. 子 skill 不可自行決定是否回寫 DB
3. 子 skill 不可覆寫總控狀態機

### 1.4 程式元件責任
| 元件 | 只負責 |
|---|---|
| `extract_pdf.py` | PDF 萃取，不做 probe/BIOS 優先序決策 |
| `understand.py` | 保證 spec schema 可承載 `information`，不做回填決策 |
| `query_config_db.py` | deterministic DB 查詢，不混入資料融合 |
| `susi_gen.py` | 產生 pre-INI / split INI / section matrix，不負責遠端部署與 API runner |
| `build_machineB_section_configs.py` | 所有 Machine-B section JSON 的唯一 builder |
| `run_machineB_full_validation.py` | P5-P12 總控與狀態機 |
| `machineb_transport.py` | SSH/SCP/PowerShell transport 與遠端檔案生命週期 |
| `targetB_task/machineB_validation/*.ps1` | 實際 SUSI API 驗證與 JSON report |
| `reload_susi4_driver.bat` | 目標機 driver reload 入口；舊 AIMB-289 BAT 不在 14-section 正常流程 |

## 2. Source of Truth
1. 使用者提供的平台型號（product_name）
2. target board 動態產生的 `susi_board_probe_report.txt`（執行時產物，不可假設預先存在）
3. AMD-only：`susi_spd_idx_probe_report.txt`（遠端固定路徑見 10.1）
4. `{project}-spec.json`
5. `config_new.db`

## 3. 判圖策略（已拍板）
1. **強制前置**：本輪需要分析 BIOS 圖、電路圖或電路圖 PDF 時，必須先讀本案目前版本的 `AnalysisSKill/bios_circuit_image_analysis_rule.md`，作為判圖唯一規則主檔；不可只靠記憶或其他摘要。
2. **引擎**：一律用 Hermes `vision_analyze`（線上模型直接看像素）；不使用 tesseract/rapidocr/paddleocr 等 offline OCR。
3. **推理強度**：只有「電路圖/BIOS 判讀與歧義判斷」用 high reasoning；其餘（probe、extract、understand、query、generate、verify）用 Hermes 預設。不在 `susi_gen.py` 內硬編碼 reasoning 等級。
4. **圖片範圍**：`bios*`、`circuit*`，副檔名 `.png/.jpg/.jpeg`，不分大小寫。不可只用 `bios*.png` 判定 `NO_BIOS_IMAGES`。
5. **何時看電路圖**：依 9.1 晶片路線總表。EC 不看；EIO-300 / `NCT6694B*` 只看 GPIO；SIO（`NCT61**D*`）看 GPIO、Voltage 分壓、Fan。
   - `circuit*.pdf`：只在 `circuit*` 圖片證據不足（解析度/裁切上下文不夠）時追加，用來補 pin 編號與網名對位，不推導超出圖證的結論。
6. 判讀證據不足時，維持 pending/ambiguous，不做硬推。

## 4. Master Flow (v1)
1. 取得平台型號。
2. 觸發 target board probe，收回 `susi_board_probe_report.txt`。
3. Information 回填（見第 7 節），落盤更新 `{project}-spec.json`。
4. 依 probe/spec 決定 chip key 與晶片路線（見 9.1 總表）：
   - `EC version` 有值：優先走 `platform + EC chipname`。
   - 無 EC 或 EC 線索不足：改走 `platform + PCH/SuperIO chipname`。
   - 都沒有可用 chipname：`PENDING_NO_CHIP_KEY`，不進 DB query。
5. 呼叫 `candidate_query_skill`。
6. 呼叫 `diagram_filter_skill` 做 BIOS/電路圖過濾。
7. 呼叫 `config_builder_skill` 產生自動化測試 cfg。
8. 結果交給 `verdict_report_skill`。
9. 成功則回寫考古 DB；失敗則回歸校正（不直接覆蓋 DB）。

## 5. 執行模式與產物位置

### 5.1 `/susiagent` 兩種入口
```text
/susiagent <PROJECT>
    → 產生階段：probe/vision/spec/pre-INI/split INI/matrix
    → 若命中 v2 convergence gate，仍依產生流程做必要 deploy/reload/reprobe
    → 呼叫 build_machineB_section_configs.py 產生 GENERATED section JSON
    → 停在 P5 handoff，不進 P6-P12

/susiagent <PROJECT> --all --host <MACHINE_B_HOST> [--user <USER>]
    → 先完成上述產生階段
    → 再以 run_machineB_full_validation.py --execute 執行 P5-P12
```
1. `--all` 是 `/susiagent` 的旗標，不可原樣轉傳給 `susi_gen.py`。
2. `--user` 預設 `susiaa`。P12 host 禁止猜測、沿用 context summary 或拿不相關 SSH alias。
3. 使用者說「所有 section 都要」：14 個 section 都要給 status；`SKIPPED_EMPTY_SECTION` 不算失敗，需分別列出 `GENERATED` / `SKIPPED_EMPTY_SECTION` / `PENDING_*`。

### 5.2 本地 probe 模式
使用者表示網路不通、或 probe report 已在專案目錄時：
1. 不執行 `ssh`、`scp`、`fetch_probe.py --mode ssh` 或其他遠端 probe；以 `--probe` 指向本地報告。
2. 不為了符合預設檔名而改名 probe 報告（例：`susi_full_probe_report.txt` 直接用）。
3. 無需求表時，可建立最小 bootstrap `<PROJECT>.json` / `-spec.json`（專案識別、canonical chip key、使用者指定的 `features`）。
4. `--stage all` 因 PDF 內嵌圖片超過上限而失敗時，回報該 blocker，改跑 `--stage generate` 並以 `--sections` 限定可做的 section。
5. 不自動重新開啟使用者未勾選的 section。

### 5.3 產物位置鎖定
1. `--project` 使用絕對專案目錄 `CASES/<PROJECT>`，並明確傳入 `--in-pdf`、`--out-json`、`--spec-out`、`--out-ini`、`--probe`，全部位於該目錄內。
2. 專案 PDF 檔名不是 `<PROJECT>.pdf`（例：`circuit_SIO6126.pdf`）時，仍以 `<PROJECT>` 命名輸出，確保 `section-matrix.project == <PROJECT>`。
3. `CASES/` 根目錄下的同名產物視為 stale 候選，不得當作本輪輸出；刪除需使用者同意。
4. Machine-B JSON 只能由同一輪、同一專案目錄的 section matrix 產生。

## 6. 14-Section 清單與產物規則

### 6.1 固定清單
`SMBus`、`I2C`、`VGA.Backlight`、`VGA.Brightness`、`HWM.Voltage`、`HWM.Current`、`HWM.Temperature`、`HWM.Fan`、`HWM.Fan.Control`、`HWM.CaseOpen`、`WDT`、`GPIO`、`StorageArea`、`ThermalProtect`

### 6.2 產物規則
1. 每輪必須對 14 個 section 全部完成評估並產出 status，不可跳過。
2. 有 status 不等於要產 INI：section 至少有一個 non-empty key/value 才輸出 `{project}_{section}.ini`。
3. 無內容時標記 `SKIPPED_EMPTY_SECTION`，不輸出該 section ini；只保留在流程狀態記錄。
4. 對 Machine-B 的交付物是「有內容的 section ini 集合」。

### 6.3 HWM 存在性：BIOS 為準（使用者決策 1B）
1. 只適用 `HWM.Voltage/Current/Temperature/Fan/Fan.Control/CaseOpen`。
2. 完成且可讀的 BIOS Hardware Monitor 畫面是 HWM section/item 存在性的裁決來源：BIOS 有顯示才產生；未顯示就不產生，即使 spec 勾選。BIOS 有顯示時，spec 漏勾或空 list 不得阻止產生。
3. `HWM.Fan.Control` 可沿用 Fan / Smart Fan 的 BIOS 證據，不要求 BIOS 有同名 section。
- **證據只看 Hardware Monitor 頁的即時讀值**：只有顯示即時讀值的那張 BIOS 圖算數；CPU 設定、iManager、主畫面版本等其他頁面一律不算。分析文字裡的否定句或設定選項名稱（例：`Case Open Detection`）不是證據。程式只依 cache 的結構化讀值（`*_value_hints`、`caseopen_hints`）判斷。
4. 非 HWM section 不套用此缺席 gate，依各自 spec/probe/DB/電路圖規則判定。
5. BIOS 圖缺失、不可讀或分析未完成：保留 pending/ambiguous，停止正式 HWM 輸出，不以 spec/DB 猜測。

## 7. Information 回填（spec.json）
1. **必備欄位**：`information.PlatformVersion`、`information.BIOSVersion`、`information.ECVersion`；型別一律 string，缺值用 `""`，不可用 null。可選來源追蹤：`information.source.<欄位>`。
2. **優先序（使用者拍板）**：probe report > BIOS > PDF/form。
3. **判定細則**：
   - 高優先值存在時，不可被低優先覆蓋。
   - `[ERR]`、缺值、明顯 placeholder 視為無效值，不可覆蓋有效值。
   - 可正規化但不可改語意（例：去除 BIOS 版本外層括號）。
   - 衝突時採高優先值，低優先值只記在 trace/source。
4. **時機**：probe 回收後、candidate query 前執行；回填後先落盤，後續 skill 一律讀回填後版本。
5. **責任**：orchestrator 負責來源整合、優先序、衝突處理與寫回 spec。

## 8. Probe 判讀用途
1. `status`：確認該項可讀/可用（SUCCESS vs UNSUPPORTED/ERR）。
2. `string`（`BOARD_NAME_STR` / `BOARD_BIOS_REVISION_STR` / `BOARD_EC_FW_STR`）：Information 回填與來源裁決；query 前置 gate（例：EC version 有效性）。
3. `value`（HWM 即時值）：與 BIOS 可見項目交叉核對候選是否過量或缺項；作為 Machine-B 驗證的容差 baseline。
4. 邊界：probe 可判讀「項目存在性與合理性」，不可直接推導最終 channel/hwid/ioport 編碼。

## 9. 晶片路線與功能來源

### 9.1 晶片路線總表（唯一來源）

| | EC | EIO-300 / `NCT6694B*` 複合晶片 | SIO（`NCT61**D*`） |
|---|---|---|---|
| 判斷 | probe 有 EC version（例：EIO-201/211、IT-xxxx） | 晶片為 EIO-300 或 `NCT6694B*` | `NCT6106D/NCT6116D/NCT6126D`（純 SIO，不可當 EC） |
| 各 section 基礎值 | DB query | DB query | 較複雜的回查比對流程（10.5 SuperIO、10.6、10.7、10.8） |
| query 用的 chip_name | 該 EC | `SMBus`、`I2C`：`NCT6694B`；其餘：`EIO-300` | 該 SIO |
| 數量過濾 | `*_susi_board_probe_report.txt` 的 `[OK]` 項目 | 同左 | probe 多半不回報，不能依賴 |
| Name（alias） | `*-bios-image-cache.json` 回填 | 同左 | 同左 |
| 看電路圖 | **不看** | **只看 `[GPIO]`**（以 `NCT6694B` 角度，`EC_P*_GPIO*`） | `[GPIO]`、`HWM.Voltage` 分壓圖、`HWM.Fan`/`HWM.Fan.Control` 配對 |
| `fan-pairing.json` | 不產生、不使用 | 不產生、不使用 | 需要（10.7） |

- 此表優先於其他章節的零散描述；有衝突以此表為準。
- 程式端已落實：EC 路線的 Fan 只用 DB rows（依 probe `[OK]` 過濾），忽略 `fan-pairing.json`。

### 9.2 各 skill 分工
1. `diagram_filter_skill`：從 BIOS/電路圖取得功能來源證據與 physical pin/net mapping；不覆寫 spec、DB 或總控狀態。
2. `candidate_query_skill`：依 orchestrator 提供的 `(product_name, chip_name)` 做 DB query；不判定 GPIO/HWM/Fan 的最終 chip。
3. orchestrator：整合 form/spec、block diagram、net-level 電路圖與實測結果，產生每個功能的 `*_function_source` 與衝突/歧義 status。

### 9.3 功能來源證據優先序
1. net-level 電路圖 pin/wire/bridge 證據
2. 可識別連接路徑的實測/probe 證據
3. block diagram 證據
4. form/`-spec.json` chip 欄位
5. chip 家族歷史經驗

沒有 net-level 證據時，不得宣稱 GPIO/HWM/Fan 來源已確認。

### 9.4 輸出欄位
`gpio_function_source`、`hwm_function_source`、`fan_function_source`、`physical_pin_map`（diagram skill 提供或標記缺證據）；`function_source_evidence[]` 為選配除錯欄位，衝突/歧義時建議填寫。

### 9.5 衝突與 pending
1. spec 與電路圖不同：保留兩邊值，採電路圖值，標記 `SPEC_SCHEMATIC_FUNCTION_SOURCE_CONFLICT`。
2. 只有 spec/form 值：標記 `FUNCTION_OWNERSHIP_NEEDS_SCHEMATIC_PROOF`，不可寫成 confirmed。
3. 同一 chip 多功能各有獨立 pin/net 證據：標記 `SAME_CHIP_MULTI_FUNCTION_CONFIRMED`，仍分開輸出各功能 mapping。
4. GPIO 經過 expander/bridge：以直接連接的 expander/bridge 為 GPIO source，上游 EC/SIO 保留為 context。
5. 無法唯一收斂：保留候選並標記 `FUNCTION_SOURCE_AMBIGUOUS`，由 orchestrator 決定 BLOCKED/PENDING。

## 10. Section 規則

### 10.1 SMBus
決策優先序（由上而下）：
1. **SIO 不短路**：`NCT61**D*`、`NCT6776D*` 走一般 CPU 家族路線（Intel 固定 Channel1、AMD 用 SPD idx），不再一律 `SECTION_EMPTY`；SIO 的 SMBus 晶片通常是 PCH，非 EC-related，只有 Channel1。
2. **Intel Channel1 例外**：Intel 路線（含 EC、SIO）即使 `features.smbus=false`，仍固定產生 SDRAM `Channel1=0x00000001,0,0,0xA0000000,`；但 `features.smbus=false` 不允許 Channel2+。
3. **Spec gate**：其餘情況 `features.smbus=false` → `SECTION_EMPTY`；`true` 才往下。
4. **EC 描述 gate**：`feature_details.smbus.chip` 含 `EC` 才允許 Channel2+ 擴展。
5. **Source chip rule**：依實際 `smbus_function_source` 判定，不可用 GPIO/HWM 的其他 chip 名稱觸發。`EIO-300 / NCT6694B` 的 SMBus 走 `NCT6694B` DB query/template；template 未完成時標記 pending，不自行填值。此規則優先於 CPU 家族路線。
6. **CPU 家族路線**：Intel 用 full board probe；AMD 用 SPD idx probe。probe 只在已選定路線內提供證據，不可反過來覆寫 chip rule。
7. **Template 組合**：最後才把已確認的 channel/index 與 HWID/template 組成 INI。

Intel Channel2+（full board probe）：
1. 只在 `features.smbus=true` 且 EC-related 時，解析 `SMBUS_OEMn ... EXISTS`。
2. 映射：`OEM0 -> Channel2`、`OEM1 -> Channel3`、`OEM2 -> Channel4`、`OEM3 -> Channel5`；channel 欄位 `0x80000000 + n`。
3. HWID 取 DB `ProductChip.hardware_id`；`io_port=0`、`option=0xA0000000`。
4. 無 OEM EXISTS 或非 EC-related：只保留 Channel1。

AMD SPD idx（固定路徑）：
1. 以 SSH 觸發 `C:\Users\susiaa\Desktop\suto\V7\run_susi_spd_idx_probe.bat`，完成後以 SCP 拉回 `C:\Users\susiaa\Desktop\suto\V7\susi_spd_idx_probe_report.txt`；不可改成手動搬檔。
2. Channel1 採 SPD 動態 idx。
3. `features.smbus=true` 且 EC-related：Channel1 idx 在 `0..3` → `Channel2=0x80000004`；idx 為 `4` → 不產生 Channel2。Channel2 的 HWID 取 DB，`io_port=0`、`option=0xA0000000`。
4. `SMBUS_SUPPORTED` 或 bus `EXISTS` 只能篩候選，不能取代實際 SPD 讀取結果。
5. 無有效 SPD 或出現多個有效 idx：`PENDING_AMD_SPD_PROBE_PATH_OR_RESULT`，不可猜測。

### 10.2 I2C
1. **Spec gate**：只有 `features.i2c=true` 才查詢與產生；false 或缺值 → `SECTION_EMPTY`，不查 DB。
2. **值只來自 DB**：依 `(product_name, chip_name)` 取 `ProductChip.hardware_id`，再查 `I2C` rows；`channel_id`、`io_port`、`options` 全取 DB。DB 沒有 I2C rows 即代表沒有 I2C（`SECTION_EMPTY`），不由 probe 補值。
3. **Probe 只做刪減**：DB rows 是 maximum set；full probe 有 I2C bus 清單時，只保留 `report_name` 與 probe 成功 bus 相符的 rows。`Devices found: (none)` 不代表 bus 不支援。probe 沒有 bus 清單時保留 DB 全集。
4. **編碼（使用者拍板）**：`channel = 0x80000000 + Id`，INI key 為 `Channel(Id+1)`，最多 `Channel5`：`I2C_EXTERNAL → Channel1 = 0x80000000`、`I2C_OEM0 → Channel2 = 0x80000001`、`I2C_OEM1 → Channel3 = 0x80000002`。
5. `I2C.id` 是 DB primary key，不是 INI channel 或 probe id。
6. 過濾後無剩餘 row → `SECTION_EMPTY`；matrix 記錄被濾掉的 id（`i2c_skipped_oem_ids`）。

### 10.3 VGA.Backlight / VGA.Brightness
1. **Backlight key 大小寫**：INI 必須是 `Backlight1=`、`Backlight2=`；禁止 `BACKLIGHT1=`。DB/JSON 的大寫 item name 不得沿用到 INI key；由 `susi_gen.py` renderer 落實並有回歸測試。
2. **Channel 數量**：DB rows 為 maximum set。probe 含 `VGA.Brightness Channels` / `VGA.Backlight Channels` 區塊時，只計 primary channel 的 `[OK] ... Status=FOUND`（`_Max`、`_Min`、`_Enable`、`_Level` 不重複計數），保留 DB 前 N rows，超出者從 split INI 與 pre-INI 剔除。兩個 section 分開計數。
3. 報告沒有對應區塊時維持 DB 全集，不可誤刪。
4. matrix 記錄 probe count、DB row count、trimmed count 與 `PROBE_CHANNEL_FILTER` route。

### 10.4 GPIO
1. **EC 路線**（`EIO-201*`、`EIO-211*`、`IT-8528*`、`IT-5782*`）：
   - target board auto report 決定實際數量（8 個輸出 `GPIO00`~`GPIO07`，4 個輸出 `GPIO00`~`GPIO03`）。
   - `hwid` 取 `ProductChip.hardware_id`；`[IOBase]`、`[IOPort/Device Address]`、`[Option]`、`[Group]`、`[Bit]` 取同 `hardware_id` 的 `GPIO` row 之 `base_addr`、`io_port`、`options`、`group`、`pin`。
   - `spec.json.gpio.pins[].name` 有值才填 `[Name]`，未填留空，不自動命名。
2. **電路圖路線**（SIO `NCT61**D*`，以及 EIO-300 / `NCT6694B*` 複合晶片的 GPIO）：GPIO 無法由 DB query 得到，`[Group],[Bit]` 只能由電路圖判定（diagram skill R-016/R-017），不套用 EC 的 auto report/template。
3. **INI key 規則（所有路線）**：`GPIO00=`、`GPIO01=`、`GPIO02=`…依序編號。電路圖路線由程式依訊號順序編（`EC_P1_GPIO0～7` → `GPIO00～07`、`EC_P2_GPIO0～7` → `GPIO08～15`；`SIO_GPIOn` 依 n），不採用代理填的 `report_name` 或晶片功能名（例：`GPIOD0` 不可變成 `GPIO130`）。group/bit 取圖上的功能名。
4. **上機驗證後 GetCaps 回 SUCCESS、但能力遮罩只缺部分 bit**：route 正確，缺的那幾支可能是 group/pin 追錯；在報告中指出這些腳位交人工確認，不自動重新追線，也不可裁切或改 route（見 verdict_report_skill.md 第 4 節第 6 點）。GetCaps 在每個 bank 都回錯誤碼時不適用本點，屬 route fallback trigger（11.5）。
5. 電路圖 trace 只有 ambiguous 結果時：status `GPIO_TRACE_AMBIGUOUS`、`row_count=0`、不輸出 `[GPIO]`。
4. 任一路線缺必要證據時標記 pending，不以另一條路線補猜，也不混合兩條 mapping。

### 10.5 HWM.Voltage

EC 路線：
1. **適用**：EC 情況，且已查得含 `channel_id` 與 `report_name` 的 `HWM.Voltage` rows。
2. **固定模板**：每列尾段為 `0,0x80000000,0,0,,0`（`[IOPort],[option],[R1],[R2],[Name],[Offset]`）；`Name` 留空時必須保留 `,,`。`HW` 取 DB `hardware_id`，`Channel` 依候選列。
3. **Name 回填時機**：初始模板 `Name` 一律留空，後處理階段再回填 alias。
4. **key 與 Name 分工**：item key 保留 DB/report 語意（`report_name` + `channel_id`）；BIOS label 只用來填 Name，不改 key（例外見第 8、9 點）。
5. **消歧**：
   - BIOS 可直接區分時直接回填（`+12V -> V120`、`VBAT -> VBAT`、DC 輸入 `+Vin`/`DC IN -> DC`，Name 照 BIOS 原文）。
   - 有歧義時（`+5V` vs `+5VSB`；`+3.3V` vs `3VSB`）：以該 row 的 `report_name` 對齊 probe `HWM_VOLTAGE_*` 決定回填目標列。
   - `+9V/9V` 是合法 BIOS 電壓 label。
6. **證據優先序**：BIOS 分析文字的實測 label（`VOLTAGE_VALUE: ...`）> `voltage_value_hints` > 一般 label hints。
7. **Singleton remainder**：bridge 最後只剩一列未對上、且 BIOS 只剩一個未用的已知 rail label 時，直接配對並記 `BIOS_SINGLETON_REMAINDER_MATCH`。
8. **VBAT 升級**：BIOS 顯示 `VBAT`、唯一候選是 `HWM_VOLTAGE_VBATLI`、且 section 內沒有真正的 `VBAT` key 時，key 由 `VBATLI` 改為 `VBAT`（只做一次）。
9. **AIMB + NCT6126D 例外（使用者決策 2B）**：structured net-level 證據證明 DB `V5SB`/VIN0 實際量 `+3.3V` 時，保留原 tuple/channel，INI key 改 `V33`、Name 填 `+3.3V`；同 channel 已有 V33 row 時保留 V33 並移除 V5SB。只有 BIOS label、block diagram 或自由文字不足以觸發。
10. **對不上時的落點（使用者拍板 2026-10-06）**：key 一律由 DB + probe（`report_name` / `channel_id`）決定，BIOS 名稱只填 Name。
    - BIOS 名稱對不上、但 key 有 SUSI 電壓 ID（例：`DC`、`12NV`）→ key 不改、Name 留空，route 加 `+AMBIGUOUS_HWM_VOLTAGE_ALIAS`，matrix 列 `voltage_alias_unresolved`。
    - DB + probe 都給不出有 SUSI 電壓 ID 的 key → 才放到下一個空的 `VOEM0`～`VOEM3`，Name 用 BIOS 名稱、沒有就填 `OEM Voltage`；route 加 `+VOLTAGE_OEM_SLOT`，matrix 列 `voltage_oem_slots`。
    - VOEM 四格用完 → 不猜，matrix 列 `voltage_unplaced`。
    - 「有沒有 SUSI 電壓 ID」以 `build_machineB_section_configs.VOLTAGE_API_INDEX`（對齊 Susi4.h）為唯一判斷來源。
11. **回歸檢查**：不同 rail 不可併成同一 key（`V5SB` 不可變 `V50`）；DB 同時有 5V 與 5VSB 時輸出必須分開。

SuperIO 路線（`NCT61xxD` / `NCT6776D`；EIO-300 / `NCT6694B` 複合晶片走 EC 路線，見 9.1）：
1. **v1 種子**：不以 `report_name` 對位為前置；DB 查到的 `channel_id` 列全部輸出，先上機驗證。
2. **v2 收斂**：只保留 BIOS 有顯示的電壓 item，並把 BIOS 顯示名稱回填到 `Name`（例：`+12V/+5V/+3.3V/+5VSB`）。
3. 同一 `channel_id` 仍有多 item 衝突（`CHANNEL_DUPLICATE`）時，交 R-014 圖證收斂；無證據標 pending。
4. 兩輪規則見 10.8。

驗證層級（Non-EC Voltage）：
- `PASS_MAPPING`：通道可讀 + item 對位 + Name 回填完成。
- `CONDITIONAL_CALIBRATION_PENDING`：mapping 成功但 R1/R2/scale 仍需校正。
- `FORMAL_PASS`：完成重複性/容差/交付標準驗證。

### 10.6 HWM.Temperature（Non-EC SuperIO）
1. **適用**：非 EC（`is_ec=False` 或未知）且 chip 屬 `NCT61xxD` / `NCT6776D`。EIO-300 / `NCT6694B` 複合晶片走 EC 路線（9.1）。
2. **v1 種子**：不以 probe report name 對位為前置；DB rows 依序全部輸出（保留 channel_id），item/key 允許暫定。
3. **v2 收斂**：只保留「BIOS 可見」且「probe `[OK]`」的 item，BIOS 顯示名稱回填 `Name`；無法收斂者標 pending/ambiguous。
4. 兩輪規則見 10.8。

### 10.7 HWM.Fan / HWM.Fan.Control

**EC 與 EIO-300 / `NCT6694B*` 路線**（不看電路圖、不做配對）：
- `[HWM.Fan]`：DB `HWM.Fan` rows 為基礎（`report_name` ↔ probe `HWM_FAN_*`，例：`SUSI_ID_HWM_FAN_CPU` ↔ `HWM_FAN_CPU` → `FCPU`），只保留 probe `[OK]` 的項目；channel/io_port/options/pulses 全取 DB；Name 由 BIOS 回填。
- `[HWM.Fan.Control]`：與 `[HWM.Fan]` 一對一同 key、同 channel、同 io_port；option `0x20000000`；Name 相同。
- DB 沒有對應 row 時不產生（`SECTION_EMPTY`），不自行推算 channel。

**SIO（`NCT61**D*`）路線**（以下 1–9 只適用 SIO）：
1. **決策時機**：先完成電路圖分析，再判定配對型態；不可先假設一對一。一對多時會另外提供 Fan Control 電路圖，從圖上判斷哪幾個風扇共用同一組 PWM 控制。
2. **訊號依據**：`[HWM.Fan]` 看 `*FAN_TACH*` / `*FAN_SPEED*`；`[HWM.Fan.Control]` 看 `*FAN_PWM*`。`*FAN_SD#*`、`*FAN_MODE*` 只作輔助。
3. **Template**：
   - `[HWM.Fan]`：`key = hwid, channel_id, io_port, options, pulses, "alias"`
   - `[HWM.Fan.Control]`：`key = hwid, channel_id, io_port, control_options, "alias"`
   - `io_port`：只有 `NCT6106D/NCT6116D/NCT6126D` 預設 `0x2E`；其他 chip 依 DB/現行 generator 規則，不可全域硬套。
   - `options/pulses/control_options` 依 DB/現行 generator template，不假設所有 chip 共用。
4. **Name 來源優先序**（所有路線）：`<PROJECT>-fan-name-hints.json` > BIOS cache 分析文字（取 BIOS 畫面上的實際文字，例：`CPU FAN Speed`）> key 預設名稱（`FCPU->CPU Fan`、`FSYS->System Fan`、`FOEMn->OEM Fan n`）。同一 key 在兩個 section 必須同名。
5. **一對一（`FAN_ONE_TO_ONE_CONFIRMED`）**：兩個 section 相同 key 集合（`FCPU`、`FSYS`、`FOEMx`），`channel_id` 同步用 `base_channel_id + idx`。
6. **一對多（`FAN_ONE_TO_MANY_CONFIRMED`）**：key 集合可相同，但 `[HWM.Fan]` 用 `base_channel_id + fanin_idx`，`[HWM.Fan.Control]` 用 `base_channel_id + control_idx`；多個 key 可共用同一 control channel。
7. **NCT61xxD（含 NCT6776D）probe 全 FAIL**：先以 BIOS fan 名稱確認 key 集合，再以 key 反查 DB `HWM.Fan` rows 取 `channel_id/io_port/options/pulses`；不可用「連續 idx 預設值」覆蓋 DB row，DB 無對應 key 才走一般 fallback。
8. **無法收斂**：pairing artifact 標記 `FAN_PAIRING_AMBIGUOUS` / `FAN_PAIRING_NEEDS_FANCONTROL_EVIDENCE` 或證據衝突時，不硬套 mapping，保留候選待補圖證。
9. **缺 artifact fallback**：pairing artifact 完全缺失、且 `HWM.Fan` channels 已 deterministic resolve 時，現行程式用一對一 control fallback 並標記 `FAN_PAIRING_MISSING_FALLBACK_ONE_TO_ONE`（不等於圖證確認）。

### 10.8 SuperIO v2 兩輪收斂（Voltage / Temperature）
1. **強制兩輪**：`NCT6106D/NCT6116D/NCT6126D` 的 `HWM.Voltage` 與 `HWM.Temperature` 必須：第一輪產 v1 並部署上機 → 重抓 probe → 第二輪產 v2。其他 SuperIO 依證據條件決定是否需要。
2. **收斂判準**（看 `<PROJECT>-section-matrix.json`）：Voltage route 含 `+SUPERIO_V2_BIOS_PROBE_ALIAS`；Temperature route 含 `+SUPERIO_TEMP_V2_BIOS_PROBE_ALIAS`。
3. **未收斂流程**：部署 `<PROJECT>-pre.ini` → reload SUSI driver → 重跑 full probe → 拉回專案目錄 → 重跑 generate → 再檢查 matrix。
4. **停止條件**：跑完一輪仍未收斂，回報明確 blocker（BIOS label 證據缺失 / probe 無 `[OK]` 通道 / 未獲遠端 deploy/reload 授權），狀態用 `PENDING_APPROVAL_FOR_V2_CONVERGENCE_LOOP` 或對應的 pending status。
5. 只有 v1 輸出時不得宣稱完成。

## 11. Post-INI Machine-B 流程（P5-P12）

### 11.1 交棒原則
產生 `<project>-pre.ini`、section matrix 與 split INIs 後，分析流程在此交棒：不再重看 BIOS/電路圖、不重查 DB/probe、不重新推導 channel。

### 11.2 固定流程
1. `build_machineB_section_configs.py` 產生所有 section JSON。
2. `run_machineB_full_validation.py` 依固定 registry 建 manifest、做 local preflight，經 `machineb_transport.py` 做 staging/backup。
3. 只把完整 `<project>-pre.ini` 複製為 `C:\Windows\SUSI\<project>.ini`；正常流程不部署 split INI。
4. 關閉 `SusiDemo4`，validation reload 一次；以唯一 SUSI4 PnP 裝置 `Status=OK`、`Problem=0/CM_PROB_NONE` 作 readiness gate。
5. 依 registry 固定順序執行 14 runners；`HWM.Fan` 與 `HWM.Fan.Control` 相鄰且有 dependency gate。
6. 所有 runner 共用同一份完整 runtime INI；第一階段會還原的寫入開關（`AllowControl`、`EnableSetConfigTest`、`EnableFunctionalTest`、`EnableWriteTest`、`EnableSetTest`、`EnableStartStopTest`）預設開啟，`EnableFixtureTest`、`EnableStimulus` 預設不傳。加 `--no-write-tests` 時全部不傳（唯讀）。判定規則見 `verdict_report_skill.md` §3。
7. 每段只接受本次新產生的 `<report_prefix>_*.json`，立即下載解析；JSON verdict 為主，process exit code 只當診斷證據。
   - 多通道 section 任一 required channel 失敗時整體可為 `FAIL`，但必須標示 `PARTIAL_FAIL` 或全通道失敗，列出 total/passed/failed、成功與失敗通道及 status code。
   - 禁止把 `Backlight1=PASS、Backlight2=FAIL` 簡寫成「VGA.Backlight failed」。
8. 產生 JSON/text summary；無論成敗都還原原 runtime INI（原先不存在則刪除），並做獨立 recovery reload。
9. P12 實機執行必須有當回合明確授權與已確認的 Machine-B host/user。

固定 handoff 命令（`run_id` 每次唯一）：
```text
.venv/bin/python run_machineB_full_validation.py \
  --project <PROJECT> \
  --repo-root /home/company2/AIagent_susi \
  --run-id <UNIQUE_RUN_ID> \
  --execute --host <MACHINE_B_HOST> --user <USER>
```

### 11.3 `--all` 授權與完成定義
1. `--all` 授權：staging、runtime INI backup/deploy、一次 validation reload、安全 runners、report 回收、rollback、一次 recovery reload。
2. `--all` 授權第一階段會還原的寫入測試（control/functional/SetConfig/write）；**不**授權 fixture、stimulus，需另有當回合明確授權。
3. 只跑 `GENERATED`/runnable sections；`SKIPPED_EMPTY_SECTION`/`PENDING_*` 不可假裝成 runnable。
4. 單一 runner `FAIL`/`ERROR` 只記入 section result，不得中止其他獨立 sections；只有 dependency gate 可阻擋依賴它的 runner。
5. 全部 runnable sections 與 rollback/cleanup 安全完成後，回報 `completed` 與 aggregate summary 路徑；個別 `FAILED`/`UNRESOLVED` 留在 summary。
6. pre-INI 產生 blocker 必須先停，不可拿舊 artifacts 進 P12；部署/reload 基礎設施失敗依狀態機進 recovery，不繼續 runners。

### 11.4 Windows PowerShell 相容性（已驗證）
1. 目標機 `New-Item` 需用 `-Path`；不可假設支援 `-LiteralPath`。
2. `Get-PnpDevice.Problem` 可能回傳 enum 名稱；readiness parser 必須正規化 `CM_PROB_NONE -> 0`、`CM_PROB_DISABLED -> 22`，並保留 raw 值。

### 11.5 完整驗證後的 route fallback convergence
1. **範圍**：與 10.8 的 v2 收斂不同；只處理 `--all` 第一輪完整 INI 驗證後，符合 route-probe failure contract 的 section。
2. **Trigger**：
   - 一般 section：`EXPECTED_SECTION_ALL_CHANNEL_API_FAILED`（所有 relevant channel API 都失敗）；任一成功即不觸發。
   - GPIO：`EXPECTED_GPIO_ROUTE_PROBES_ALL_FAILED`，只看每個 required bank 的 `SusiGPIOGetCaps` input/output 是否全敗；`GetDirection`/`GetLevel` 成功不取消資格；GetCaps 證據缺失/模糊必須 fail closed。報告中附帶的 capability mask 不影響判定。GPIO 收斂與 PASS/PARTIAL 判定見 `verdict_report_skill.md` §4 第 6 點。
   - 排除：`PARTIAL_FAIL`、fixture、infrastructure 問題。
3. **AI 責任**：讀第一輪 summary，照每個失敗 section 的 `fallback trigger:` 行決定是否執行 `--converge`；解讀結果。trigger 判定與 `fallback-plan.json` 由程式產生（validator 在第一輪結束後自動寫到 run 目錄），AI 不自行撰寫或修改 plan。
4. **Python 責任**：驗證 plan provenance/hash 與候選白名單，固定候選順序；每輪從目前已接受的完整 INI 只改一個 section 的 `IOPort/Address`，執行 deploy → reload → targeted validation，保存 tuple、INI hash、reload、report、status code。
5. **候選限制**：只能來自 `targetB_task/machineB_validation/fallback_candidate_registry.json`，排除 baseline 與重複值，禁止 AI 自創。只做 route fallback：`option_fallback_enabled=false`，不做 `(io_port, option)` 笛卡兒積。
6. **禁止修改**：`channel_id`、key、HWID、Option 等其他 tuple 欄位；SMBus `Channel1` 固定不動。
7. **結果處理**：命中後保留該 route 再處理下一 section；候選耗盡則恢復嘗試前的完整 INI，記 `UNRESOLVED` 並繼續。阻止安全完成的 deployment/reload/runner/report/restore/transaction 錯誤屬整體 fatal。
8. **落盤**：成功值寫入 `{project}-config-overrides.json`；generator 套用到完整/分段 INI，matrix 記 `PROJECT_ROUTE_OVERRIDE`，section JSON 由 builder 重建。所有遠端操作結束後仍須恢復 Machine-B 原始 runtime INI。
9. 舊 `tools/hwm_temperature_option_fallback.py` 不再由 generator 呼叫，不可混入本流程。

## 12. Common Status Codes
- `READY_FOR_QUERY`
- `PENDING_NO_CHIP_KEY`
- `PENDING_NO_EC_RULE`
- `NO_SUCH_PRODUCT_CHIP`
- `SECTION_EMPTY`
- `SKIPPED_EMPTY_SECTION`
- `FOUND`
- `AMBIGUOUS_BIOS_ITEMS`
- `GPIO_SPEC_DB_MISMATCH`
- `GPIO_TRACE_AMBIGUOUS`
- `BLOCKED_PARAMETER`
- `BLOCKED_FIXTURE`
- `FAIL_ALL_CANDIDATES`
- `INFO_BACKFILLED`
- `INFO_CONFLICT_RESOLVED`
- `INFO_MISSING_REQUIRED`
