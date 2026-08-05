# SUSI API Wrapper Contract

## Purpose

The platform-independent validators must not call DLL functions directly. They should call a stable wrapper interface.

The wrapper may be implemented in PowerShell, C#, Python `ctypes`, or another language, but it must accept structured parameters and return structured JSON.

Current pack status:
- Wrapper contract is action-based and stable.
- Section orchestration is done outside the wrapper; this contract remains per-action/per-call.

## Command Pattern

```powershell
.\susi_api.ps1 `
  -Action "HwmGet" `
  -ParametersJson '{"id":"0x00021004"}'
```

## Required Response Fields

```json
{
  "action": "HwmGet",
  "status": 0,
  "status_hex": "0x00000000",
  "status_name": "SUSI_STATUS_SUCCESS",
  "value": 4990,
  "unit": "mV",
  "duration_ms": 4
}
```

## Wrapper Requirements

- Never print unstructured diagnostic text to standard output.
- Send logs to standard error or a log file.
- Return the raw SUSI status code.
- Preserve unsigned values correctly.
- Encode byte buffers as uppercase hexadecimal.
- Include the exact arguments passed to the DLL in debug logs.
- Validate pointer and buffer lengths.
- Initialize and uninitialize SUSI safely.
- Return a nonzero process exit code only when the wrapper itself fails. SUSI API failures should still be returned as JSON.

## Suggested Atomic Actions

### General

- `Initialize`
- `Uninitialize`
- `GetValue`
- `GetString`

### HWM

- `HwmGet`

### Storage

- `StorageQuery`
- `StorageRead`
- `StorageWrite`

### WDT

- `WdtQuery`
- `WdtStart`
- `WdtTrigger`
- `WdtStop`

### Thermal Protect

- `ThermalQuery`
- `ThermalGet`
- `ThermalSet`

### GPIO

- `GpioQuery`
- `GpioGetDirection`
- `GpioSetDirection`
- `GpioGetLevel`
- `GpioSetLevel`

### VGA

- `BacklightGet`
- `BacklightSet`
- `BrightnessQuery`
- `BrightnessGet`
- `BrightnessSet`

### I2C

- `I2cQuery`
- `I2cRead`
- `I2cWrite`
- `I2cWriteRead`

### SMBus

- `SmbusQuery`
- `SmbusReadByte`
- `SmbusWriteByte`
- `SmbusReadWord`
- `SmbusWriteWord`
- `SmbusBlockRead`
- `SmbusBlockWrite`

### Fan Control

- `FanControlQuery`
- `FanControlGet`
- `FanControlSet`

## Python Adapter Example

```python
def call_susi(action: str, parameters: dict) -> dict:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy", "Bypass",
            "-File", "susi_api.ps1",
            "-Action", action,
            "-ParametersJson", json.dumps(parameters),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    if not completed.stdout.strip():
        raise RuntimeError(
            f"SUSI wrapper produced no JSON. stderr={completed.stderr}"
        )

    result = json.loads(completed.stdout)

    if completed.returncode != 0:
        raise RuntimeError(
            f"Wrapper failure. result={result}, stderr={completed.stderr}"
        )

    return result
```
