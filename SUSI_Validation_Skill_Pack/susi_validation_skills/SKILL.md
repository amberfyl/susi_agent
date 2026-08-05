# SUSI Platform Function Validation Agent

## 1. Purpose

This skill defines a reusable validation framework for SUSI SDK platform porting.

The input platform contains:

- A platform-specific SUSI `.ini` file installed under `C:\Windows\SUSI`
- `Susi4.dll`
- SUSI4 driver
- Platform BIOS / EC / firmware
- Optional external test fixtures

The goal is not merely to check whether the INI syntax is valid. The goal is to verify that every function declared by the INI is actually usable through the SUSI API and correctly implemented by the underlying driver, BIOS, EC firmware, and hardware.

A function is considered **PASS** only when the required validation layers for that function pass.

## 2. Validation Scope

The framework currently covers 14 section-level categories:

1. `WDT`
2. `StorageArea`
3. `ThermalProtect`
4. `HWM.Temperature`
5. `HWM.Voltage`
6. `HWM.Fan`
7. `HWM.Current`
8. `HWM.Fan.Control`
9. `HWM.CaseOpen`
10. `VGA.Brightness`
11. `VGA.Backlight`
12. `GPIO`
13. `I2C`
14. `SMBus`

Each section has its own skill file under `skills/` and uses the same L1~L6 validation semantics.

## 3. Recommended Architecture

Use a deterministic test runner for all hardware operations.

```text
Platform INI
    ↓
INI Parser
    ↓
Test Manifest Generator
    ↓
Safety Policy Check
    ↓
SUSI API Test Runner
    ↓
Read-back / Physical Response / Recovery
    ↓
Structured JSON Result
    ↓
AI Diagnosis and Report Generation
```

The AI agent may:

- Parse the INI
- Select the correct validation skill
- Generate test cases and parameters
- Identify missing platform-specific data
- Analyze structured results
- Suggest the most likely failure layer
- Generate QA and defect reports

The AI agent must not:

- Invent safe storage offsets
- Invent writable I2C or SMBus registers
- Change thermal protection limits outside an approved range
- Perform destructive writes without backup and restore
- Treat API success alone as a functional PASS
- Continue writes after restore failure
- Guess that an unsupported API is a firmware defect without comparing it with the INI and platform requirements

## 4. Inputs

Required:

- Platform INI path
- SUSI API wrapper or PowerShell command interface
- Output directory

Recommended:

- Platform profile YAML or JSON
- BIOS version
- EC firmware version
- SUSI driver version
- SUSI DLL version
- Hardware mapping information
- Approved safety policy
- Optional fixture mapping

Example execution:

```powershell
python runner.py `
  --ini "C:\Windows\SUSI\SOM-6833.ini" `
  --profile ".\profiles\SOM-6833.yaml" `
  --categories ALL `
  --output ".\results\SOM-6833"
```

## 5. Common Validation Layers

Every generated case should record which layers were tested.

### L1 — Configuration

Confirm that the INI item:

- Exists when expected
- Is not empty
- Has the expected number of fields
- Contains parseable numeric values
- Uses a known section and item name

### L2 — Capability

Confirm that the SUSI API exposes the same capability declared by the INI.

Examples:

- Enabled INI channel is supported by the API
- Empty INI item is unsupported
- Supported mask matches enabled channels
- Range and feature flags match the INI definition

### L3 — API Command

Confirm that the intended SUSI API call returns the expected status.

### L4 — Read-back

For writable functions:

1. Read the original value
2. Set a test value
3. Read the value again
4. Compare with the expected result
5. Restore the original value
6. Confirm restoration

### L5 — Functional or Physical Response

Confirm that the hardware behavior changes as intended.

Examples:

- GPIO input follows an output loopback
- Fan RPM changes after a control command
- Backlight luminance changes after brightness commands
- WDT causes a real reset
- I2C or SMBus returns a known device ID

### L6 — Recovery

Confirm correct behavior after:

- Restore
- Driver disable / enable
- Reinitialization
- Reboot
- S3 / S4, when required

## 6. Result States

Use these result values:

- `PASS`
- `FAIL_CONFIG`
- `FAIL_CAPABILITY`
- `FAIL_API`
- `FAIL_READBACK`
- `FAIL_FUNCTIONAL`
- `FAIL_RECOVERY`
- `UNSUPPORTED_PASS`
- `N_A`
- `BLOCKED_PARAMETER`
- `BLOCKED_REFERENCE`
- `BLOCKED_FIXTURE`
- `SAFETY_SKIPPED`
- `ABORTED_RESTORE_FAILURE`

Do not collapse unsupported, blocked, and failed cases into a generic error.

## 7. Common Case Result Schema

```json
{
  "case_id": "GPIO.GPIO00.OUTPUT_HIGH",
  "category": "GPIO",
  "target": "GPIO00",
  "platform": {
    "name": "SOM-6833",
    "bios": "68330000060X007",
    "ec_fw": "V01004404",
    "driver": "4.x",
    "library": "4.x"
  },
  "parameters": {},
  "expected": {},
  "actual": {},
  "api_calls": [],
  "validation_layers": {
    "L1_configuration": "PASS",
    "L2_capability": "PASS",
    "L3_api": "PASS",
    "L4_readback": "PASS",
    "L5_functional": "BLOCKED_FIXTURE",
    "L6_recovery": "PASS"
  },
  "result": "BLOCKED_FIXTURE",
  "restore_result": "PASS",
  "suspected_layers": [],
  "evidence": [],
  "started_at": "",
  "duration_ms": 0
}
```

## 8. Common Execution Rules

1. Initialize SUSI once per test session unless the API requires a different lifecycle.
2. Record all input parameters and raw API status codes.
3. Always read and preserve an original value before modifying it.
4. Use `try/finally` or an equivalent mechanism to restore modified values.
5. Abort the category if restoration fails.
6. Use fixed, reproducible test sequences.
7. Use configurable delays rather than hard-coded assumptions.
8. Run unsupported negative cases for empty INI entries.
9. Save raw results before AI interpretation.
10. Never let AI-generated prose overwrite raw evidence.

## 9. Category Routing

Use the following section skill files:

- `skills/1_wdt.md`
- `skills/2_storage.md`
- `skills/3_thermal_protect.md`
- `skills/4_hwm_temperature.md`
- `skills/5_hwm_voltage.md`
- `skills/6_hwm_fan.md`
- `skills/7_hwm_current.md`
- `skills/8_fan_control.md`
- `skills/9_hwm_caseopen.md`
- `skills/10_vga_brightness.md`
- `skills/11_vga_backlight.md`
- `skills/12_gpio.md`
- `skills/13_i2c.md`
- `skills/14_smbus.md`

Use `schemas/platform-profile.md` for platform parameters and `schemas/api-wrapper-contract.md` for the PowerShell/Python interface.

## 10. Overall Platform Verdict

A platform is:

- **PASS** when all required cases are `PASS` or `UNSUPPORTED_PASS`
- **CONDITIONAL PASS** when no required case fails, but some cases are blocked
- **FAIL** when any required case is a failure
- **ABORTED** when restoration, system recovery, or safety control fails

The final report must clearly separate:

- INI configuration mismatch
- API exposure mismatch
- API execution failure
- Read-back failure
- Functional or physical failure
- Recovery failure
- Missing platform parameter
- Missing fixture
