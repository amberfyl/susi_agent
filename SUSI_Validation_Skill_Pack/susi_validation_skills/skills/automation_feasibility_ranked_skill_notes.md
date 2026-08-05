# SUSI 14項技能說明（依自動化可行性高→低）

本文件用於「先做得動、再做完整」的 skill 規劃。
原則：
- 可行性高：給出可直接落地的 skill 說明（目標/最小流程/證據）
- 可行性低：不硬寫不確定流程，只描述困難點、前置條件與阻塞原因
- 所有項目都維持 L1~L6 思維，但低可行項目可先做到 L1~L4 + BLOCKED_*

---

## 1) [WDT]（最高）
### Skill 說明（可直接自動化）
- 目標：驗證 start/refresh/stop/timeout reset/recovery
- 最小自動流程：
  1. 查 capability + timeout range
  2. start→短等候→stop（確認不重啟）
  3. start→定期 refresh（確認不重啟）
  4. 經核准後執行 timeout reset + 開機續跑 checkpoint
- 主要證據：API 回傳碼、checkpoint、重啟時間誤差、reset reason

## 2) [StorageArea]
### Skill 說明（可直接自動化）
- 目標：驗證 query/read/write/read-back/restore/persistence
- 最小自動流程：
  1. query area size/capability
  2. 讀取核准 offset 的原始 bytes 並備份
  3. 寫入測試 pattern→讀回 byte compare
  4. finally 強制 restore 並二次驗證
- 主要證據：before/after bytes、pattern compare、restore_result
- 安全規則：無核准 offset 一律 BLOCKED_PARAMETER

## 3) [ThermalProtect]
### Skill 說明（可直接自動化）
- 目標：驗證 threshold get/set/read-back/restore，必要時 driver reload persistence
- 最小自動流程：
  1. 查 channel capability/range/unit
  2. 讀原值
  3. 設定合法測試值→讀回比對
  4. restore 原值並確認
- 主要證據：閾值前後值、合法範圍檢查、restore_result
- 備註：trigger action（風扇增速/降頻）可列為可選進階

## 4) [HWM.Temperature]
### Skill 說明（可直接自動化）
- 目標：驗證 API 讀值穩定性 + BIOS 對照 + 動態趨勢
- 最小自動流程：
  1. capability 對齊 INI
  2. 連續取樣 N 次，做 plausible range 檢查
  3. 與 BIOS 截圖值做時間接近對照（容差）
  4. CPU load 前後趨勢檢查（方向性）
- 主要證據：time series、BIOS 對照表、容差結果

## 5) [HWM.Voltage]
### Skill 說明（可直接自動化）
- 目標：驗證電壓通道映射與讀值可信度（含 BIOS 對照）
- 最小自動流程：
  1. capability + 通道名稱映射確認
  2. 重複取樣做穩定性檢查
  3. BIOS 值對照（容差）
- 主要證據：通道對照、樣本統計、BIOS 差值
- 備註：若 BIOS 無該 rail，可維持 L1~L4 + BLOCKED_REFERENCE

## 6) [HWM.Fan]
### Skill 說明（可直接自動化）
- 目標：驗證 RPM 讀值、通道映射與趨勢
- 最小自動流程：
  1. capability + channel map
  2. 連續讀 RPM，檢查 0/突跳/噪聲異常
  3. 與 BIOS 風扇值對照
  4. 若可控刺激（負載或 fan control），驗證方向性
- 主要證據：RPM time series、對照誤差、趨勢判斷

## 7) [HWM.Current]
### Skill 說明（可部分自動化）
- 目標：先完成 API 層與 BIOS 對照可行範圍
- 最小自動流程：
  1. capability + channel map
  2. 重複取樣 + plausible range
  3. 有 BIOS 時做容差對照
- 主要證據：取樣統計、BIOS 差值
- 限制：若平台無穩定電流參考，L5 僅能做趨勢，非絕對精度

