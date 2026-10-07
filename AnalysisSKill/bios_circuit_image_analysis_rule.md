# Diagram Filter Skill (BIOS / Schematic Analysis Rules)

版本：v0.5（整併版）
角色：此檔即 diagram_filter_skill 的規則主檔（沿用既有檔名 `bios_circuit_image_analysis_rule.md`）。
適用範圍：AIagent_susi 專案中，針對 BIOS 圖片與電路圖做候選過濾與 ini 內容裁切。

補充（圖資判讀）：
- BIOS raster 以大小寫不敏感的 `bios*.png|jpg|jpeg` 納入 scoped analysis。
- 電路圖 raster 以大小寫不敏感的 `circuit*.png|jpg|jpeg` 納入。
- 何時看電路圖（使用者拍板）：SIO 晶片 `NCT61**D*`、`NCT6694B*` 的 GPIO 等資訊無法由 DB query 取得，必須分析電路圖；其他晶片只在既有證據不足時才分析。
- `circuit*.pdf` 只在 `circuit*` 圖片證據不足（解析度/裁切上下文不夠）時追加；PDF 命中頁必須產生可覆核的高解析 focused crop，用於「pin 編號 + 網名 + 功能名」對位，不可脫離圖證據做硬推。

## 規則 R-001：AMD 平台不做 EPYC hard exception

### 原則
- 在 SMBus 路由中，不再使用 `AMD EPYC` 作為先行排除條件。
- 只要 CPU family 為 AMD，統一進入 AMD 正規流程（SPD idx probe + 下游規則）判定。

### 後續動作
- 不產生 `SMBUS_PLATFORM_EXCLUDED` / `AMD_EPYC_NO_SMBUS` 這類先行阻斷狀態。
- 不因為 BIOS 文字含 `AMD EPYC` 就提前刪除或清空 `[SMBus]`。
- 是否輸出 SMBus、輸出哪些 channel，交由 AMD 正規流程與 probe 證據決定。

### 影響範圍
- 本調整僅移除 AMD EPYC hard exception。
- 其他 SMBus gate（例如 spec gate、EC-related channel 規則）維持原流程。

## 規則 R-002：HWM BIOS 白名單與 section gate（Query 後裁切）

### 適用範圍
- 只適用於會呈現在 BIOS Hardware Monitor / PC Health 類頁面的 `HWM.*`：`HWM.Voltage`、`HWM.Current`、`HWM.Temperature`、`HWM.Fan`、`HWM.Fan.Control`、`HWM.CaseOpen`。
- `HWM.Fan.Control` 可由 BIOS 的 Fan / Smart Fan evidence 代表，不要求畫面另列一個同名 section。
- SMBus、I2C、VGA、WDT、GPIO、StorageArea、ThermalProtect 等非 HWM section 本來就不以 BIOS Hardware Monitor 清單呈現，不得套用此 gate。

### 判定邏輯（使用者決策 1B）
- request/spec 的勾選可能誤勾、漏勾或註記 follow BIOS，因此 HWM section/item 的存在性以可用 BIOS 畫面為準，spec 只保留作 intent/衝突 trace，不得覆寫 BIOS 結果。
- 以 BIOS 圖中實際可見的同類監測項目建立白名單（label set）。
- `保留 = Query候選項目 ∩ BIOS白名單項目`。
- BIOS 畫面未顯示任何該類 HWM item 時，不產生該 `HWM.*` section；同時移除 stale split/config artifacts，matrix 記錄 `SKIPPED_NOT_APPLICABLE` / `BIOS_EVIDENCE_NOT_FOUND`。
- BIOS 有顯示該類項目時，只產生可對齊白名單的 rows；不可因 DB 有候選就補出 BIOS 未顯示的 HWM item。
- BIOS 圖缺失、頁面不可讀或 scoped analysis 未完成時，標記 pending/ambiguous 並停止正式 HWM 輸出；不得以 spec/DB 猜測成正式 section。

### 範例
- BIOS 可見：`CPU Temperature`、`COM Module FAN`、`Carrier Board FAN`、`+12V`、`+5V`、`VBAT`。
- 若 query 在 `HWM.Voltage` 回傳 `+12V,+5V,+3.3V,VBAT`，則刪除 `+3.3V`，保留其餘三項。
- 即使 spec 勾選 `HWM.Current`，若可用 BIOS Hardware Monitor 畫面沒有 Current/Ampere 項目，仍不產生 `[HWM.Current]`。

## 規則 R-003：HWM BIOS 功能存在性 Gate（不裁決 tuple）

### 觸發條件
- 已取得並完成 scoped analysis 的 BIOS Hardware Monitor / PC Health 頁面。
- 已有 DB query 候選資料作為 input。

### 判定邏輯
- BIOS 可見某類 HWM 監測項，該 HWM section 才有產生資格。
- BIOS 未顯示該類 HWM 監測項，該 section 不產生；不得降級為「低優先但仍輸出」。
- 此 gate 只決定 HWM section/item 是否存在，不確認 channel/hwid/io_port/option tuple 正確性。

### BIOS -> Section 對應
- Voltage rail labels -> `HWM.Voltage`
- Temperature labels -> `HWM.Temperature`
- Fan/RPM labels -> `HWM.Fan`
- Fan / Smart Fan evidence -> `HWM.Fan.Control`
- Current/Ampere labels -> `HWM.Current`
- Case Open / Chassis Intrusion labels -> `HWM.CaseOpen`

### 注意事項
- 不得把本規則外推到非 HWM sections；例如 Backlight、I2C、SMBus 是否存在由各自 spec/probe/DB/電路圖規則裁決。
- 不可用本規則推導任何 INI tuple 欄位。

## 規則 R-004：BIOS Label 到 INI Item 的軟映射（Soft Mapping）

