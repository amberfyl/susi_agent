# Skill: Validate VGA.Brightness — Automated Flow

## Scope

Validate `[VGA.Brightness]` using:
- PowerShell runner
- C# wrapper (P/Invoke to `Susi4.dll`)
- Brightness set/get sequence
- Optional physical luminance/PWM correlation
- Restore/recovery checks

This category can achieve strong API-level automation. Full functional proof needs physical observation source.

## Objective

Verify all required brightness behaviors:
1. Declared brightness channel/range is exposed correctly
2. Set/Get is coherent across test sequence
3. Values stay within legal range and step policy
4. Optional physical brightness response matches command direction
5. Original brightness is restored safely
6. Re-init/reload keeps control path coherent

---

## Required Inputs

### A) Platform/Test metadata
- `platform_name`
- `bios_version` (optional)
- `driver_version` / `dll_version` (optional)

### B) Brightness policy
- `brightness_channel`
- `range_min`, `range_max`
- `step_policy` (if applicable)
- `test_sequence` (e.g. 0/25/50/75/100)
- `settle_time_ms`

### C) Functional correlation policy (optional)
- `physical_check_enabled` (true/false)
- `observation_source` (`lux_meter` | `camera_roi` | `pwm_capture`)
- `expected_monotonic` (true/false)
- `min_observable_delta` (source-specific)

### D) Safety policy
- `abort_on_restore_failure` (true)

---

## Missing Conditions (do not hard-fail)

- No physical observation source -> API-level pass allowed; L5 may be `N_A`/`BLOCKED_FIXTURE`
- Unclear range mapping from platform policy -> `BLOCKED_REFERENCE`

---

## Step 0 — Probe Gate (Mandatory)

1. Initialize SUSI library
2. Probe brightness capability/range
3. Validate test sequence within legal range

Decision rules:
- Required channel unsupported -> `FAIL_CAPABILITY`
- Illegal sequence value -> `BLOCKED_PARAMETER`

---

## Automated Test Flow

## Case A — Capability & Range

Goal: verify brightness control is exposed and bounded.

Steps:
1. Query brightness support and range
2. Read current brightness as baseline
3. Validate sequence values and steps

Pass criteria:
- channel/range query success
- baseline readable

Fail mapping:
- query/read error -> `FAIL_API`
- unsupported channel -> `FAIL_CAPABILITY`

## Case B — Set/Get Sequence (Core)

Goal: prove command/read-back coherence.

Steps:
1. Save original brightness
2. For each value in sequence:
   - set brightness
   - wait settle time
   - read back brightness
   - compare with expected
3. In finally:
   - restore original brightness
   - read-back verify

Pass criteria:
- set/get consistent for sequence
- restore verified

Fail mapping:
- set/get API error -> `FAIL_API`
- read-back mismatch -> `FAIL_READBACK`
- restore failure -> `ABORTED_RESTORE_FAILURE`

## Case C — Physical Correlation (Optional)

Goal: verify commanded brightness causes physical response.

Steps:
1. Run same sequence while collecting physical signal
2. Compare command order vs observed trend
3. Check monotonicity and minimum delta

Pass criteria:
- observed trend follows command direction
- deltas meet threshold

Fail mapping:
- no source available -> `BLOCKED_FIXTURE`
- trend mismatch -> `FAIL_FUNCTIONAL`

## Case D — Recovery/Re-init

Goal: verify control path remains valid after recovery operations.

Steps:
1. SUSI re-init or driver reload (per policy)
2. Re-run short set/get pair
3. Verify restore path still valid

Pass criteria:
- set/get still coherent
- restore passes

Fail mapping:
- broken after recovery -> `FAIL_RECOVERY`

## Negative Cases

- set below min / above max
- invalid channel ID

Expected:
- API rejects invalid requests

---

## L1~L6 Mapping (VGA.Brightness)

- L1 Configuration: range/sequence/settle policy complete
- L2 Capability: channel and range exposed
- L3 API Command: set/get statuses
- L4 Read-back: sequence and restore consistency
- L5 Functional: physical brightness correlation (if required)
- L6 Recovery: re-init/reload continuity

---

## Output Schema (per case)

```json
{
  "case_id": "VGA.Brightness.SEQUENCE_SETGET",
  "category": "VGA.Brightness",
  "target": "Panel0",
  "parameters": {
    "test_sequence": [0,25,50,75,100],
    "settle_time_ms": 800
  },
  "actual": {
    "readback_sequence": [0,25,50,75,100],
    "physical_series": [10,30,55,77,99]
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
    "vga_brightness_setget_log.json",
    "vga_brightness_physical.csv",
    "vga_brightness_recovery_log.json"
  ],
  "suspected_layers": []
}
```

---

## Platform Verdict Contribution

VGA.Brightness verdict:
- `PASS`: required cases pass
- `CONDITIONAL`: API/readback pass, functional source unavailable
- `FAIL`: any required case fails
- `ABORTED`: restore/recovery framework aborted

Always keep `BLOCKED_*` explicit for follow-up.