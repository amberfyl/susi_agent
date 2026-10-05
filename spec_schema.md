# SUSI spec.json Schema

Stage 1 LLM 輸出 / Stage 2 generate_ini.py 輸入。
命名慣例：`{project}-spec.json`

---

## 頂層結構

```json
{
  "version":  "1.0",
  "project":  "SOM-6884",
  "platform": "SOM-6884 A3",

  "chips": { ... },
  "voltages": [ ... ],
  "temperatures": [ ... ],
  "fans": [ ... ],
  "currents": [ ... ],
  "caseopen": [ ... ],
  "smartfan": [ "FSYS", "FOEM0" ],
  "gpio": { ... },
  "features": { ... },
  "screen_control": { ... }
}
```

---

## chips

```json
"chips": {
  "hwm":  "Advantech EIO_IS200",
  "gpio": "Advantech EIO_IS200",
  "wdt": "",
  "storage": "",
  "thermalprotect": ""
}
```

- 值必須是 ChipDB 能查到的名稱（canonical DB name 或 alias）
- 若 HWM 和 GPIO 用同一顆晶片，兩個填一樣
- LLM 負責從原始字串（如 "Rdc Semi A9620LIF2A(EIO-211)"）解析出正確名稱
- WDT / Storage / ThermalProtect 預設為 EC-only：
  - 預設使用 `chips.gpio`（EC）解析
  - 只有在 `chips.wdt` / `chips.storage` / `chips.thermalprotect` 有明確填值時，才會額外用該名稱查詢
- **名字樣式不可用來判斷 EC/SuperIO**（同是 Nuvoton：NCT6694B 是 EC 的搭配角色、
  NCT6126D 是 SuperIO）。EC 判定由下游依 chip DB category 決定（有 Storage ⇒ EC），
  `NCT6694B` 單獨出現時 chip_db 的 EC companion 規則會自動把 EC-side 功能解析到
  `Advantech EIO_IS200`——spec 只需照表單原文填 chip 名（見 rule.md 第 5、6 節）

---

## voltages

```json
"voltages": [
  {"ini_key": "V120", "label": "+12V"},
  {"ini_key": "V50",  "label": "+5V"},
  {"ini_key": "VBAT", "label": "VBAT"}
]
```

- `ini_key`：Stage 2 用來查 DB channel 和填 INI key 的識別符
- `label`：顯示名稱（form 上的文字，空字串代表不填）

**合法 ini_key：**
```
VCORE  VCORE2  V25   V33   V50
V120   V5SB    V3SB  VBAT
VN50   VN120   VTT   V240  DC   DCSTBY  VBATLI
V15    V18     V105
VOEM0  VOEM1   VOEM2  VOEM3
V5S5   V3S5
```

---

## temperatures

```json
"temperatures": [
  {"ini_key": "TCPU", "label": "CPU temperature"},
  {"ini_key": "TSYS", "label": "System temperature"}
]
```

**合法 ini_key：**
```
TCPU  TCPU2  TSYS  TCHIPSET
TOEM0  TOEM1  TOEM2  TOEM3  TOEM4  TOEM5  TOEM6
GRAPHIC
```

---

## fans

```json
"fans": [
  {"ini_key": "FCPU",  "label": "COM Module FAN"},
  {"ini_key": "FSYS",  "label": "Carrier Board FAN"}
]
```

- [HWM.Fan] 是 Fan Speed 顯示用途（監看轉速），不是控制開關。

**合法 ini_key：**
```
FCPU  FSYS  FCPU2
FOEM0  FOEM1  FOEM2  FOEM3  FOEM4  FOEM5  FOEM6
```

---

## currents

```json
"currents": [
  {"ini_key": "OEM0", "label": "Current sense"}
]
```

- 對應 `[HWM.Current]` section（MIO-5354 等板有此項）
- 無 current 監測時省略或給空陣列

**合法 ini_key：**
```
OEM0  OEM1  OEM2
```

---

## caseopen

```json
"caseopen": [
  {"ini_key": "CO0", "label": "Chassis Intrusion"}
]
```

- 對應 `[HWM.CaseOpen]` section
- 無 case-open 偵測時省略或給空陣列

**合法 ini_key：**
```
CO0  CO1  CO2
```

---

## smartfan

Fan.Control（SmartFan）要啟用的風扇，用 ini_key 引用 fans 列表。

```json
"smartfan": ["FSYS", "FOEM0"]
```

- [HWM.Fan.Control] 用來開啟/關閉可控風扇；有啟用的 fan 才可做 PWM/Level 的百分比控速（實際可用百分比依平台而定）。
- 空列表 = 無 SmartFan（僅顯示 fan speed，不提供風扇控制）。

---

## gpio

```json
"gpio": {
  "count": 8,
  "pins": [
    {"index": 0, "direction": "input", "name": ""},
    {"index": 1, "direction": "output", "name": ""}
  ]
}
```

- `count`：GPIO 腳位總數（generate_ini 依此產生 GPIO00~GPIO(N-1)）
- `pins`：選填，每腳方向與使用者名稱。省略 = 全部用 "both"（generate_ini 預設行為）
- `direction` 合法值：`"input"` / `"output"` / `"both"`
- `name`：使用者在原始 request form 填寫的 GPIO 名稱；未填寫或表單沒有此欄位時為空字串。

---

## features

Boolean flags，控制哪些 section 要填值。

```json
"features": {
  "smbus":         true,
  "i2c":           true,
  "backlight":     true,
  "brightness":    true,
  "wdt":           true,
  "wdt_pinevent":  true,
  "storage":       true,
  "thermalprotect": true
}
```