### 目的
- 在 DB 候選過多時，先做「名稱級」對齊，降低不相關項目。

### 常用軟映射（可跨案沿用）
- `COM Module FAN` -> `CPUFAN`（或 `FCPU`）
- `Carrier Board FAN` -> `SYSFAN`（或 `FSYS`）
- `CPU Temperature` -> `TCPU`
- `+12V` -> `V12`
- `+5V` -> `V50`
- `VBAT` -> `VBAT`
- 讀 BIOS 圖時「只取即時讀值列、不取門檻設定」等讀圖規則見 R-020（例：`CPU Temperature` 可用、`CPU Shutdown Temperature` 不可用）。

### 執行規則
- 上述映射屬於「高機率」而非硬規則，必須保留可覆寫空間。
- 若同案實測/驗證顯示對應不同，實測優先並回寫案內 mapping。

### 交付格式提醒
- 有可靠 BIOS/圖證據時，先持久化為 project-scoped hints artifact，再由 generator 在產生 split INI 與 `-pre.ini` 時 deterministic 套用 display name。
- Machine-B 驗證不是 name backfill 的必要前置；不得直接手改生成後 INI。證據不足時才保留空白/候選並標記 ambiguity。

## 規則 R-005：BIOS 數值用途分層（Reference Only）

### 可做
- BIOS 即時值（溫度/電壓/RPM）可作為後續驗證比對基準（容差比對）。
- 讀圖時要一併記錄可見的即時值（V/mV、C、RPM），規則見 R-020；結果存入 `<PROJECT>-bios-image-cache.json` 的 `voltage_value_hints` / `temperature_value_hints` / `fan_value_hints`。

### 不可做
- 不可用 BIOS 當下數值推導 ini channel 編碼。
- 不可用 BIOS 當下數值替代 DB 的 channel/hwid/ioport 決策。

## 規則 R-006：CPU Configuration BIOS 頁面的用途邊界

### 可用用途
- 可用於平台分流與一般規則判斷（例如：Intel / AMD family 路由）。
- 可用於案內一致性核對（避免拿錯專案圖資）。

### 不可用用途
- 不可據此判定 I2C/SMBus/GPIO/Backlight 的 channel/hwid/io_port。
- 不可據此做 section item 白名單裁切（除非頁面有明確顯示對應項目）。

### 判讀結論
- `CPU Configuration` 類頁面屬於「平台身分證據」，不是「SUSI 介面映射證據」。

## 規則 R-007：電路圖證據等級（Routing vs. Item）

### 強證據（可直接用於候選過濾）
- 有明確網名 + 連線路徑 + 來源/目的端（含 option 電阻狀態）的 I2C/SMBus 路由圖。
- 可用於判定「bus 來源是 EC 還是 SOC/PCH」，並做候選降權/保留。

### 中證據（可做輔助，不做硬裁決）
- Block diagram 或功能框圖只顯示「有 I2C/SMBus 通道」，但無實際 net 與接點細節。
- 可做功能存在性支持，不可獨立決定通道映射。

### 弱/無證據
- 僅有元件名稱、無路徑；或與目標 section 無直接關聯的頁面。
- 不得據此新增或刪除具體 ini item。

## 規則 R-008：BIOS 圖的 Information Header 回填（PlatformVersion / BIOSVersion）

### 目的
- 當新案沒有最終 `.ini` 參考答案時，利用 BIOS 圖可讀資訊回填 `[Information]` 中可確定的中繼資料。

### 可回填欄位（僅限）
- `PlatformVersion=`
- `BIOSVersion=`

### 觸發條件
- 已取得 BIOS 主畫面或可清楚顯示平台/版本資訊之頁面截圖。
- 已有 `vision_analyze` 或人工讀值可得到明確字串，且可唯一判讀。

### 回填規則
1. 來源優先序：full probe `BOARD_PLATFORM_REV_VAL` / `BOARD_BIOS_REVISION_STR` > BIOS 圖 fallback > project/spec fallback。
2. probe 值存在時必須使用 probe，不得由 BIOS 圖覆寫。
3. probe 缺值且 BIOS 圖單一值、高可讀性，或多張圖讀到同值時，才回填輸出 INI 的 `[Information]`。
4. 多張 BIOS 圖讀到不一致值：
  - 不回填，標記 `AMBIGUOUS_INFO_HEADER`（含衝突欄位名）。
5. 字串不完整或 `vision_analyze` 信心不足：
  - 不回填，標記 `AMBIGUOUS_INFO_HEADER`。

### 不可由 BIOS 圖推導的欄位
- `SusiAi=`
- `FollowConfigure=`

> 上述兩欄屬流程/策略參數，應由專案預設或 orchestrator 策略決定，不可依 BIOS 圖硬推。

### 與過濾流程邊界
- R-008 僅負責 `[Information]` header 補值，不參與 section/item 候選過濾。
- 不可用 `PlatformVersion` / `BIOSVersion` 直接刪除或新增 `HWM/SMBus/I2C/VGA` 項目。

## 規則 R-009：spec.json 與 BIOS 的後處理優先序

### 適用情境
- 本流程以「DB query 候選」作為 input，進入 BIOS/圖面後處理時，`spec.json` 與 BIOS 圖判讀結果出現衝突。

### 裁決原則
1. **HWM section/item：BIOS 優先（使用者決策 1B）**
  - `HWM.Voltage/Current/Temperature/Fan/Fan.Control/CaseOpen` 的存在性與 item 白名單由完成分析的可用 BIOS Hardware Monitor evidence 決定。
  - spec 即使明確 true、false、空 list 或非空 list，都只作 intent/衝突 trace；不得強迫新增 BIOS 未顯示的 HWM section/item，也不得阻止 BIOS 已顯示的 HWM section/item。
