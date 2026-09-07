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

## 14) HWM.Fan.Channels

> 注意：table 名稱含 `.`，SQL 需加雙引號：`"HWM.Fan.Channels"`

用途：依 `hardware_id` 與 `report_name` 對應 Fan channel。

欄位：
- `id` (INTEGER, PK)
- `hardware_id` (TEXT, NOT NULL)
- `report_name` (TEXT, NOT NULL)
- `channel_name` (TEXT, NOT NULL)
- `channel_id` (TEXT, NOT NULL)

約束：
- `UNIQUE(hardware_id, report_name)`

---

## 15) HWM.Fan.Defaults

> 注意：table 名稱含 `.`，SQL 需加雙引號：`"HWM.Fan.Defaults"`

用途：`HWM.Fan` 相關預設鍵值。

適用於 EC Fan template：`EIO-201`、`EIO-211`、`IT-8528`、`IT-5782`、`IT-5121`。
`disp_name` 預設為空，未有圖片確認名稱時保持空白。

欄位：
- `id` (INTEGER, PK)
- `name` (TEXT, NOT NULL)
- `value` (TEXT, NOT NULL)

約束：
- `UNIQUE(name)`

---

## 16) GPIO.GroupPins

> 注意：table 名稱含 `.`，SQL 需加雙引號：`"GPIO.GroupPins"`

用途：GPIO report name 與 group/bit 的靜態對照。

欄位：
- `id` (INTEGER, PK)
- `report_name` (TEXT, NOT NULL)
- `group` (TEXT, NOT NULL)
- `pin` (TEXT, NOT NULL)

約束：
- `UNIQUE(report_name)`

---

## 17) GPIO.Defaults

> 注意：table 名稱含 `.`，SQL 需加雙引號：`"GPIO.Defaults"`

用途：GPIO template 相關預設鍵值。

欄位：
- `id` (INTEGER, PK)
- `name` (TEXT, NOT NULL)
- `value` (TEXT, NOT NULL)

約束：
- `UNIQUE(name)`

---

## 18) I2C

用途：提供 `[I2C]` 的硬體模板欄位。`I2C` table 沒有 `item_name`；`id` 只代表 DB row primary key，不代表 INI `Channel` 或 full probe 的 I2C id。

欄位：
- `id` (INTEGER, PK)
- `prod_chip_id` (INTEGER, FK -> ProductChip.id)
- `channel` (TEXT, NOT NULL, default "")
- `io_port` (TEXT, NOT NULL, default "")
- `options` (TEXT, NOT NULL, default "")
- `disp_name` (TEXT, NOT NULL, default "")

約束：
- 目前無 UNIQUE index（依現況）

目前資料（join `ProductChip`）：

| I2C.id | prod_chip_id | product_name | chip_name | channel | io_port | options |
|---:|---:|---|---|---|---|---|
| 1 | 2 | MIO | EIO-201 | 空 | `0` | `0x20000000` |
| 2 | 3 | MIO | IT-8528 | 空 | `0` | `0x20000000` |
| 3 | 9 | ARK | IT-8528 | 空 | `0` | `0x20000000` |
| 4 | 21 | MIO | NCT6694B | `0x00000000` | `0x2E` | `0x20000000` |

生成規則：
- request/spec 的 `features.i2c` 不是 `true`：不查詢、不產生 I2C。
- 沒有命中以上 DB row：`SECTION_EMPTY`，不 fallback 到其他 chip。
- `io_port`、`options` 從 DB row 取。
- DB `channel` 有值：直接使用 DB channel。
- DB `channel` 為空：由 full probe 的有效 `I2C_OEMn` 取得邏輯 `n`，使用 `0x80000000 + n`；`I2C_OEM0` 對應 INI `Channel2`，依序至 `Channel5`。

---

## Query Key（資料面摘要）

- 固定鍵：`product_name + chip_name`
- 流程摘要：
  1) 查 `ProductChip` 取得 `id`（`prod_chip_id`）
  2) 用 `prod_chip_id` 查目標 table

註：
- `query_config_db.py` 對外輸出可見 `option` 欄位，是由 DB 的 `options AS option` 投影而來。
- 哪些 table 可作為 section 查詢與其狀態碼定義，請看 `candidate_query_skill.md`。