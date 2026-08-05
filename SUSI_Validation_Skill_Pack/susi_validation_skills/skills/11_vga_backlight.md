# Skill: Validate VGA.Backlight — Automated Flow

## Scope

Validate `[VGA.Backlight]` using:
- PowerShell runner
- C# wrapper (P/Invoke to `Susi4.dll`)
- Backlight on/off set/get sequence
- Optional physical light-state correlation
- Restore/recovery checks

This category has high API-level automation potential. Full functional proof needs physical observation source.

## Objective

Verify all required backlight behaviors:
1. Declared backlight channel is exposed by API
2. On/Off set/get is coherent and repeatable
3. Original state can be restored safely
4. Optional physical light-state follows command
5. Re-init/reload keeps control path coherent

---

## Required Inputs

### A) Platform/Test metadata
- `platform_name`
- `bios_version` (optional)
- `driver_version` / `dll_version` (optional)

### B) Backlight policy
- `backlight_channel`
- `state_on_value`, `state_off_value` (platform encoding)
- `toggle_sequence` (e.g. OFF->ON->OFF->ON)
- `settle_time_ms`

### C) Functional correlation policy (optional)
- `physical_check_enabled` (true/false)
- `observation_source` (`lux_meter` | `camera_roi` | `panel_signal_probe`)
- `min_observable_delta`

### D) Safety policy
- `abort_on_restore_failure` (true)

---

## Missing Conditions (do not hard-fail)

- No physical observation source -> API-level pass allowed; L5 may be `N_A`/`BLOCKED_FIXTURE`
- Unknown platform on/off encoding -> `BLOCKED_REFERENCE`

---

## Step 0 — Probe Gate (Mandatory)

1. Initialize SUSI library
2. Probe backlight capability
3. Validate state encoding and sequence

Decision rules:
- Required backlight channel unsupported -> `FAIL_CAPABILITY`
- Invalid sequence/state encoding -> `BLOCKED_PARAMETER`

---

## Automated Test Flow

## Case A — Capability & Baseline State

Goal: verify channel availability and baseline readability.

Steps:
1. Query backlight support
2. Read current state as baseline

Pass criteria:
- support query success
- baseline read success

Fail mapping:
- unsupported required channel -> `FAIL_CAPABILITY`
- query/read error -> `FAIL_API`

## Case B — On/Off Set/Get Sequence (Core)

Goal: prove state transition and read-back coherence.

Steps:
1. Save original state
2. Run toggle sequence:
   - set target state
   - wait settle time
   - read back state
   - compare with expected
3. In finally:
   - restore original state
   - read-back verify

Pass criteria:
- all toggles read back correctly
- restore verified

Fail mapping:
- set/get API error -> `FAIL_API`
- read-back mismatch -> `FAIL_READBACK`
- restore failure -> `ABORTED_RESTORE_FAILURE`

## Case C — Physical Light Correlation (Optional)

Goal: verify panel light behavior follows backlight state.

Steps:
1. Execute toggle sequence while collecting physical signal
2. Compare OFF/ON transitions against observed signal changes
3. Validate delta exceeds threshold

Pass criteria:
- physical signal changes match command direction

Fail mapping:
- no observation source -> `BLOCKED_FIXTURE`
- direction mismatch -> `FAIL_FUNCTIONAL`

## Case D — Recovery/Re-init

Goal: verify path remains valid after re-init/reload.

Steps:
1. SUSI re-init or driver reload (policy)
2. Run short OFF->ON test
3. Restore original state

Pass criteria:
- set/get still coherent
- restore passes

Fail mapping:
- path broken after recovery -> `FAIL_RECOVERY`

## Negative Cases

- invalid channel ID
- invalid state value

Expected:
- API rejects invalid operations

---

## L1~L6 Mapping (VGA.Backlight)

- L1 Configuration: state encoding/sequence/settle policy complete
- L2 Capability: channel exposed
- L3 API Command: set/get statuses
- L4 Read-back: transition and restore consistency
- L5 Functional: physical light-state correlation (if required)
- L6 Recovery: re-init/reload continuity

---

## Output Schema (per case)

```json
{
  "case_id": "VGA.Backlight.TOGGLE_SETGET",
  "category": "VGA.Backlight",
  "target": "Panel0",
  "parameters": {
    "toggle_sequence": [0,1,0,1],
    "settle_time_ms": 800
  },
  "actual": {
    "readback_sequence": [0,1,0,1],
    "physical_series": [5,88,6,90]
  },
  "validation_layers": {
    "L1_configuration": "PASS",
    "L2_capability": "PASS",
    "L3_api": "PASS",
    "L4_readback": "PASS",
    "L5_functional": "PASS",
    "L6_recovery": "PASS"
  },
  "result": "PASS",
  "restore_result": "PASS",
  "evidence": [
    "vga_backlight_toggle_log.json",
    "vga_backlight_physical.csv",
    "vga_backlight_recovery_log.json"
  ],
  "suspected_layers": []
}
```

---

## Platform Verdict Contribution

VGA.Backlight verdict:
- `PASS`: required cases pass
- `CONDITIONAL`: API/readback pass, physical source unavailable
- `FAIL`: any required case fails
- `ABORTED`: restore/recovery framework aborted

Always keep `BLOCKED_*` explicit for follow-up.