2. **非 HWM section：不得用 BIOS 缺席裁決**
  - SMBus、I2C、VGA、WDT、GPIO、StorageArea、ThermalProtect 等依各自 spec/probe/DB/電路圖 gate；BIOS Hardware Monitor 畫面未出現它們是正常現象。
3. **DB 候選為基底，不是存在性真相**
  - DB row 只提供可套用的 tuple 候選；HWM 仍需經 BIOS 白名單裁切，非 HWM 則依其專屬規則裁切。
4. **BIOS 不推導 tuple**
  - BIOS 可決定 HWM section/item 與 Name/alias，但不得自行推導 channel/hwid/io_port/option。

### 衝突處理
- HWM 的 spec 與 BIOS 互斥時：依 BIOS 結果產生/剔除，並記錄 `SPEC_BIOS_HWM_CONFLICT_BIOS_WINS` 與兩邊 evidence。
- 非 HWM 的 spec 與 BIOS 互斥時：不得套用此 HWM 優先規則，回到該 section 專屬 gate。

### 邊界與限制
- BIOS 圖缺失、不可讀或尚未完成 scoped analysis 時，不得把「無 evidence」誤寫成「BIOS 明確未顯示」；應標記 pending/ambiguous，且不產生正式 HWM section。
- 不得據此跳過電路圖/實測層去硬推 channel/hwid/ioport。

## 規則 R-010：spec.json 中「舉例語意」內容一律不作為裁決依據

### 適用情境
- 在 spec.json 的文字欄位中，出現「示例/舉例/範例」語意，且後面帶有例示值（非明確需求值）。

### 判定原則
- 只要判定為「舉例語意」，其後例示內容一律視為 **non-authoritative example**，不可用於：
  - item 保留/刪除裁決
  - displayname 回填
  - channel/hwid/ioport 推論

### 常見語意線索（非封閉集合）
- `Ex:`
- `<EX:`
- `(Ex:`
- `舉例`
- `例如`
- `範例`
- `sample`
- `for example`
- `e.g.`

### 後續動作
1. 忽略該例示值，不納入裁決資料。
2. 若該欄位僅有例示、無明確值：標記 `SPEC_EXAMPLE_ONLY_IGNORED`。
3. 由其他來源（BIOS / DB / 電路圖 / 後續驗證）補足決策。

### 注意事項
- 使用者可能以多種自然語言表達「舉例」，判讀時採語意優先，不侷限固定字串。
- 若無法確定是否為舉例語意，標記 `SPEC_TEXT_AMBIGUOUS`，避免誤採信。

## 規則 R-011：HWM.Current / HWM.CaseOpen BIOS gate

### 適用情境
- DB query 已回傳 `HWM.Current` 或 `HWM.CaseOpen` 候選。
- 已完成可用 BIOS Hardware Monitor / PC Health 畫面分析。

### 裁決原則
- DB tuple、generic `hardware_monitor=true` 或 spec 勾選都不足以單獨啟用這兩個 subsection。
- BIOS 顯示 Current/Ampere 項目才產生 `HWM.Current`；BIOS 顯示 Case Open/Chassis Intrusion 項目才產生 `HWM.CaseOpen`。
- BIOS 有對應項目時，即使 spec 漏勾或空 list，仍可由 BIOS evidence 啟用。
- BIOS 沒有對應項目時，即使 spec 已勾選，仍標記 `SKIPPED_NOT_APPLICABLE`，移除 stale split/config artifacts，且不進 validation manifest/fallback。

### 對應項目線索（BIOS）
- `HWM.Current`：Current / Ampere / A 等電流監控項。
- `HWM.CaseOpen`：Case Open / Chassis Intrusion / Chassis Open Warning 類項目。

### 執行動作
1. 先讀 completed BIOS evidence，再用 spec 只記錄 intent/衝突。
2. BIOS evidence 成立才查詢/產生正式 section。
3. applicability 不成立時，先前錯誤生成造成的 API failure 不得觸發 route fallback。

### 邊界
- 本規則是 R-002/R-009 的 Current/CaseOpen 明確化；其他 HWM sections 同樣由 BIOS gate，但使用各自 label/evidence parser。
- 非 HWM sections 不套用本規則。

## 規則 R-012：Block Diagram 可用證據範圍與處理動作

### 觸發條件
- 專案圖資中存在可讀的 block diagram（平台架構圖/方塊圖）。

### 可採信範圍（可用於第一層過濾）
1. 功能存在性（存在/不存在）
   - 可用於判定 I2C / SMBus / WDT / Backlight / Brightness / GPIO 等功能是否在平台架構層被宣告。
2. 架構角色（方向級 ownership）
   - 可辨識 EC / SoC / PCH 與低速介面的大方向連接關係。
3. 橋接元件角色
   - 可辨識 CH7513A / IT8883 等橋接或轉換元件在路徑中的角色。
4. option 風險訊號
   - `BOM Option` / `CO-Layout` / `option` 字樣可作為「可能不實裝」的降權依據。

### 必做動作
- 明確標示存在的功能：`KEEP_CANDIDATE`
- 出現 option 語意：`DOWNRANK_OPTIONAL`
- ownership 僅方向可見、無法定案：`MARK_OWNERSHIP_AMBIGUOUS`
- 需要最終裁決的項目：`NEED_SCHEMATIC_PROOF`

### 明確禁止
- 不可由 block diagram 直接填寫 ini 硬值（channel/hwid/ioport）。
- 不可由 block diagram 直接裁決 backlight 最終通道數量。
- 不可將 `option` 項目視為「已實裝」。

### 與其他規則邊界
- R-012 僅負責第一層候選保留/降權與風險標記。
- 最終裁決仍由 BIOS（R-003/R-009）與電路圖 net-level 證據（R-007）處理。

