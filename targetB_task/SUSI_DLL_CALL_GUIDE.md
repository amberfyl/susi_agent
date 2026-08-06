# SUSI DLL 呼叫速查（僅保留 SusiBoardGetValue 範圍）

你這次關心的是 `SusiBoardGetValue(Id, ref value)` 的 Id。
以下已排除你不在意的範圍：
- 不展開 `SusiDevice` / `Device*.dll`
- 不展開 `susiAI / susiIOT`
- 不展開 `SusiNetwork` 的 ID

## 1) ID 記錄在哪

權威來源（C/C++）：
- `F:\_D_project_here\SUSI_4.0_nw\SUSI\SourceCode\Include\Susi4.h`

C# 對應（SusiDemo4 實際會用）：
- `F:\_D_project_here\SUSI_4.0_nw\SUSI\SourceCode\Applications\SusiDemo4\SusiCommon\Susi4.cs`

## 2) 你要用的 `SusiBoardGetValue` 常見 ID（精簡）

### 2.1 Board 基本資訊數值
- `SUSI_ID_BOARD_BOOT_COUNTER_VAL`
- `SUSI_ID_BOARD_RUNNING_TIME_METER_VAL`
- `SUSI_ID_BOARD_PNPID_VAL`
- `SUSI_ID_BOARD_PLATFORM_REV_VAL`
- `SUSI_ID_BOARD_LAST_SHUTDOWN_STATUS_VAL`
- `SUSI_ID_BOARD_LAST_SHUTDOWN_EVENT_VAL`

### 2.2 Board 版本資訊
- `SUSI_ID_BOARD_DRIVER_VERSION_VAL`
- `SUSI_ID_BOARD_LIB_VERSION_VAL`
- `SUSI_ID_BOARD_FIRMWARE_VERSION_VAL`
- `SUSI_ID_BOARD_DOCUMENT_VERSION_VAL`

### 2.3 HWM（溫度/電壓/風扇/電流）
溫度（0.1 Kelvin）：
- `SUSI_ID_HWM_TEMP_CPU`
- `SUSI_ID_HWM_TEMP_SYSTEM`
- `SUSI_ID_HWM_TEMP_CHIPSET`
- `SUSI_ID_HWM_TEMP_OEM0~OEM5`

電壓（mV）：
- `SUSI_ID_HWM_VOLTAGE_VCORE`
- `SUSI_ID_HWM_VOLTAGE_3V3`
- `SUSI_ID_HWM_VOLTAGE_5V`
- `SUSI_ID_HWM_VOLTAGE_12V`
- `SUSI_ID_HWM_VOLTAGE_VBAT`

風扇（RPM）：
- `SUSI_ID_HWM_FAN_CPU`
- `SUSI_ID_HWM_FAN_SYSTEM`
- `SUSI_ID_HWM_FAN_CPU2`

電流（mA）：
- `SUSI_ID_HWM_CURRENT_OEM0~OEM2`

### 2.4 支援能力位元
- `SUSI_ID_SMBUS_SUPPORTED`
- `SUSI_ID_I2C_SUPPORTED`

> 注意：這兩個回來的是 bit mask，不是單一值。

## 3) 會不會用到？

會。SusiDemo4 內已有實際使用：
- `Plugins/SusiInformation/ctlMain.cs`
- `Plugins/SusiHWM/ctlMain.cs`
- `Plugins/SusiI2C/ctlMain.cs`
- `Plugins/SusiSMBus/ctlMain.cs`
- `Plugins/SusiSmartFan/ctlMain.cs`

## 4) 呼叫範例（C#）

```csharp
UInt32 st = SusiLib.SusiLibInitialize();
if (st != 0) return;

UInt32 val = 0;
st = SusiBoard.SusiBoardGetValue(SusiBoard.SUSI_ID_BOARD_LIB_VERSION_VAL, ref val);
if (st == 0)
{
    // val 有效
}

SusiLib.SusiLibUninitialize();
```

## 5) 單位與解碼

- `SUSI_ID_HWM_TEMP_*` 回傳是「0.1 Kelvin」
  - 攝氏換算：`(value - 2731) / 10.0`
- 電壓通常是 mV
- 風扇通常是 RPM

## 6) 如果你下一步要更實用

我可以再補一段「只針對你板子會成功的 ID 探測程式」：
- 依序測一批 `SUSI_ID_*`
- 成功就列出 value
- `SUSI_STATUS_UNSUPPORTED` 直接略過

這樣你會得到一份「這台機器實際可讀」的 ID 清單。