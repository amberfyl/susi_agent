# QueryDB 規格筆記（config.db）

路徑：`/home/company2/AIagent_susi/config.db`
用途：記錄目前可查詢 table 與欄位（依現況，非完整體）。

邊界：
- 本文件只描述 schema（資料面）。
- Query 規則、status 契約、流程責任邊界，一律以 `candidate_query_skill.md` 為準。

## 1) ProductChip

用途：以 `product_name + chip_name` 命中唯一 `prod_chip_id`，再到各 section table 查值。

欄位：
- `id` (INTEGER, PK)
- `product_name` (TEXT, NOT NULL)
- `chip_name` (TEXT, NOT NULL)
- `hardware_id` (TEXT, NOT NULL)
- `config_chip` (TEXT, NOT NULL)

約束：
- `UNIQUE(product_name, chip_name)`

---

## 2) WDT

欄位：
- `id` (INTEGER, PK)
- `prod_chip_id` (INTEGER, FK -> ProductChip.id)
- `item_name` (TEXT, NOT NULL)
- `channel` (TEXT, NOT NULL, default "")
- `io_port` (TEXT, NOT NULL, default "")
- `options` (TEXT, NOT NULL, default "")
- `disp_name` (TEXT, NOT NULL, default "")

約束：
- `UNIQUE(prod_chip_id, item_name, channel, io_port)`

---

## 3) StorageArea

欄位：
- `id` (INTEGER, PK)
- `prod_chip_id` (INTEGER, FK -> ProductChip.id)
- `item_name` (TEXT, NOT NULL)
- `channel` (TEXT, NOT NULL, default "")
- `io_port` (TEXT, NOT NULL, default "")
- `options` (TEXT, NOT NULL, default "")
- `disp_name` (TEXT, NOT NULL, default "")

約束：
- `UNIQUE(prod_chip_id, item_name, channel, io_port)`

---

## 4) ThermalProtect

欄位：
- `id` (INTEGER, PK)
- `prod_chip_id` (INTEGER, FK -> ProductChip.id)
- `item_name` (TEXT, NOT NULL)
- `channel` (TEXT, NOT NULL, default "")
- `io_port` (TEXT, NOT NULL, default "")
- `options` (TEXT, NOT NULL, default "")
- `disp_name` (TEXT, NOT NULL, default "")

約束：
- `UNIQUE(prod_chip_id, item_name, channel, io_port)`

---

## 5) VGA.Backlight

> 注意：table 名稱含 `.`，SQL 需加雙引號：`"VGA.Backlight"`

欄位：
- `id` (INTEGER, PK)
- `prod_chip_id` (INTEGER, FK -> ProductChip.id)
- `item_name` (TEXT, NOT NULL)
- `channel` (TEXT, NOT NULL, default "")
- `io_port` (TEXT, NOT NULL, default "")
- `options` (TEXT, NOT NULL, default "")
- `disp_name` (TEXT, NOT NULL, default "")

約束：
- `UNIQUE(prod_chip_id, item_name, channel)`

---

## 6) VGA.Brightness

> 注意：table 名稱含 `.`，SQL 需加雙引號：`"VGA.Brightness"`

欄位：
- `id` (INTEGER, PK)
- `prod_chip_id` (INTEGER, FK -> ProductChip.id)
- `item_name` (TEXT, NOT NULL)
- `channel` (TEXT, NOT NULL, default "")
- `io_port` (TEXT, NOT NULL, default "")
- `options` (TEXT, NOT NULL, default "")
- `range_max` (TEXT, NOT NULL, default "")
- `range_min` (TEXT, NOT NULL, default "")
- `frequency` (TEXT, NOT NULL, default "")
- `disp_name` (TEXT, NOT NULL, default "")

約束：
- `UNIQUE(prod_chip_id, item_name, channel, io_port)`

---

## 7) HWM.Current

> 注意：table 名稱含 `.`，SQL 需加雙引號：`"HWM.Current"`

