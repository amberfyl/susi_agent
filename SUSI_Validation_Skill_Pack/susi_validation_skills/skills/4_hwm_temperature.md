# Skill: Validate HWM.Temperature — Automated Flow

## Scope

Validate `[HWM.Temperature]` with an automation-first method using:
- PowerShell runner
- C# wrapper (P/Invoke to `Susi4.dll`)
- Time-series sampling
- BIOS reference comparison (when screenshot/reference values are provided)
- Optional controlled-stimulus trend validation

This skill focuses on channel correctness, value plausibility, reference consistency, and dynamic response direction.

## Objective

Verify all required temperature behaviors:
1. Declared temperature channels are actually exposed by API
2. Returned values decode correctly and remain plausible
3. BIOS reference values can be correlated within tolerance
4. Controlled stimulus causes expected trend direction (optional but recommended)
5. Re-init/reload still returns stable and coherent readings

---

## Required Inputs

### A) Platform/Test metadata
- `platform_name`
- `bios_version` (recommended)
- `ec_fw_version` (optional)
- `driver_version` / `dll_version` (optional)

### B) Channel policy
- `required_channels` (e.g. `CPU`, `SYSTEM`)
- `optional_channels` (e.g. `CHIPSET`, `OEMx`)
- `channel_id_map` (SUSI ID mapping used by wrapper)

### C) Sampling policy
- `sample_count`
- `sample_interval_ms`
- `plausible_min_c`
- `plausible_max_c`
- `max_jitter_c` (optional)

### D) BIOS reference policy
- `bios_reference_enabled` (true/false)
- `bios_reference_values` (channel->value, with capture timestamp)
- `bios_tolerance_c`
- `max_reference_age_sec`

### E) Stimulus policy (optional)
- `allow_stimulus_test` (true/false)
- `stimulus_type` (e.g. CPU load)
- `stimulus_duration_sec`
- `expected_delta_min_c`
- `post_stimulus_cooldown_sec`

---

## Step 0 — Probe Gate (Mandatory)

Before temperature cases:
1. Initialize SUSI library
2. Query all channels in `required_channels`
3. Build available/unavailable channel set

Decision rules:
- Required channel unsupported -> `FAIL_CAPABILITY`
- Optional channel unsupported -> record and continue
- No required channel available -> category `FAIL_CAPABILITY`

Do not run reference/stimulus checks if capability gate fails.

---

## Automated Test Flow

## Case A — Capability & Decode Validation

Goal: ensure channel availability and decoding logic are correct.

Steps:
1. For each required channel, call read API once
2. Decode raw value to Celsius using project rule
3. Log raw + decoded values
4. Verify decoded value in plausible range

Pass criteria:
- All required channels readable
- Decode succeeds
- Initial values plausible

Fail mapping:
- read error -> `FAIL_API`
- required channel unsupported -> `FAIL_CAPABILITY`
- decoded out-of-range -> `FAIL_READBACK` or `FAIL_FUNCTIONAL` (per policy)

## Case B — Repeated Sampling Stability

Goal: verify repeated reads are stable and API reliability is acceptable.

Steps:
1. For each required channel, sample N times at fixed interval
2. Record timestamp, raw value, decoded Celsius
3. Compute min/max/avg/stddev/jitter
4. Verify all values remain plausible

Pass criteria:
- Sampling call success rate meets policy
- No impossible jumps beyond policy (if `max_jitter_c` set)
- Statistical outputs generated

Fail mapping:
- frequent call failures -> `FAIL_API`
- implausible spikes -> `FAIL_FUNCTIONAL`

## Case C — BIOS Reference Correlation (When Enabled)

Goal: correlate SUSI channel readings with BIOS reference values.

Steps:
1. Validate BIOS reference timestamp freshness
2. Read SUSI values near comparison time
3. For each mapped channel, compute absolute delta
4. Compare delta against `bios_tolerance_c`

Pass criteria:
- Required mapped channels within tolerance

Fail mapping:
- missing required BIOS reference -> `BLOCKED_REFERENCE`
- stale reference age -> `BLOCKED_REFERENCE`
- out-of-tolerance delta -> `FAIL_FUNCTIONAL`

