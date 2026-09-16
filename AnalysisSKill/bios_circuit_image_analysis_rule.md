# Diagram Filter Skill (BIOS / Schematic Analysis Rules)

版本：v0.5（整併版）
角色：此檔即 diagram_filter_skill 的規則主檔（沿用既有檔名 `bios_circuit_image_analysis_rule.md`）。
適用範圍：AIagent_susi 專案中，針對 BIOS 圖片與電路圖做候選過濾與 ini 內容裁切。

補充（PNG 判讀 fallback）：
- 預設先分析 `bios*.png` / `circuit*.png`。
- 若 `circuit*.png` 因解析度或裁切造成 pin/網名對位不穩，允許追加分析同案電路圖 PDF（常見檔名：`circuit*.pdf`）。
- PDF 主要用於補強「pin 編號 + 網名 + 功能名」對位證據；不可脫離圖證據做硬推。

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

## 規則 R-002：BIOS 項目白名單過濾（Query 後裁切）

### 觸發條件
- 已完成 DB query，且某 section 的候選設定項目數量 > BIOS 圖中可辨識的同類監測項目數量。

### 判定邏輯
- 以 BIOS 圖中實際可見的項目名稱建立白名單（label set）。
- 對應 section 的候選 key/value 只保留「名稱可對齊 BIOS 白名單」的項目。
- BIOS 圖未出現的項目，一律視為本輪不保留項目。

### 執行規則
- `保留 = Query候選項目 ∩ BIOS白名單項目`
- `刪除 = Query候選項目 - BIOS白名單項目`
- 不因為 DB 有值就新增 BIOS 圖未出現的監測項目。

### 範例（依提供 BIOS 圖）
- BIOS 可見：`CPU Temperature`、`COM Module FAN`、`Carrier Board FAN`、`+12V`、`+5V`、`VBAT`
- 若 query 在 `HWM.Voltage` 回傳 `+12V,+5V,+3.3V,VBAT`，則刪除 `+3.3V`，保留其餘三項。

### 注意事項
- 本規則是「BIOS 圖驅動的裁切規則」，目的是降低考古候選過量帶來的誤配。
- 若 BIOS 圖品質不足或 `vision_analyze` 無法穩定辨識，需標記 `AMBIGUOUS_BIOS_ITEMS`，交由人工覆核，不可默默保留全部。

## 規則 R-003：BIOS 功能存在性 Gate（只做保留/降權，不做最終通道裁決）

### 觸發條件
- 已取得 BIOS 設定頁截圖（Advanced/Chipset/PC Health 等）。
- 已有 DB query 候選資料作為 input。

### 判定邏輯
- 只要 BIOS 可見某功能的設定項，即判定該 section 為「存在候選」。
- 若 BIOS 完全未出現某功能，不直接判死，但標記為「低優先候選」。

### BIOS -> Section 對應（通用）
- `I2C* Control` -> `I2C`
- `SMBus* Control` -> `SMBus`
- `Backlight*` -> `VGA.Backlight`
- `Brightness* PWM*` -> `VGA.Brightness`
- `Smart Fan*` -> `HWM.Fan.Control`
- `PC Health / Hardware Monitor` 子頁 -> `HWM.*`（Voltage/Temperature/Fan）

### 注意事項
- 本規則只確認「功能存在可能性」，不確認 channel/hwid/ioport 正確性。
- 不可用此規則直接決定 Backlight1/2 數量（那是電路圖裁決層）。

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

### 執行規則
- 上述映射屬於「高機率」而非硬規則，必須保留可覆寫空間。
- 若同案實測/驗證顯示對應不同，實測優先並回寫案內 mapping。

### 交付格式提醒
- 產生 `-pre.ini` 階段通常不強填 display name。
- display name 建議在 machine B 驗證後回填，避免先驗假設造成誤標。

## 規則 R-005：BIOS 數值用途分層（Reference Only）

### 可做
- BIOS 即時值（溫度/電壓/RPM）可作為後續驗證比對基準（容差比對）。

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
1. 單一值且高可讀性：
  - 直接回填到輸出 ini 的 `[Information]`。
2. 多張 BIOS 圖讀到同值：
  - 視為一致，回填該值。
3. 多張 BIOS 圖讀到不一致值：
  - 不回填，標記 `AMBIGUOUS_INFO_HEADER`（含衝突欄位名）。