欄位：
- `id` (INTEGER, PK)
- `prod_chip_id` (INTEGER, FK -> ProductChip.id)
- `item_name` (TEXT, NOT NULL)
- `channel` (TEXT, NOT NULL, default "")
- `io_port` (TEXT, NOT NULL, default "")
- `options` (TEXT, NOT NULL, default "")
- `disp_name` (TEXT, NOT NULL, default "")

約束：
- `UNIQUE(prod_chip_id, item_name, channel)`

---

## 8) HWM.CaseOpen

> 注意：table 名稱含 `.`，SQL 需加雙引號：`"HWM.CaseOpen"`

欄位：
- `id` (INTEGER, PK)
- `prod_chip_id` (INTEGER, FK -> ProductChip.id)
- `item_name` (TEXT, NOT NULL)
- `channel` (TEXT, NOT NULL, default "")
- `io_port` (TEXT, NOT NULL, default "")
- `options` (TEXT, NOT NULL, default "")
- `disp_name` (TEXT, NOT NULL, default "")

約束：
- `UNIQUE(prod_chip_id, item_name, channel, io_port)`

---

## 9) HWM.Voltage

> 注意：table 名稱含 `.`，SQL 需加雙引號：`"HWM.Voltage"`

欄位：
- `id` (INTEGER, PK)
- `prod_chip_id` (INTEGER, FK -> ProductChip.id)
- `item_name` (TEXT, NOT NULL)
- `channel` (TEXT, NOT NULL, default "")
- `io_port` (TEXT, NOT NULL, default "")
- `options` (TEXT, NOT NULL, default "")
- `resistor1` (TEXT, NOT NULL, default "0")
- `resistor2` (TEXT, NOT NULL, default "0")
- `disp_name` (TEXT, NOT NULL, default "")
- `offset` (TEXT, NOT NULL, default "0")

約束：
- 目前無 UNIQUE index（依現況）

---

## 10) HWM.Voltage.Channels

> 注意：table 名稱含 `.`，SQL 需加雙引號：`"HWM.Voltage.Channels"`

用途：report_name 與 channel_id 的靜態對照。

欄位：
- `id` (INTEGER, PK)
- `report_name` (TEXT, NOT NULL)
- `channel_name` (TEXT, NOT NULL)
- `channel_id` (TEXT, NOT NULL)

約束：
- `UNIQUE(report_name)`

---

## 11) HWM.Voltage.Defaults

> 注意：table 名稱含 `.`，SQL 需加雙引號：`"HWM.Voltage.Defaults"`

用途：`HWM.Voltage` 相關預設鍵值。

欄位：
- `id` (INTEGER, PK)
- `name` (TEXT, NOT NULL)
- `value` (TEXT, NOT NULL)

約束：
- `UNIQUE(name)`

---

## 12) HWM.Temperature.Channels

> 注意：table 名稱含 `.`，SQL 需加雙引號：`"HWM.Temperature.Channels"`

用途：report_name 與 channel_id 的靜態對照。

欄位：
- `id` (INTEGER, PK)
- `report_name` (TEXT, NOT NULL)
- `channel_name` (TEXT, NOT NULL)
- `channel_id` (TEXT, NOT NULL)

約束：
- `UNIQUE(report_name)`

---

## 13) HWM.Temperature.Defaults

> 注意：table 名稱含 `.`，SQL 需加雙引號：`"HWM.Temperature.Defaults"`

用途：`HWM.Temperature` 相關預設鍵值。

欄位：
- `id` (INTEGER, PK)
- `name` (TEXT, NOT NULL)
- `value` (TEXT, NOT NULL)

約束：
- `UNIQUE(name)`

---

## Query Key（資料面摘要）

- 固定鍵：`product_name + chip_name`
- 流程摘要：
  1) 查 `ProductChip` 取得 `id`（`prod_chip_id`）
  2) 用 `prod_chip_id` 查目標 table

註：
- `query_config_db.py` 對外輸出可見 `option` 欄位，是由 DB 的 `options AS option` 投影而來。
- 哪些 table 可作為 section 查詢與其狀態碼定義，請看 `candidate_query_skill.md`。