## 規則 R-013：AIMB + NCT6126D 的 V5SB 實際量測 +3.3V 特例（需電路圖強證據）

> **適用範圍：只適用 SIO（`NCT61**D*`）。** EC 與 EIO-300 / `NCT6694B*` 複合晶片的 HWM.Voltage 走 DB 路線，不看分壓圖（orchestrator 9.1）。

### 背景
- `ProductChip = (AIMB, NCT6126D)` 時，DB 的 `HWM.Voltage` 常態候選通常包含 `V5SB`，這在多數 AIMB 平台是正確設計。
- 但已確認存在平台特例（目前已知：`AIMB-522`、`AIMB-523`）：
  - 實際硬體路由為 `VIN0/ATX_5VSB` 腳位量測 `+3.3V`（而非 5VSB）。
  - BIOS 也顯示 `+3.3V` 類項目，未出現 `5VSB`。

### 觸發條件（必須同時成立）
1. `product_name == AIMB`
2. `chip_name == NCT6126D`
3. DB 查詢結果於 `HWM.Voltage` 含 `V5SB` item
4. 電路圖可取得「強證據」顯示：`VIN0/ATX_5VSB` 對應網路實際接 `+3.3V`（或等價 3V3 網名）

### 強證據定義（本規則）
- 必須可在 net-level 圖面同時建立以下關聯：
  1. `VIN0/ATX_5VSB` pin 對應到某輸入網（例：`SIO_+V3.3IN`）
  2. 該輸入網經分壓/連線來源可追溯到 `+V3.3`（或 `3V3`）
- 僅有 block diagram、僅有 BIOS 名稱、或僅有口述描述，皆不足以觸發本 key rename 特例。
- 強證據需持久化為 structured route hint，例如 `voltage_route_hints[{source_item: V5SB, actual_rail: +3.3V, evidence_level: NET_LEVEL_CONFIRMED}]`，讓 Python 可 deterministic 驗證；不可只靠自由文字臨時改 INI。

### 執行動作（命中特例時，使用者決策 2B）
1. 若 DB 只有 `V5SB` mapping，保留原 tuple/channel，但將正式輸出 INI key/item identity 由 `V5SB` 改為 `V33`。
2. 將 BIOS 擷取到的 `+3.3V`（或等價別名）填入 `V33` 的 Name/alias。
3. 若 DB 同一 channel 已同時存在 `V33` 與 `V5SB` duplicate rows，保留既有 `V33` row、刪除同 channel 的 `V5SB` row，避免輸出 duplicate key。
4. alias bridge 記錄 `item_name_override=V33` 與可追溯標記：
   - `AIMB_NCT6126D_V5SB_RENAMED_TO_V33_BY_SCHEMATIC`

### 未命中特例時（預設路徑）
- 若缺少電路圖、或電路圖無法證明 `VIN0 -> 3.3V`：
  - 不改 key 或 Name/alias
  - 不刪除 `V5SB`
  - 維持 DB 常態輸出（`V5SB`）

### 風險控管
- 本規則僅限 `(AIMB, NCT6126D)`，不外推到其他 ProductChip。
- 若 BIOS 顯示 `+3.3V` 但沒有 structured net-level 電路圖強證據，僅標記：`BIOS_DB_LABEL_MISMATCH_PENDING_SCHEMATIC`，不得改 key/alias。

## 規則 R-014：SIO（`NCT61**D*`）HWM.Voltage 分壓圖判讀（通用；R-015 併入本規則）

> **適用範圍：所有 SIO（`NCT61**D*`，含 NCT6106D/6116D/6126D），不分產品線與型號。** EC 與 EIO-300 / `NCT6694B*` 的 HWM.Voltage 走 DB 路線，不看分壓圖（orchestrator 9.1）。

### 背景
- SIO 只有 `VIN0 / VIN1 / VIN2(AUXTIN)` 三個輸入接外部分壓電阻，只有這三路的 R1/R2 不為 0；其他電壓項目（VCORE、VBAT、3VCC…）R1/R2 一律是 0。
- DB 的 `HWM.Voltage` 對 R1/R2 都是 0，且 DB 的 rail↔VIN 對應只是「常見假設」。實際哪個 rail 接到哪個 VIN 因板子而異，**不得假設固定順序**（不可預設 VIN0=5VSB、VIN1=5V、VIN2=12V，也不可套固定左右順序）。

### 判讀流程（agent 看圖）
1. 在 SIO 晶片圖上找 `VIN0/VIN1/VIN2(AUXTIN)` 三支腳（記錄 package pin），沿 net 追到分壓電阻節點，再追到來源 rail。
2. 讀出每一路分壓的 R1（上臂，rail 側）與 R2（下臂，接地側）。
3. 以追線結果決定每個 VIN 實際接到哪個 rail；判斷依據是 net/rail 連線，不是 pin 的功能字串（如 `ATX_5VSB`）。
4. 看不清楚就裁更小範圍再放大（見 R-016 的裁圖與證據保留規則）；放大後仍不確定的 VIN 不要寫進 `routes`。
5. 結果寫入專案目錄 `<PROJECT>-voltage-vision-evidence.json`：

```json
{
  "evidence_images": ["<裁切圖路徑>"],
  "routes": [
    {"vin": "VIN0", "package_pin": 100, "net": "SIO_+V5SBIN", "rail": "+V5_DUAL", "r1": "30K", "r2": "10K"}
  ],
  "analysis_status": "DONE_VISION_ANALYZE"
}
```
- `r1`/`r2` 照圖面原樣填（含小數與單位，例 `40.2K`），縮放由程式處理。
- `vin` 為 `VIN0`/`VIN1`/`VIN2`（AUXTIN 即 VIN2）。