4. 字串不完整或 `vision_analyze` 信心不足：
  - 不回填，標記 `AMBIGUOUS_INFO_HEADER`。

### 不可由 BIOS 圖推導的欄位
- `SusiAi=`
- `FollowConfigure=`

> 上述兩欄屬流程/策略參數，應由專案預設或 orchestrator 策略決定，不可依 BIOS 圖硬推。

### 與過濾流程邊界
- R-008 僅負責 `[Information]` header 補值，不參與 section/item 候選過濾。
- 不可用 `PlatformVersion` / `BIOSVersion` 直接刪除或新增 `HWM/SMBus/I2C/VGA` 項目。

## 規則 R-009：spec.json 與 BIOS 衝突時的優先序（後處理裁決）

### 適用情境
- 本流程以「DB query 候選」作為 input，進入 BIOS/圖面後處理時，`spec.json` 與 BIOS 圖判讀結果出現衝突。

### 裁決原則
1. **BIOS 優先（最高優先）**
  - 若 BIOS 可清楚讀到項目存在性或名稱（含 `+5V` / `+5VSB` 這類可明確區分字串），以 BIOS 為準。
2. **spec.json 次優先**
  - 僅在 BIOS 無法清楚判讀、或該項在 BIOS 圖未出現時，才採用 spec.json。
3. **DB 候選為基底**
  - DB query 結果僅提供候選集合，需經 BIOS/spec 後處理裁切，不可直接視為最終輸出。

### 衝突處理
- `spec.json` 與 BIOS 互斥時：
  - 採 BIOS 判讀結果。
  - 記錄衝突標記：`SPEC_BIOS_CONFLICT_BIOS_WINS`（含欄位名與兩邊值）。

### 邊界與限制
- R-009 的「BIOS 優先」僅適用於本規則已允許的層級（功能存在性、名稱/alias 對應、displayname 回填）。
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

## 規則 R-011：HWM.Current / HWM.CaseOpen 的 BIOS 缺席即剔除（特例）

### 適用情境
- DB query 已回傳 `HWM.Current` 或 `HWM.CaseOpen` 候選。
- 進入 BIOS 圖後處理階段。

### 裁決原則
- 由於 `HWM.Current` 與 `HWM.CaseOpen` 在本流程中缺乏可行驗證路徑，採保守剔除策略：
  - 若 BIOS 圖未顯示對應項目，則從後續輸出 ini 移除該 section 候選資訊。

### 對應項目線索（BIOS）
- `HWM.Current`：Current / Ampere / A 等電流監控項。
- `HWM.CaseOpen`：Case Open / Chassis Intrusion / Chassis Open Warning 類項目。

### 執行動作
1. BIOS 有對應項目：可保留候選（仍屬低信心，待後續能力補證）。
2. BIOS 無對應項目：
   - 移除 `HWM.Current` 候選輸出。
   - 移除 `HWM.CaseOpen` 候選輸出。
   - 記錄標記：`FILTERED_BY_BIOS_NO_CURRENT_CASEOPEN_EVIDENCE`。

### 邊界
- 本規則僅為 `HWM.Current` / `HWM.CaseOpen` 特例，不外推至其他 section。

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

## 規則 R-013：AIMB + NCT6126D 的 V5SB->3.3V 特例改名規則（需電路圖強證據）

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
- 僅有 block diagram、僅有 BIOS 名稱、或僅有口述描述，皆不足以觸發本改名。

### 執行動作（命中特例時）
1. 不刪除 DB 的 `V5SB` 這筆候選（保留底層 mapping 連續性）。
2. 在 alias/display-name 回填階段，將該筆以 `V33` 語意輸出：
   - item 名稱由 `V5SB` 改為 `V33`
   - 將 BIOS 擷取到的 `+3.3V`（或等價別名）填入該 item 的 alias/display name。
3. 記錄可追溯標記：
   - `AIMB_NCT6126D_V5SB_RENAMED_TO_V33_BY_SCHEMATIC`

### 未命中特例時（預設路徑）
- 若缺少電路圖、或電路圖無法證明 `VIN0 -> 3.3V`：
  - 不改名
  - 不刪除 `V5SB`
  - 維持 DB 常態輸出（`V5SB`）