Notes:
- If BIOS shows only subset channels, only mapped subset is mandatory
- Unmapped channels should not be forced to fail

## Case D — Stimulus Trend Test (Optional)

Run only when `allow_stimulus_test=true`.

Goal: verify expected temperature trend direction under controlled load.

Steps:
1. Capture baseline series before stimulus
2. Apply stimulus (e.g. CPU load)
3. Sample during stimulus window
4. Stop stimulus and sample cooldown window
5. Compute baseline vs peak delta and direction

Pass criteria:
- Required channel(s) rise by at least `expected_delta_min_c`
- Trend direction is correct (up during stimulus, stabilizing/down in cooldown)

Fail mapping:
- stimulus run failure -> `FAIL_API` or `BLOCKED_PARAMETER`
- no expected trend -> `FAIL_FUNCTIONAL`

## Case E — Recovery / Re-init Consistency

Goal: verify readings remain coherent after re-init/reload.

Steps:
1. Perform SUSI uninit/init or driver reload (as allowed by platform policy)
2. Re-read required channels
3. Compare with pre-reinit plausible envelope

Pass criteria:
- channels still readable
- values remain plausible and non-corrupt

Fail mapping:
- channel lost after reinit -> `FAIL_RECOVERY`
- read corruption/out-of-range after recovery -> `FAIL_RECOVERY`

## Negative Cases

Run safe invalid reads:
- invalid channel ID
- unsupported optional channel read

Expected:
- API returns proper unsupported/invalid status

Fail mapping:
- invalid channel unexpectedly treated as success -> `FAIL_API`

---

## L1~L6 Mapping (HWM.Temperature)

- L1 Configuration: channels/tolerances/sampling settings parseable
- L2 Capability: required channels exposed as declared
- L3 API Command: read calls status and reliability
- L4 Read-back: decoding consistency and repeated-read coherence
- L5 Functional: BIOS correlation and stimulus trend behavior
- L6 Recovery: re-init/reload continuity

A case is PASS only when required layers for that case pass.

---

## Safety Rules

1. Temperature read tests are non-destructive by default
2. Stimulus tests require explicit enablement and bounded duration
3. Stop stimulus immediately on safety threshold breach
4. Do not claim functional pass from API success alone when reference/stimulus is required

---

## Output Schema (per case)

```json
{
  "case_id": "HWM.Temperature.CPU.BIOS_CORRELATION",
  "category": "HWM.Temperature",
  "target": "CPU",
  "parameters": {
    "sample_count": 20,
    "sample_interval_ms": 1000,
    "bios_tolerance_c": 5.0
  },
  "actual": {
    "series_c": [52.0, 52.5, 53.0],
    "avg_c": 52.5,
    "bios_ref_c": 54.0,
    "delta_c": 1.5
  },
  "api_calls": [
    {"name":"HwmTempRead","status":"SUSI_STATUS_SUCCESS","ts":"..."}
  ],
  "validation_layers": {
    "L1_configuration": "PASS",
    "L2_capability": "PASS",
    "L3_api": "PASS",
    "L4_readback": "PASS",
    "L5_functional": "PASS",
    "L6_recovery": "PASS"
  },
  "result": "PASS",
  "restore_result": "N_A",
  "evidence": [
    "hwm_temp_series.csv",
    "bios_reference_map.json",
    "hwm_temp_analysis.json"
  ],
  "suspected_layers": []
}
```

---

## Suggested Runner Order

1. Case A Capability/Decode
2. Case B Sampling Stability
3. Case C BIOS Correlation (if enabled)
4. Case D Stimulus Trend (if enabled)
5. Case E Recovery/Re-init
6. Negative Cases
7. Final summary + verdict

---

## Platform Verdict Contribution

HWM.Temperature verdict:
- `PASS`: all required cases pass
- `CONDITIONAL`: no fail, but optional reference/stimulus case skipped by policy
- `FAIL`: any required case fails
- `ABORTED`: safety framework triggered abort

Always separate unsupported/blocked from failure in final report.