### 程式處理（`susi_gen._apply_superio_voltage_divider`，SIO 一律執行）
1. 由 `rail`/`net` 文字決定電壓 item（`+5VSB`/`+V5_DUAL`→`V5SB`、`+5V`→`V50`、`+12V`→`V120`、`+3.3V`→`V33`、`+3VSB`→`V3SB`；負電壓不處理）。
2. 該 item 的 `Channel` 改為圖證的 VIN channel（`VIN0=0x80000000`、`VIN1=0x80000001`、`VIN2=0x80000002`），`R1/R2` 填入縮放後整數。
3. 沒有出現在 `routes` 的 item，R1/R2 一律寫 `0`。
4. 若其他 item（非圖證 rail）佔用了被圖證認定的 VIN channel（例：DB 中 `V33` 與 `V5SB` 同為 `0x80000000`），該列刪除（`dropped`）。
5. 路線加 `+SUPERIO_VOLTAGE_DIVIDER_SCHEMATIC`，matrix 的 `voltage_divider` 記錄每條 route 結果、已套用 item、未對上 item、被刪除列。

### R1/R2 整數縮放（INI 不可有小數點）
- INI 的 R1/R2 必須是整數，且 R1:R2 比例不可變。
- 兩個電阻（以 K 為單位）以同一個 10 的次方等比例放大，直到兩者都是整數：`40.2K/10K → 402/100`、`30K/10K → 30/10`、`5.6K/1.2K → 56/12`。
- 不可只放大有小數的那一個。

### 證據不足
- 沒有證據檔、檔案無 `routes`、或沒有任何可用 route：不改動 R1/R2（維持 DB 值），matrix `voltage_divider_status=HWM_VOLTAGE_DIVIDER_EVIDENCE_INSUFFICIENT`，最終報告要列出此項，不可當成已處理。

### 輸出標記
- `HWM_VOLTAGE_DIVIDER_REMAPPED_BY_SCHEMATIC`、`HWM_VOLTAGE_DIVIDER_EVIDENCE_INSUFFICIENT`
- 不可輸出：非圖證據驅動的硬刪除、脫離 `VIN0/1/2` 與 net/rail 的主觀猜測。


## 方向原則（跨案適用）

1. 有證據才收斂
- 能從 BIOS/圖面明確判讀出的訊號，才用於候選保留/過濾/降權。

2. BIOS 缺席的語意依 section 類型分流
- 對已完成且可讀的 BIOS Hardware Monitor 頁面，某類 `HWM.*` item 未顯示即不產生該 HWM section/item（R-002/R-009，決策 1B）。
- 對非 HWM sections，BIOS 未出現是正常現象，不可由缺席硬刪；回到該 section 的 spec/probe/DB/電路圖規則。
- BIOS 圖缺失、不可讀或 analysis 未完成時，標記 pending/ambiguous，不得把缺證據冒充為明確缺席，也不得正式產生 HWM section。

3. 分層裁決
- BIOS 圖：HWM section/item 存在性與名稱白名單（第一層）。
- 電路圖/實測：路由、通道數、channel/hwid/io_port 最終裁決（第二層）。

4. 保守優先
- HWM 證據不足時不產生正式 section，保留 pending 狀態；非 HWM 證據不足時依其專屬規則保留候選或 pending，不跨層猜測。

## 備註
- 目前已落地分析規則二十條（R-001~R-020）；後續新增規則時延續編號。
- 建議在最終報告中記錄：
  - 觸發證據（BIOS 圖哪一張、擷取到的 CPU 字串/項目列表）
  - 實際執行結果（是否刪除 SMBus section、各 section 刪除哪些非 BIOS 項目）
  - 使用了哪些 soft mapping（以及是否被實測覆寫）

## 規則 R-016：電路圖 net 追線優先於文字位置對齊

> **適用範圍：SIO（`NCT61**D*`，起點 `SIO_GPIO*`）與 EIO-300 / `NCT6694B*` 複合晶片（起點 `EC_P*_GPIO*`）的 `[GPIO]`。** 其他 EC 的 GPIO 走 DB 路線，不追線（orchestrator 9.1）。

> 各晶片的起點訊號（`NCT6694B*`/`EIO-300*` → `EC_P*_GPIO*`、`NCT61**D*`/`NCT6776D*` → `SIO_GPIO*`、`EIO-211*` → `EC_GP*`）由程式 `_gpio_target_signal_pattern` 決定。
> 本節是 agent 追線的規則。呼叫視覺工具（例：`vision_analyze`）看圖時，把本節「範圍」「讀 PDF 電路圖」「追線方法」「function label 轉 group/bit」的內容寫進提問，不可只寫「依規則」。`prompts/gpio_trace.md` 是 py 備援專用的英文副本，改規則時兩邊一起改。

### 範圍：先建立目標訊號集合
- 只追 case 指定的目標 pattern 外部 GPIO 訊號（例：`EC_P*_GPIO*`、`SIO_GPIO*`）。
- 對應前先列出完整的目標訊號集合與數量；每個成員最後都要有對應結果或 AMBIGUOUS。
- `EC_P*_GPIO*` 要包含所有 port（P1、P2、P3…），不可追完第一組 port 就停。
- 目標 pattern 以外的訊號即使接到 GP* 腳也是 OUT_OF_SCOPE（例：FAN_SPEED*、*_PWM、*BEEP*、SIO_LED*、PORT80*），不輸出。