### 風險控管
- 本規則僅限 `(AIMB, NCT6126D)`，不外推到其他 ProductChip。
- 若 BIOS 顯示 `+3.3V` 但沒有電路圖強證據，僅標記：`BIOS_DB_LABEL_MISMATCH_PENDING_SCHEMATIC`，避免誤改名。

## 規則 R-014：AIMB + NCT6106D（SuperIO，非 EC）之 HWM.Voltage 三路分壓重建（AIMB-205）

### 背景
- 本規則屬 **SuperIO（非 EC）** 案例。
- `ProductChip = (AIMB, NCT6106D)` 時，DB query 回來的 `HWM.Voltage` 可能包含多個電壓項目，不只三項。
- 在本規則流程中，會先過濾掉 `R1/R2` 為 0 的候選；可進入三路分壓判圖的通常是有實際分壓值的候選。
- 目前已確認命中此特例的型號：`AIMB-205`。

### 觸發條件（必須同時成立）
1. `product_name == AIMB`
2. `chip_name == NCT6106D`
3. 已取得兩類電路圖證據：
   - 分壓電阻圖（可讀三路 rail/net 與 R1/R2）
   - SIO pin/net 圖（可讀 `VIN0/VIN1/VIN2(AUXTIN)` 對應）
4. 可建立 `rail/net -> VIN channel` 的可追溯映射

### 判定原則（重點）
- 不可先假設最右一路一定是 `V5SB`（其他平台可能是 `V33` 或其他項）。
- 必須依圖面標籤/net 證據判定每一路是什麼 rail，再決定對應 item。
- alias 不是在本規則判定；alias 由 BIOS 圖後續回填。

### 執行動作（命中時）
1. 以圖面證據重建三路對應：`rail/net <-> VIN0/VIN1/VIN2`。
2. 在 query 輸出中，僅對命中的三路候選覆寫：
   - `Channel`
   - `R1`
   - `R2`
3. `R1/R2` 填值規則：
   - 由分壓圖讀出的電阻值以「保比例」方式寫入。
   - 若有小數點，存值為 `實際值 x 10`（例：`56.2K -> 562`）。
   - 無小數則直接存整數（例：`30K -> 30`、`10K -> 10`）。
4. 其他欄位（`HW`, `IOPort/Device Address`, `option`, `offset`）維持既有模板。
5. 記錄標記：`HWM_VOLTAGE_DIVIDER_REMAPPED_BY_SCHEMATIC`。

### 已知案例（AIMB-205）
- 由 SIO 圖可讀到：
  - `VIN0 -> SIO_+12VIN`
  - `VIN1 -> SIO_+5VIN`
  - `VIN2/AUXTIN -> SIO_+5VSBIN`
- 因此三路判圖時應依圖面方向重建，不可套用固定左右順序假設。

### 未命中或證據不足
- 若缺少分壓圖或 pin/net 對應不足：
  - 不做自動重映射
  - 標記 `HWM_VOLTAGE_DIVIDER_EVIDENCE_INSUFFICIENT`

## 規則 R-015：AIMB + NCT6126D（SuperIO，非 EC）之 HWM.Voltage 三路去重與 alias 回填

### 目的
- 處理 `HWM.Voltage` 候選中「同一分壓 channel 被多個 item 佔用」的情況，
  以電路圖 + BIOS 名稱做保留/刪除與 alias 回填。

### 適用前提
1. `product_name == AIMB`
2. `chip_name == NCT6126D`（SuperIO，非 EC）
3. 已有 DB query 結果
4. 已有電路圖可判讀三路分壓（`VIN0/VIN1/VIN2`）
5. 已有 BIOS 電壓名稱可回填 alias

### 標準三路與圖面判讀原則
- NCT6126D 三路分壓預設由 **最右往左** 對應：
  - `VIN0 = 0x80000000`
  - `VIN1 = 0x80000001`
  - `VIN2 = 0x80000002`
- 判讀以圖上的 `VIN0/VIN1/VIN2` 及 net/rail 連線為準，
  不以 pin 的功能字串（如 `ATX_5VSB`）直接下結論。

### 執行流程
1. 先從 query 候選中篩出 `R1/R2 != 0` 的電壓列（分壓候選）。
2. 檢查三路 channel 唯一性：`0x80000000 / 01 / 02` 應各對應單一候選。
3. 若同一 channel 出現多列（例：`V33` 與 `V5SB` 同為 `0x80000000`）：
   - 以電路圖判定該 channel 實際 rail
   - 以 BIOS 名稱交叉確認
   - 保留符合圖證據者，刪除不符者