## 8) [HWM.Fan.Control]
### Skill 說明（中等可行）
- 目標：驗證 set/get + 對應 fan RPM 變化 + restore
- 最小自動流程：
  1. 讀取原 mode/value
  2. 寫入測試序列（如 20/40/60/80）並讀回
  3. 每步取對應 fan RPM 平均值
  4. restore 原模式與原值
- 主要證據：control 值讀回、RPM 響應、restore_result
- 限制：多風扇隔離驗證常需額外治具/明確通道映射

## 9) [VGA.Brightness]
### Skill 說明（中等可行）
- 目標：驗證亮度 range/set/get/restore
- 最小自動流程：
  1. query range
  2. 設定 0/25/50/75/100 並讀回
  3. restore 原值
- 主要證據：set/get 一致性、range 合法性
- 限制：若無 lux/camera/PWM fixture，L5 只能算 API-level PASS

## 10) [VGA.Backlight]
### Skill 說明（中等可行）
- 目標：驗證 on/off set/get/restore
- 最小自動流程：
  1. 讀原狀態
  2. set off→read back
  3. set on→read back
  4. restore
- 主要證據：state transition、restore_result
- 限制：無外部量測時，難證明「物理背光真的變化」

## 11) [HWM.CaseOpen]
### 困難點（先不硬寫完整流程）
- 常見問題：需要實體開關/跳線或機構觸發，純軟體難閉環
- 主要阻塞：
  - 無可程式控制的 case-open fixture
  - 訊號去抖/延遲行為未知
  - BIOS/SUSI 對同一事件時間點可能不同步
- 建議前置條件：提供可重複觸發的外部治具與事件時間戳

## 12) [GPIO]
### 困難點（先不硬寫完整流程）
- 常見問題：完整 L5 需要 loopback 或 fixture，多 pin 會牽涉人工換線
- 主要阻塞：
  - 無固定 pin pairing 圖
  - 電平標準與保護條件不明
  - 多路測試時易互相干擾
- 建議前置條件：提供固定 loopback 線表或自動切換矩陣治具

## 13) [I2C]
### 困難點（先不硬寫完整流程）
- 常見問題：沒有 golden slave/register 就無法判定對錯
- 主要阻塞：
  - 不可寫寄存器白名單缺失
  - repeated-start/timeout 行為未定義
  - 不同板卡 bus 上設備差異大
- 建議前置條件：每 channel 提供「地址/寄存器/期望值/可寫遮罩」

## 14) [SMBus]（最低）
### 困難點（先不硬寫完整流程）
- 常見問題：除基本讀寫外，還牽涉 protocol 變體、PEC、block length
- 主要阻塞：
  - 需可控 SMBus fixture 才能穩定重現 PEC 與錯誤情境
  - word byte order/command code 常有平台差異
  - 負向案例（NACK/錯誤 PEC）難在無治具下自動化
- 建議前置條件：提供可注入錯誤的 fixture 或可編程從裝置

---

## 統一輸出建議（所有項目）
- case result 一律輸出：
  - result, validation_layers(L1~L6), restore_result, evidence, suspected_layers
- 無法驗證時，不要硬判 FAIL：
  - 用 BLOCKED_PARAMETER / BLOCKED_REFERENCE / BLOCKED_FIXTURE
- 低可行項目先交付「可追溯阻塞資訊」，等 fixture/參數齊備再升級為完整自動化 skill

## 後續驗證注意事項（提醒）
- 這段是提醒清單，不是硬規則；實際判定以當前流程文件與測試上下文為準。
- 驗證時優先保留可追溯證據：原始 API 回傳、時間戳、環境條件、前後值對照。
- 若缺參數/治具/參考基準，不硬判 FAIL，改標記 BLOCKED_* 並寫清阻塞原因。
- 任何修正應落在流程、程式、或資料來源（DB/設定），避免人工手改結果檔。
- 每次調整後至少做最小回歸：重跑相關 section，確認結果可重現、差異可解釋。
- 協作時先執行可驗證步驟再下結論；遇到失敗要直接回報阻塞點與可行替代路徑。