### 讀 PDF 電路圖與小字
- 先定位：在 PDF 文字中搜尋目標 pattern 或關鍵字，列出所有命中頁與區域；不可只看第一個命中或第一組 port。
- 先放大再判斷：每個命中區域都裁成聚焦的高解析圖，讓訊號名、走線轉折、接點、腳號與 function label 都清楚可讀。
- 整頁圖只能用來定位。字太小時不可據以判定，要再裁更小範圍、放更大；放大後仍看不清楚才標 AMBIGUOUS。
- 保留證據：裁切圖存在專案目錄（不可只放 /tmp），並列在 `meta.evidence_images`。（Fan 配對、電壓分壓等其他電路圖分析同樣適用。）

### 追線方法
- 外部訊號名只是起點；答案是走線實際接到的晶片腳旁印的 GPIO function label。
- 沿連續的電氣走線逐段追，包含轉折與垂直段。文字位置、上下排列、OCR 順序、名稱相似都不是連線證據。
- 最常見的錯誤是落到隔壁腳（例：`GPIOA6` 誤為 `GPIOA5`、`GPIO91` 誤為 `GPIO90`）。採用對應前，確認走線末端的腳號，並讀同一腳位列上的 function label。
- 交叉處沒有接點（junction dot）不算連接；只在接點處分岔。
- `X`/`NC` 標記只有在同一腳或同一走線末端、且走線在那裡結束時才算數。
- 訊號名旁的 `<數字>` 標記（例：`EC_P1_GPIO2 <49>`）不改變判斷。同一張圖上有連續走線接到晶片腳就照常對應；沒有走線接到晶片腳才標 AMBIGUOUS。
- `<數字>` 只是列出同一條 net 出現的其他頁，絕不代表這一頁沒有走線。要從 port/BI 符號**與標記相反的那一側**追（走線可能往左接到晶片），並穿過串聯的 0Ω 電阻／跳線一路追到晶片腳。
- 走線被截斷、端點不清楚、或分不出是走線還是標註時，標 AMBIGUOUS，不猜。

### function label 轉 group/bit
- `GPxy` / `GPIOxy` → group x、bit y（例：`GPIO34` → 3,4；`GP50` → 5,0）。
- group 那一位可能是十六進位：`GPIOA5` → 10,5；`GPIOB0` → 11,0；`GPIOD0` → 13,0。
- group/bit 只能由晶片 function label 解出，不可由外部訊號後綴推導（`EC_P2_GPIO5` 不代表 group 2 或 bit 5）。程式另以 `_parse_gpio_function_label` 解析並覆蓋。
- 晶片封裝腳號（例：`F1`、`L6`）只是輔助證據，看得清楚才記錄。
- 不決定 INI key。generator 依訊號順序編 `GPIO00`、`GPIO01`…（`EC_P1_GPIO0..7`，接著 `EC_P2_GPIO0..7`…；`SIO_GPIOn` 依 n）。晶片 function label 填在 `function_label`，不可填進 `report_name`。

### 驗證案例：ARK-1251
- `EC_P1_GPIO4` 的文字位置接近 `ESPI_ALERT#/GPIOB3`，但實際 wire 先水平延伸、再向下摺線，最後接到 `F1`。
- 因此正確 mapping 為：
  - `EC_P1_GPIO4 -> F1 -> SHD_CS#/CLKRUN#/ESPI_CS2#/GPIOB1`
- 錯誤 mapping `EC_P1_GPIO4 -> GPIOB3` 是由文字 Y 座標對齊造成，違反本規則的追線優先序。

## 規則 R-017：清楚 GPIO 圖面的 physical pin map 與 SUSI logical GPIO 分層

> **適用範圍：SIO（`NCT61**D*`，起點 `SIO_GPIO*`）與 EIO-300 / `NCT6694B*` 複合晶片（起點 `EC_P*_GPIO*`）的 `[GPIO]`。** 其他 EC 的 GPIO 走 DB 路線，不追線（orchestrator 9.1）。

### 目的
- 當電路圖直接顯示 GPIO group/port/pin function 與外部 net 的清楚接線時，建立可追溯的 physical pin mapping。
- 避免把 request form/JSON 的 SUSI logical location（例如 `GPIO1`）誤當成晶片 function label（例如 `GPIO0_P0_0`）。

### 觸發條件
- 電路圖可清楚辨識 chip name、chip pin endpoint、pin number 或座標，以及 chip 內部的 GPIO function label。
- 外部 signal/net 以連續 wire 直接接到該 pin，或可透過明確的 0R/Co-lay 路徑追到該 pin。

### 常見適用 chip（經驗提示，非硬限制）
- R-017 常見於一般 EC / SuperIO / SoC GPIO 圖面（例如 `ITE`、`ENE`、`EIO-211` 等）。
- 只要圖面能建立清楚 physical pin map 與 function label 分層，就應套用；不因 chip 家族不同而豁免。

### 必做輸出
1. 主要輸出先建立 signal 到 GPIO function label 的 mapping：
  - `external signal/net -> chip GPIO function label`
  - 只處理 R-016 `分析範圍 Gate` 通過的 `target_signal_set`；`OUT_OF_SCOPE` signal 不得進入 mapping 表。
2. function label 正規化成 group/pin（含十六進位 group，例：`GPIOB0` → 11,0）：規則見 R-016「function label 轉 group/bit」。group/pin 只能由 function label 解出，不可由外部訊號後綴推導。
3. chip package pin number（例如 `L6`、`M6`、`A12`）只作追線證據/除錯欄位，不是主要 GPIO mapping 結果；若圖面可讀，才附加記錄。
4. 再輸出 SUSI logical mapping（若 form/JSON 有 GPI/GPO 項目）：
  - `logical GPI0/GPO0 -> normalized group/pin`（只有在 wire/order/文件證據支持時才能建立）
5. `function_label`、`normalized_group_pin`、`chip_package_pin`、`logical_location` 必須分欄保存，不得只用單一 `location` 欄位混合表示。