4. 對保留列回填 BIOS alias 與分壓值（R1/R2 依圖面）。

### 已知特例
- 標準假設常為：`VIN0(5VSB), VIN1(+5V), VIN2(+12V)`。
- 但 `VIN0` 可能接到其他 rail（不一定是 5VSB），需由圖面決定。
- 目前已知例外型號：`AIMB-522`、`AIMB-523`。

### 本規則輸出契約
- 可輸出：
  - `HWM_VOLTAGE_CHANNEL_DUPLICATE_DETECTED`
  - `HWM_VOLTAGE_ALIAS_BACKFILLED_BY_BIOS_AND_SCHEMATIC`
  - `HWM_VOLTAGE_ITEM_DROPPED_BY_SCHEMATIC`
  - `AMBIGUOUS_HWM_VOLTAGE_ALIAS`
- 不可輸出：
  - 非圖證據驅動的硬刪除
  - 脫離 `VIN0/1/2` 與 net/rail 的主觀猜測

## 方向原則（跨案適用）

1. 有證據才收斂
- 能從 BIOS/圖面明確判讀出的訊號，才用於候選保留/過濾/降權。

2. 看不到不等於不存在
- 圖面未出現或無法判讀的項目，不做硬刪除；僅代表該區塊過濾效果有限。
- 需標記 `AMBIGUOUS_BIOS_ITEMS`（或等價狀態）供後續人工/實測補證。
- 例外：`HWM.Current` / `HWM.CaseOpen` 依 R-011 可在 BIOS 缺席時直接剔除。

3. 分層裁決
- BIOS 圖：功能存在性與項目白名單（第一層）。
- 電路圖/實測：路由、通道數、channel/hwid/io_port 最終裁決（第二層）。

4. 保守優先
- 在證據不足情況下，寧可暫留候選進後續驗證，也不要提早誤殺。

## 備註
- 目前先落地分析規則七條（R-001~R-007），後續可逐條擴充（R-008...）。
- 建議在最終報告中記錄：
  - 觸發證據（BIOS 圖哪一張、擷取到的 CPU 字串/項目列表）
  - 實際執行結果（是否刪除 SMBus section、各 section 刪除哪些非 BIOS 項目）
  - 使用了哪些 soft mapping（以及是否被實測覆寫）

## 規則 R-016：電路圖 net 追線優先於文字位置對齊

### 目的
- 避免因 signal label 與鄰近 pin/function label 在垂直方向接近，誤把訊號配到相鄰 GPIO。
- 只適用於本次任務指定的目標 GPIO signal；包括 `EC_P*_GPIO*`、`SIO_GPIO*`、`EC_GP*` 的命名提示，不代表要把所有接到 `GP*` pin 的 signal 都納入。
- `EC_P1_GPIO4` 僅是本規則的錯判示例，不是唯一或特殊的分析對象；`EC_P2_GPIO*`、`EC_P3_GPIO*` 等其他 port 也必須套用相同流程。

### 常見適用 chip（經驗提示，非硬限制）
- R-016 常見於 `NUVOTON_NCT6694B` / `EIO-300` 類案例（外部 signal 常見 `EC_P*_GPIO*` 命名）。
- 但本規則不綁定型號；只要任務是 `signal -> pin -> function label` 的 net-level 追線，都必須套用。

### 判定優先序
1. 實際 electrical wire 的連續路徑（水平線、垂直線、摺線、轉折）。
2. junction、T-connection、pin endpoint 與 connector/net label 的連接關係。
3. chip pin number 與該 pin 旁的 GPIO function label。
4. signal label 的文字位置、字串相似度與上下排列順序，僅作候選定位，不得單獨裁決。

