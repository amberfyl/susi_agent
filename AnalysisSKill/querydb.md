# QueryDB schema 筆記（config_new.db）

路徑：`/home/company2/AIagent_susi/config_new.db`

用途：記錄目前正式使用的 query database schema。舊的 `config.db` 已淘汰，不再是查詢來源。

邊界：
- 本文件只描述資料表、欄位與資料關係（schema/data contract）。
- Query 流程、status 契約、probe/spec gate、BIOS/電路圖融合與最終判定，依 `candidate_query_skill.md`、`orchestrator_skill.md` 及其他對應 skill 執行。
- 本 DB 不保存 BIOS、電路圖或 probe 的最終判定。

## 1. ProductChip

用途：以固定查詢鍵 `(product_name, chip_name)` 命中一筆平台／晶片組合；再使用該列的 `hardware_id` 查各 section table。

欄位：
- `id` (INTEGER, PK；資料列識別用，不作 section join key)
- `product_name` (TEXT, NOT NULL)
- `chip_name` (TEXT, NOT NULL)
- `hardware_id` (TEXT, NOT NULL)
- `config_chip` (TEXT, NOT NULL)
- `SMBus` (TEXT, NOT NULL, default `0`, CHECK `0/1`)
- `I2C` (TEXT, NOT NULL, default `0`, CHECK `0/1`)
- `VGA.Backlight` (TEXT, NOT NULL, default `0`, CHECK `0/1`)
- `VGA.Brightness` (TEXT, NOT NULL, default `0`, CHECK `0/1`)
- `HWM.Voltage` (TEXT, NOT NULL, default `0`, CHECK `0/1`)
- `HWM.Current` (TEXT, NOT NULL, default `0`, CHECK `0/1`)
- `HWM.Temperature` (TEXT, NOT NULL, default `0`, CHECK `0/1`)
- `HWM.Fan` (TEXT, NOT NULL, default `0`, CHECK `0/1`)
- `HWM.CaseOpen` (TEXT, NOT NULL, default `0`, CHECK `0/1`)
- `WDT` (TEXT, NOT NULL, default `0`, CHECK `0/1`)
- `GPIO` (TEXT, NOT NULL, default `0`, CHECK `0/1`)
- `StorageArea` (TEXT, NOT NULL, default `0`, CHECK `0/1`)
- `ThermalProtect` (TEXT, NOT NULL, default `0`, CHECK `0/1`)

約束：
- `UNIQUE(product_name, chip_name)`
- section flag 是歷史 porting evidence／候選提示，不取代 section table 的實際 row，也不是硬體能力的絕對判定。

## 2. Section table 的共同欄位與關係

下列 section table 以 `hardware_id` 對應 `ProductChip.hardware_id`：

- `SMBus`
- `I2C`
- `WDT`
- `GPIO`
- `StorageArea`
- `ThermalProtect`
- `VGA.Backlight`
- `VGA.Brightness`
- `HWM.Current`
- `HWM.CaseOpen`
- `HWM.Fan`
- `HWM.Temperature`
- `HWM.Voltage`

共同欄位（依各表實際 schema）：
- `id` (INTEGER, PK)
- `hardware_id` (TEXT, NOT NULL)
- `report_name` (TEXT, NOT NULL, default `""`)
- `channel_name` (TEXT, NOT NULL, default `""`)
- `channel_id` (TEXT, NOT NULL, default `""`)
- `io_port` (TEXT, NOT NULL, default `""`)
- `options` (TEXT, NOT NULL, default `""`)

欄位語意：
- `hardware_id`：section row 的硬體模板識別值。
- `report_name`：SUSI probe 回報的 channel／item 名稱，是 report mapping 的主要欄位。
- `channel_name`：DB 整理時使用的輔助名稱，不作 query key。
- `channel_id`：INI `Channel` 欄位對應值。
- `io_port`：INI `IOPort/Device Address` 欄位對應值。
- `options`：INI `Option` 欄位對應值。