| Key           | 對應 INI section          | 說明 |
|---------------|--------------------------|------|
| smbus         | [SMBus] Channel2+        | EC SMBus channel |
| i2c           | [I2C] Channel1+          | DB template；空 channel 時由 full probe 的 I2C_OEMn 產生 |
| backlight     | [VGA.Backlight]          | 背光開關 |
| brightness    | [VGA.Brightness]         | 亮度調節 |
| wdt           | [WDT]                    | Watchdog timer |
| wdt_pinevent  | WDT option bit 0x10      | Pin event 模式 |
| storage       | [StorageArea]            | EC storage area |
| thermalprotect| [ThermalProtect]         | 熱保護 |

---

## screen_control（可選，建議保留）

保留 Screen control 區塊的結構化資訊，避免只剩 `features.brightness/backlight` 布林值導致上下文遺失。

```json
"screen_control": {
  "brightness": {
    "enabled": true,
    "items": [
      {"socket": "LVDS1", "chip": "PTN3460", "remark": ""}
    ]
  },
  "backlight": {
    "enabled": false,
    "items": [
      {"socket": "<Ex: CN01>", "chip": "<Ex: EC >", "remark": ""}
    ]
  }
}
```

- `enabled`: 該子功能是否勾選（■/□）。
- `items`: 對應表格列，逐列保留 `socket/chip/remark` 原文。
- 若表格只有範例值（如 `<Ex: ...>`），照原文保留，不要自行推導為實際值。
- `generate_ini.py` **會使用**此欄位：搭配 mapping 表做 board-level routing override
  （brightness channel 選擇）。欄位省略時向後相容——回退 chip DB 預設 channel。

---

## 完整範例（SOM-6884）

```json
{
  "version": "1.0",
  "project": "SOM-6884",
  "platform": "SOM-6884 A3",
  "chips": {
    "hwm":  "Advantech EIO_IS200",
    "gpio": "Advantech EIO_IS200"
  },
  "voltages": [
    {"ini_key": "V120", "label": "+12V"},
    {"ini_key": "V50",  "label": "+5V"},
    {"ini_key": "VBAT", "label": "VBAT"}
  ],
  "temperatures": [
    {"ini_key": "TCPU", "label": "CPU temperature"}
  ],
  "fans": [
    {"ini_key": "FCPU",  "label": "COM Module FAN"},
    {"ini_key": "FSYS",  "label": "Carrier Board FAN"}
  ],
  "smartfan": ["FCPU", "FSYS"],
  "gpio": {
    "count": 8,
    "pins": []
  },
  "features": {
    "smbus":          true,
    "i2c":            true,
    "backlight":      true,
    "brightness":     true,
    "wdt":            true,
    "wdt_pinevent":   true,
    "storage":        true,
    "thermalprotect": true
  }
}
```

---

## 完整範例（ITA-580，GPIO I2C expander）

```json
{
  "version": "1.0",
  "project": "ITA-580",
  "platform": "ITA-580",
  "chips": {
    "hwm":  "Advantech EIO_IS200",
    "gpio": "PCA9554"
  },
  "voltages": [
    {"ini_key": "VBAT", "label": "VBAT RTC battery"},
    {"ini_key": "V5SB", "label": "+5V Standby"},
    {"ini_key": "V120", "label": "+12V DC Input"}
  ],
  "temperatures": [
    {"ini_key": "TCPU", "label": "CPU temperature"}
  ],
  "fans": [],
  "smartfan": [],
  "gpio": {
    "count": 8,
    "pins": [
      {"index": 0, "direction": "output"},
      {"index": 1, "direction": "output"},
      {"index": 2, "direction": "output"},
      {"index": 3, "direction": "output"},
      {"index": 4, "direction": "output"},
      {"index": 5, "direction": "output"},
      {"index": 6, "direction": "output"},
      {"index": 7, "direction": "output"}
    ]
  },
  "features": {
    "smbus":          true,
    "i2c":            false,
    "backlight":      false,
    "brightness":     false,
    "wdt":            true,
    "wdt_pinevent":   false,
    "storage":        true,
    "thermalprotect": true
  }
}
```

---

## 完整範例（MIO-5152，SuperIO NCT6126D）

```json
{
  "version": "1.0",
  "project": "MIO-5152",
  "platform": "MIO-5152",
  "chips": {
    "hwm":  "NCT6126D",
    "gpio": "NCT6126D"
  },
  "voltages": [
    {"ini_key": "VCORE", "label": "VCORE"},
    {"ini_key": "V33",   "label": "3VVCC"},
    {"ini_key": "V50",   "label": "+5V"},
    {"ini_key": "V120",  "label": "+12V"},
    {"ini_key": "V5SB",  "label": "+5VSB"},
    {"ini_key": "V3SB",  "label": "3VSB"},
    {"ini_key": "VBAT",  "label": "VBAT"},
    {"ini_key": "VTT",   "label": "AVCC"}
  ],
  "temperatures": [
    {"ini_key": "TCPU", "label": "CPU temperature"},
    {"ini_key": "TSYS", "label": "System temperature"}
  ],
  "fans": [
    {"ini_key": "FSYS", "label": "System Fan"}
  ],
  "smartfan": ["FSYS"],
  "gpio": {
    "count": 8,
    "pins": [
      {"index": 0, "direction": "output"},
      {"index": 1, "direction": "output"},
      {"index": 2, "direction": "output"},
      {"index": 3, "direction": "output"},
      {"index": 4, "direction": "output"},
      {"index": 5, "direction": "output"},
      {"index": 6, "direction": "output"},
      {"index": 7, "direction": "output"}
    ]
  },
  "features": {
    "smbus":          false,
    "i2c":            false,
    "backlight":      false,
    "brightness":     true,
    "wdt":            true,
    "wdt_pinevent":   false,
    "storage":        true,
    "thermalprotect": true
  }
}
```
