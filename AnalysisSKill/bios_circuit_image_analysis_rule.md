# Diagram Filter Skill (BIOS / Schematic Analysis Rules)

版本：v0.5（整併版）
角色：此檔即 diagram_filter_skill 的規則主檔（沿用既有檔名 `bios_circuit_image_analysis_rule.md`）。
適用範圍：AIagent_susi 專案中，針對 BIOS 圖片與電路圖做候選過濾與 ini 內容裁切。

## 規則 R-001：AMD EPYC 平台不保留 SMBus ini

### 觸發條件
- 在 BIOS 圖片可讀文字中，CPU 型號包含字串：`AMD EPYC`

### 判定
- 將平台判定為：**無南橋（No South Bridge）**

### 後續動作
- 若後續流程有找出 SMBus 設定（例如候選資料、DB query 結果、或暫存 ini 區段中存在 `[SMBus]` 內容），
  必須執行刪除動作：
  - 刪除整個 `[SMBus]` section，或
  - 至少清空其所有 key/value（以「不輸出 SMBus 實際設定」為最終結果）

### 不影響範圍
- 其他 CPU 類型目前**不套用**本規則，維持原流程與原判定。

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
- 可用於平台分流與例外規則判斷（例如：是否為 AMD EPYC 類型）。
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

---

## 人工找圖提醒（非 Skill 流程，只是搜尋字串規則）

> 目的：人工從電路圖快速定位關鍵 net，補強 Backlight 與 SMBus 的判讀證據。

### A. Backlight（沿用）

#### A-1. 本案（SOM-6833 / EIO-211）優先關鍵字
- `EC_LVDS_BKLT_CTL`
- `EC_LVDS_BKLT_EN#`

#### A-2. 常見同義關鍵字（跨平台可能改名）
- EN 類：
  - `BL_EN`
  - `BKLT_EN`
  - `BKL_EN`
  - `LCD_BL_EN`
  - `LVDS_ENABKL`
- CTRL/PWM 類：
  - `BL_PWM`
  - `BKLT_PWM`
  - `BKL_PWM`
  - `BKLT_CTL`
  - `LVDS0_CTRL`
- eDP/LVDS 前綴變體：
  - `eDP_BKLT*`
  - `LVDS_BKLT*`
  - `EDP_LVDS_BKLT*`

#### A-3. 建議搜尋順序（人工）
1. 先搜廣義：`BKLT`、`BKL`、`BL_`
2. 再搜本案詞：`EC_LVDS_BKLT_CTL`、`EC_LVDS_BKLT_EN#`
3. 追線確認是否同一組路徑：來源（EC/SoC/CH7513） -> option 電阻/切換點 -> connector 輸出（`LVDS_BKLT_EN`/`LVDS_BKLT_CTRL`）

#### A-4. 判讀提醒
- 命名不是全平台統一，不能只用單一字串下結論。
- 若是 option 電阻切換（例如 0R / NL），多半是「來源可切換」，不代表有兩組獨立背光裝置。

### B. SMBus（新增）

#### B-1. 先判「有沒有 SMBus」
- `SMBUS`
- `SMB`
- `SMB_CLK`
- `SMB_DAT`
- `SMB_SCL`
- `SMB_SDA`
- `SMBCLK`
- `SMBDAT`

#### B-2. 再判「來源是誰（PCH/EC）」
- PCH/SB/FCH 側：
  - `PCH_SMB*`
  - `SML0CLK`
  - `SML0DATA`
  - `SML1CLK`
  - `SML1DATA`
  - `South Bridge`
  - `FCH`
- EC 側：
  - `EC_SMB*`
  - `EC_SMB_CLK*`
  - `EC_SMB_DAT*`
  - `EC_I2C*`

#### B-3. 尋找「路由切換證據」（高價值）
- `CO-LAY` / `COLAY`
- `OPTION` / `BOM OPTION`
- `DNP` / `DNI` / `NL` / `NC`
- `0R` / `0Ω`
- `MUX` / `SWITCH` / `SEL`
- `R###`（選配電阻跳線點）

#### B-4. 端點與用途輔助關鍵字
- `B2B`
- `CONN` / `CN` / `J`
- `SPD` / `DRAM_SMB*`
- `EDID` / `DDC`
- `EEPROM` / `24Cxx`

#### B-5. 建議搜尋順序（人工）
1. 存在性：`SMBUS`、`SMB_CLK`、`SMB_DAT`
2. 來源線索：`SML0*`、`PCH_SMB*`、`EC_SMB*`
3. 找切換點：`CO-LAY`、`OPTION`、`NL`、`0R`
4. 追線：來源晶片腳位 -> 選配電阻/切換點 -> 連接器/端點

#### B-6. 判讀提醒
- block diagram 只算中證據：可支持「有功能」，不足以單獨決定 ownership。
- net-level 連線 + 切換件（0R/NL/OPTION）才是強證據，可用於候選保留/降權。
- 若 request 文字是 `PCH or EC ...` 且缺少上游 pin/net 追線，應維持 `SMBUS_CHIP_AMBIGUOUS`，不要硬收斂單一 chipname。

### C. [HWM.Voltage] 人工抓圖關鍵字提醒（新增）

> 用途：僅供人工在大量電路圖中快速定位 [HWM.Voltage] 相關區塊。

#### C-1. 搜尋關鍵字
- `H/W Monitor`
- `HW Monitor`
- `AVCC3`
- `AUXTIN`

#### C-2. 人工操作提醒
1. 先以 `H/W Monitor` 或 `HW Monitor` 搜尋，快速定位到電壓監控相關頁面/區塊。
2. 再以 `AVCC3` 或 `AUXTIN` 細化定位到 SIO 電壓監控區。
3. 確認同區塊可看到 `VIN2/AUXTIN`、`VIN1`、`VIN0`（或其左側對應網名）。
4. 確認後即可人工截圖，作為 [HWM.Voltage] 判讀證據。

### D. [HWM.Fan] / [HWM.Fan.Control] 人工抓圖關鍵字提醒（新增）

> 用途：僅供人工在大量電路圖中快速定位 FAN 監測與 FAN 控制相關區塊。

#### D-1. 搜尋關鍵字
- `EC_FANPWM0`
- `EC_FANPWM1`
- `EC_FANTACH0`
- `EC_FANTACH1`
- `SMART FAN`

#### D-2. 人工操作提醒
1. 先以 `EC_FANPWM0/1`、`EC_FANTACH0/1` 搜尋 EC/SIO 端 pin 區塊。
2. 以 `SMART FAN` 搜尋中間驅動/調理電路區塊（若有）。
3. 人工截圖時，優先保留同框證據：`PWM/TACH net 名稱 + 對應 pin/function + 連接去向頁碼/端點`。