注意：section table 沒有 `prod_chip_id` 欄位，也不是以 `ProductChip.id` join。
同一個 `hardware_id` 可被多個 `(product_name, chip_name)` 使用；因此外部 query 必須先用完整的 `(product_name, chip_name)` 命中 ProductChip，再取其 hardware_id。

目前 section table 沒有額外 UNIQUE constraint；重複 row 由資料整理與後續流程處理。

## 3. 各 section 的額外欄位

### 3.1 WDT、SMBus、I2C、HWM.Current、HWM.CaseOpen、StorageArea、ThermalProtect、VGA.Backlight

除共同欄位外沒有額外欄位。

### 3.2 GPIO

除共同欄位外：
- `base_addr` (TEXT, NOT NULL, default `""`)
- `group` (TEXT, NOT NULL, default `""`)
- `pin` (TEXT, NOT NULL, default `""`)

GPIO 的 `group`／`pin` 是否採用，仍依 EC 或 SIO/NCT 電路圖路徑判定；DB row 本身不是最終電路圖裁決。

### 3.3 VGA.Brightness

除共同欄位外：
- `range_max` (TEXT, NOT NULL, default `""`)
- `range_min` (TEXT, NOT NULL, default `""`)
- `frequency` (TEXT, NOT NULL, default `""`)

### 3.4 HWM.Fan

除共同欄位外：
- `pulses` (TEXT, NOT NULL, default `"0"`)

`HWM.Fan.Control` 不在 DB 中建立獨立 table；通常以 `HWM.Fan` row 為基礎，由後續 fan pairing／config builder 流程產生。只有確認存在差異時才由後續流程修正。

### 3.5 HWM.Temperature

除共同欄位外：
- `offset` (TEXT, NOT NULL, default `"0"`)

### 3.6 HWM.Voltage

除共同欄位外：
- `resistor1` (TEXT, NOT NULL, default `"0"`)
- `resistor2` (TEXT, NOT NULL, default `"0"`)
- `offset` (TEXT, NOT NULL, default `"0"`)

## 4. 實際資料表清單

`config_new.db` 目前包含以下 tables：

```text
ProductChip
SMBus
I2C
WDT
GPIO
StorageArea
ThermalProtect
VGA.Backlight
VGA.Brightness
HWM.Current
HWM.CaseOpen
HWM.Fan
HWM.Temperature
HWM.Voltage
```

以下舊 schema table 不屬於 `config_new.db`：

```text
HWM.Fan.Channels
HWM.Fan.Defaults
HWM.Temperature.Channels
HWM.Temperature.Defaults
HWM.Voltage.Channels
HWM.Voltage.Defaults
GPIO.GroupPins
GPIO.Defaults
```

這些舊的拆分 mapping/defaults table 不可再被 query 程式引用；目前的 channel 與 template 欄位已整合進各 section row。

## 5. Query 的資料關係摘要

```text
(product_name, chip_name)
        |
        v
ProductChip.hardware_id
        |
        +--> SMBus.hardware_id
        +--> I2C.hardware_id
        +--> WDT.hardware_id
        +--> GPIO.hardware_id
        +--> StorageArea.hardware_id
        +--> ThermalProtect.hardware_id
        +--> VGA.Backlight.hardware_id
        +--> VGA.Brightness.hardware_id
        +--> HWM.Current.hardware_id
        +--> HWM.CaseOpen.hardware_id
        +--> HWM.Fan.hardware_id
        +--> HWM.Temperature.hardware_id
        +--> HWM.Voltage.hardware_id
```

`query_config_db.py` 對外固定提供：
- `query_key`
- `status`
- `prod_chip`
- `rows`
- `row_count`

`rows` 的 DB 原生欄位以新 schema 為準；目前另外提供下列唯讀輸出 alias，供尚未完成 migration 的 config-builder 使用：
- `item_name` ← `report_name`
- `channel` ← `channel_id`
- `option` ← `options`
- `disp_name` ← `channel_name`

這些 alias 不是 `config_new.db` 的欄位，不可反向寫回 DB。

查詢程式的預設 DB 路徑是：`/home/company2/AIagent_susi/config_new.db`。