### 分析範圍 Gate（先篩選，再追線）
- 先依使用者指定的 signal pattern 建立 `target_signal_set`；只有在此集合中的 signal 才能進入後續 wire trace 與 mapping 輸出。
- 使用者只說「分析 GPIO」而未指定 pattern 時，才依 chip-aware naming hint 選定預設 pattern；不可把多個 pattern 或所有含 `GPIO`/`GP*` 的 signal 聯集納入。
- AIMB 的 `NCT6126D*`、`NCT6116D*`、`NCT6106D*`、`NCT6776D*` 預設目標為 `SIO_GPIO*`；本次分析只輸出 `SIO_GPIO* -> GP* function label`。這是預設搜尋入口，不是唯一合法命名。
- `SIO_GPIO*`、`EC_GPIO*`、`EC_P*_GPIO*`、`EC_GP*` 都是合法的 GPIO external-signal pattern；實際採用哪一個，依使用者指定、chip hint 或圖面中與目標 chip 相連的命名證據決定。
- 預設 pattern 找不到時，才搜尋上述替代 pattern；若只有一個替代 pattern 能與目標 chip 的 GPIO wire 形成一致集合，將它選為 `target_signal_set` 並記錄實際採用的 pattern。若有多個可能 pattern，標記 `GPIO_SIGNAL_SCOPE_AMBIGUOUS`，不可把它們聯集納入。
- 其他 signal 即使實際接到 `GP*` pin，也屬 `OUT_OF_SCOPE`，不可納入本次 GPIO mapping；例如 `FAN_SPEED2`、`FAN2_PWM`、`SIO_ERR_BEEP`、`SIO_LED*`、`SIO_PORT80_SEL`。
- 若使用者明確指定其他 signal pattern，使用者指定值優先於 chip-aware naming hint；若 scope 仍無法唯一決定，標記 `GPIO_SIGNAL_SCOPE_AMBIGUOUS`，不得擴大成全部 GPIO signal。

### Chip-aware signal naming hint（搜尋入口，非 mapping 規則）
- 依候選 chip identity 優先使用下列字串縮小 GPIO 搜尋範圍：
  - `NCT6694B*` / `EIO-300*` -> `EC_P*_GPIO*`
  - `NCT6126D*` / `NCT6116D*` / `NCT6106D*` / `NCT6776D*` -> `SIO_GPIO*`
  - `EIO-211*` -> `EC_GP*`
- 上述只是外部 signal 的命名提示；命中後仍必須依 R-016 追蹤實際 wire、pin endpoint 與 chip function label。
- `SIO_GPIO*`、`EC_P*_GPIO*`、`EC_GP*` 都是外部 net label 候選，不可由 suffix/index 直接推導 `GPxx` function、package pin 或 mapping 順序。
- 同一張圖可能有多顆 NCT/SIO/EC，命名提示不能單獨決定 GPIO owner；仍須依 R-018 判定實際功能來源。
- 若候選 chip 使用其他 net 命名，或命名提示未命中，仍可搜尋 `GPIO`、`EC_GPIO`、`SIO_GPIO`、`EC_GP`、`GP*`、connector net 與 chip function label 作為候選定位；搜尋結果不能未經 scope 確認就加入 `target_signal_set`。

### 必做追線流程
1. 先完成分析範圍 Gate，再掃描 `target_signal_set`；通用 GPIO/GP 搜尋只作候選定位或確認替代命名，不得把未符合目標 pattern 的 signal 加入清單。
2. 對清單中的每一條 signal，從 signal label 或 BI/BO/IN/OUT 箭頭的實際 wire endpoint 開始。
3. 沿 wire 逐段追蹤；遇到轉折時依 wire 的新方向繼續，不以文字所在的水平列代替連線。
4. 遇到 junction 才視為分支；單純交叉但沒有 junction 的線不可視為相連。
5. 追到 chip pin 後，記錄 pin number，再讀取該 pin 對應的完整 function label。
6. 只對 `target_signal_set` 輸出一對一 mapping 表：`signal -> chip pin -> GPIO function label`；每一條目標 signal 都必須有結果或 ambiguity 標記，`OUT_OF_SCOPE` signal 不得出現在表內。
7. 若 signal label 與 pin label 不在同一水平線，必須優先採用摺線後的實際 endpoint，並標記 `MAPPED_BY_WIRE_TRACE`。

### 證據與錯誤防護
- 只依連續 electrical wire、junction、net label 與 pin endpoint 判定；顏色、文字距離、上下排列與 OCR 座標不能單獨作為連線證據。
- review/annotation 只有在依結構確認未連到 pin、junction 或 net endpoint 時才排除；不可依固定顏色判定。
- X/NC 只有在附著於同一個 pin 或 wire endpoint，且 wire 在該處終止時，才能判定未連接；附近其他 pin 或 branch 的 X/NC 不影響 trace。
- revision/review 文字只作背景資訊，不得改寫目前 wire trace；若它本身是直接接在線上的 net label，才可納入追線。
- wire 被裁切、endpoint 不清，或無法區分 wire 與 annotation 時，標記 `GPIO_NET_TRACE_AMBIGUOUS`；若涉及 X/NC 衝突，加註 `reason=X_NC_MARKER_CONFLICT`，不得硬猜。