### 清楚圖面的判定規則
- 同一 GPIO group 中連續排列、且每條 wire 明確接至連續 chip pin 的情況，可依 pin endpoint 建立完整 group map。
- 若圖面直接顯示 `GPIO0_P0_0` 到 `GPIO0_P0_7`，應完整列出 8 條，不得只回報 `count=8`。
- `count` 只能代表數量，不代表 group、port、pin 或 direction 已完成 mapping。
- `GPI/GPO` 的 input/output 是 logical interface 方向；除非電路圖或晶片資料表提供方向證據，不得僅由 wire 左右方向推導晶片 pin direction。
- 使用者若要求 GPIO 對應，預設主要回答 `signal -> GPIO function label -> normalized group/pin`；除非特別要求，不以 chip package pin 作為主要答案。

### Form/JSON 衝突處理
- `location: GPIO1` 只記錄為 SUSI logical location；不可直接改寫成 physical `GPIO0_P0_*`。
- 若 form/JSON 只有 `index`、`direction`、`location`，但沒有 physical pin，標記 `LOGICAL_GPIO_WITHOUT_PHYSICAL_PIN_MAP`。
- 若電路圖提供 physical map，應保留原始 logical 欄位，並新增 physical 欄位；不可因圖面結果覆寫原始需求資料。
- 若 logical index 與 physical pin 的一對一順序無明確證據，標記 `LOGICAL_PHYSICAL_GPIO_MAPPING_AMBIGUOUS`，不得默認 index 順序相同。
- 例外（使用者規則）：`NCT6694B*` 與 SIO `NCT61**D*` 的電路圖路線，INI key 一律依訊號順序編為 `GPIO00`、`GPIO01`…（由程式編號，見 orchestrator 10.4），不需另外的 logical 對照，也不因需求表只列部分項目而停止。

## 規則 R-018：功能來源 chip 必須由 net-level 證據確認

### 目的
- 區分「同一顆 EC/SIO 同時承擔 HWM 與 GPIO」與「HWM、GPIO 分屬不同 chip 或 GPIO expander」的架構。
- 避免把某一個 chip family 的案例經驗硬套到其他平台。
- 明確規定 `-spec.json` 的 chip 欄位是候選/需求資料，不能取代電路圖的實際連線證據。

### 通用原則
- R-016/R-017 的 GPIO 追線與 physical mapping 規則不綁定特定 chip name。
- `EIO-211`、`NCT6694B`、`NCT6126D` 等名稱只能作為候選 chip identity，不能單獨證明 GPIO 或 HWM 的功能來源。
- 同一顆 chip 可以同時提供 HWM、GPIO、Fan、WDT 等功能；也可能只有其中一部分功能，必須逐功能判定。
- 本規則中的「功能來源 chip」是硬體連線判定，不是 Skill 的所有權或軟體權限。
- GPIO mapping source 由 orchestrator 決定；指定 `schematic` 時，依 R-016/R-017 追線與正規化，不混用 auto report 結果。
- EC route 的 GPIO 數量、DB template default 與 `spec.json` 名稱由 orchestrator 的 GPIO mapping flow 處理；本規則只處理 schematic route 的追線與正規化。

### 必做判定流程
1. 分別建立功能來源：`HWM -> chip`、`GPIO -> chip`、`Fan -> chip`、`WDT -> chip`。
2. 先讀 `-spec.json`/form 作為候選；再從電路圖追線確認，不能反過來用候選 chip 名稱推導連線。
3. 對 GPIO：從 GPIO external signal 沿 wire 追到實際 GPIO pin/function；若先進入 `TCA9555`、IO expander、level shifter 或其他 bridge，GPIO 的直接功能來源應記為該元件，而不是上游 EC/SIO。
4. 對 HWM：從 `VIN/TEMP/FANIN/PWM` 或等價 net 追到實際監控 chip pin；不能因 GPIO 已判定為某 chip，就自動把 HWM 也歸給該 chip。
5. 若同一顆 chip 的不同 pin group 分別承擔 HWM 與 GPIO，可標記 `SAME_CHIP_MULTI_FUNCTION_CONFIRMED`，但仍要分別列出 pin/function mapping。
6. 若只有 chip 名稱或 form 的 `Chip` 欄位，沒有 net-level pin 證據，標記 `FUNCTION_OWNERSHIP_NEEDS_SCHEMATIC_PROOF`。

### 證據優先序
1. chip pin endpoint 與連續 wire 的 net-level 連線。
2. 明確的 bridge/expander 與其 GPIO pin 連線。
3. schematic block label 或 chip function label。
4. block diagram 的功能方塊連線，只作架構方向證據。
5. form/JSON 的 chip 欄位與 chip family 經驗，只能作候選或交叉驗證。

### 輸出契約
- 建議使用不易誤解的欄位名稱：
  - `gpio_function_source`
  - `hwm_function_source`
  - `fan_function_source`
  - `physical_pin_map`
- 若保留舊欄位，`gpio_owner`/`hwm_owner` 等只能作為相容 alias，語意等同 `*_function_source`。
- 若功能來源不同，不能以單一 `chip` 欄位合併表示。
- 若功能來源尚未由圖面證明，保留多候選並標記 ambiguity，不得因歷史案例自動套用。
- 若 `-spec.json` 與電路圖衝突，保留兩邊值並標記 `SPEC_SCHEMATIC_FUNCTION_SOURCE_CONFLICT`；net-level 電路圖值為最終硬體判定值。

### 已知案例邊界
- SOM-6833 的 `EIO-211` 可由圖面確認其 GPIO 與 Smart Fan 等功能路由到同一顆 EC，但此結論只適用於該案的圖面證據。
- `NCT6694B/EIO-300` 案例不可僅依 chip 名稱推定 GPIO 一定由 NCT6694B 直接承擔；若圖面出現 `TCA9555` 或其他 expander，應以實際 expander pin route 為準。