### 完整性要求
- 先列出 `target_signal_set` 的數量與完整清單。
- 每條目標 signal 都必須有 mapping；若 pin 無法確認，列出並標記 `GPIO_NET_TRACE_AMBIGUOUS`。
- `OUT_OF_SCOPE` signal 不得加入 mapping 表。

### 驗證案例：ARK-1251
- `EC_P1_GPIO4` 的文字位置接近 `ESPI_ALERT#/GPIOB3`，但實際 wire 先水平延伸、再向下摺線，最後接到 `F1`。
- 因此正確 mapping 為：
  - `EC_P1_GPIO4 -> F1 -> SHD_CS#/CLKRUN#/ESPI_CS2#/GPIOB1`
- 錯誤 mapping `EC_P1_GPIO4 -> GPIOB3` 是由文字 Y 座標對齊造成，違反本規則的追線優先序。

## 規則 R-017：清楚 GPIO 圖面的 physical pin map 與 SUSI logical GPIO 分層

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
2. 若 function label 可拆成 group/port/pin，必須正規化並作為主要結果：
  - `GPIO34` 或 `GP34` -> `group 3, pin 4`
  - `GPIO71` 或 `GP71` -> `group 7, pin 1`
  - 這只是 label 正規化；signal 是否真的接到該 function，仍依 R-016 追線判定。
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

### 目的
- 為 `[HWM.Fan]` / `[HWM.Fan.Control]` 提供可機器化的配對結果。
- 僅處理「可由圖證確認」的配對，不做 naming 猜測。

### 分析範圍
- 優先：`circuit*.png`。
- 證據不足時：追加 `circuit*.pdf`（尤其 fan control 頁）做交叉確認。
- 命名提示可包含：`FAN_SPEED*`、`FAN_TACH*`（IN）與 `*_PWM`（OUT），但最終必須回到 net-level 連線判定。

### 判定流程
1. 先找 IN 路徑：`FAN_SPEED*` / `FAN_TACH*` -> `*FANIN*` function label（如 `CPUFANIN`/`SYSFANIN`/`AUXFANINx`）。
2. 再找 OUT 路徑：`*_PWM` -> `*FANOUT*` 或等價 fan control 輸出鏈。
3. 以同一路徑/同一控制群組建立 IN/OUT pairing。
4. 全部配對完成後才判定是否 one-to-one；不可前置假設 one-to-one。

### 訊號採信範圍（FAN）
- `[HWM.Fan]` 主訊號僅採 `*FAN_TACH*` / `*FAN_SPEED*`（IN 端證據）。
- `[HWM.Fan.Control]` 主訊號僅採 `*FAN_PWM*`（OUT/控制來源證據）。
- `*FAN_SD#*`、`*FAN_MODE*` 僅作輔助交叉驗證，不可單獨作為配對或 idx 主依據。

### 輸出契約
- 必輸出：`fanin_label`、`fanin_signal`、`fanout_signal`、`fanin_idx_candidate`、`control_idx_candidate`。
- `fanin_idx_candidate` 的語意順序可用：`CPU -> 0`、`SYS -> 1`、`AUX0 -> 2`、`AUX1 -> 3`（僅在圖證支持時）。
- `control_idx_candidate` 依實際 PWM 控制來源網分組，不得由 connector 數量直接推定。
- 檔名/頁碼/截圖座標屬於選配除錯欄位；平常流程非必填。

### 狀態碼
- 一對一成立：`FAN_ONE_TO_ONE_CONFIRMED`
- 一對多成立：`FAN_ONE_TO_MANY_CONFIRMED`
- 證據不足：`FAN_PAIRING_NEEDS_FANCONTROL_EVIDENCE`
- 無法唯一收斂：`FAN_PAIRING_AMBIGUOUS`

---

## 人工搜圖建議

- 人工搜圖關鍵字僅供快速定位電路圖 net，不是判定規則，也不能取代實際 wire trace、pin endpoint 或其他圖證。
- 詳細內容請見 [人工搜圖建議](人工搜圖建議.md)。