## 規則 R-019：FAN IN/OUT 配對判讀（先分析再判定）

> **適用範圍：只適用 SIO（`NCT61**D*`）。** EC 與 EIO-300 / `NCT6694B*` 複合晶片不做 Fan 配對、不產生 `fan-pairing.json`；它們的 Fan / Fan.Control 由 DB + probe + BIOS 產生（orchestrator 9.1、10.7）。

### 目的
- 為 `[HWM.Fan]` / `[HWM.Fan.Control]` 提供可機器化的配對結果。
- 僅處理「可由圖證確認」的配對，不做 naming 猜測。

### 分析範圍
- 優先：`circuit*.png|jpg|jpeg`。
- 證據不足時：追加 `circuit*.pdf`（尤其 fan control 頁）做交叉確認。
- 命名提示可包含：`FAN_SPEED*`、`FAN_TACH*`（IN）與 `*_PWM`（OUT），但最終必須回到 net-level 連線判定。

### 判定流程
1. 先找 IN 路徑：`FAN_SPEED*` / `FAN_TACH*` -> `*FANIN*` function label（如 `CPUFANIN`/`SYSFANIN`/`AUXFANINx`）。
2. 再找 OUT 路徑：`*_PWM` -> `*FANOUT*` 或等價 fan control 輸出鏈。
3. 以同一路徑/同一控制群組建立 IN/OUT pairing。
4. 全部配對完成後才判定是否 one-to-one；不可前置假設 one-to-one。
5. 一對多（多個風扇共用同一組 PWM 控制）時，會另外提供 Fan Control 電路圖；從該圖判斷哪幾個風扇共用同一個控制來源，共用者的 `control_idx_candidate` 相同。

### 訊號採信範圍（FAN）
- `[HWM.Fan]` 主訊號僅採 `*FAN_TACH*` / `*FAN_SPEED*`（IN 端證據）。
- `[HWM.Fan.Control]` 主訊號僅採 `*FAN_PWM*`（OUT/控制來源證據）。
- `*FAN_SD#*`、`*FAN_MODE*` 僅作輔助交叉驗證，不可單獨作為配對或 idx 主依據。

### 輸出契約
- 必輸出：`fanin_label`、`fanin_signal`、`fanout_signal`、`fanin_idx_candidate`、`control_idx_candidate`。
- `fanin_idx_candidate` 的語意順序可用：`CPU -> 0`、`SYS -> 1`、`AUX0 -> 2`、`AUX1 -> 3`（僅在圖證支持時）。
- idx 是 SUSI 的 fan 順序，**不是**晶片腳的功能編號：圖上的 `TA4`、`PWM4`、`FANIN2` 這類數字不可直接當成 idx。
- `control_idx_candidate` 依實際 PWM 控制來源網分組，不得由 connector 數量直接推定。
- 檔名/頁碼/截圖座標屬於選配除錯欄位；平常流程非必填。

### 狀態碼
- 一對一成立：`FAN_ONE_TO_ONE_CONFIRMED`
- 一對多成立：`FAN_ONE_TO_MANY_CONFIRMED`
- 證據不足：`FAN_PAIRING_NEEDS_FANCONTROL_EVIDENCE`
- 無法唯一收斂：`FAN_PAIRING_AMBIGUOUS`

---

## 規則 R-020：BIOS 圖讀取規則

> 本節是 agent 讀 BIOS 圖的規則。呼叫視覺工具（例：`vision_analyze`）時，把本節內容寫進提問，不可只寫「依規則」。`prompts/bios_reading.md` 是 py 備援專用的英文副本，改規則時兩邊一起改。

### 哪一頁算數
- 只有 Hardware Monitor / PC Health 頁（有即時讀值的列）才是 HWM section 的證據。
- 其他 BIOS 頁（CPU configuration、chipset/iManager configuration、顯示版本的 main 頁…）不是硬體監控證據，只回答：`No live hardware-monitor sensor rows are visible.` 否定句裡不要列感測器名稱（不寫「沒有 Case Open 項目」或「沒有風扇列」）。

### 要讀什麼
- 只回報圖上看得到的文字與數值，不猜、不補。
- 電壓 rail 名稱（例：`+12V`、`+5V`、`+5VSB`、`+3.3V`、`+9V`、`VBAT`、`VCORE`）。
- 溫度感測器名稱（例：`CPU Temperature`、`System Temperature`、`Chipset Temperature`）。
- 風扇名稱（例：`CPU FAN`、`System FAN`、`COM Module FAN`、`Carrier Board FAN`）。
- 電流讀值與 Case Open / Chassis Intrusion 即時狀態，只在有顯示數值或狀態時才算。

### 即時讀值與設定值
- 只採用有即時讀值的列（例：`CPU Temperature : 45 C`、`CPU FAN Speed : 2480 RPM`）。
- 門檻與動作設定不是感測器，不可當感測器名稱回報：例如 `CPU Shutdown Temperature`、`Warning Temperature`、`Throttle Temperature`、風扇 duty 或目標溫度設定。

### 數值
- Case Open / Chassis Intrusion 只有即時狀態才算（例：`Open`、`Closed`、`Yes`、`No`、`OK`）；`Case Open Detection`、`Chassis Intrusion [Disabled]` 這類設定不是讀值。
- 看得到即時值時連同單位回報：電壓 `V` 或 `mV`、溫度 `C`、轉速 `RPM`。
- 數值照圖上抄；看不到數值時只回報名稱。

## 人工搜圖建議

- 人工搜圖關鍵字僅供快速定位電路圖 net，不是判定規則，也不能取代實際 wire trace、pin endpoint 或其他圖證。
- 詳細內容請見 [人工搜圖建議](人工搜圖建議.md)。
