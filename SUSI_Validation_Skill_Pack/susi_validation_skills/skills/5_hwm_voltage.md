# Skill: Validate HWM.Voltage — Automated Flow

## Scope

Validate `[HWM.Voltage]` with an automation-first method using:
- PowerShell runner
- C# wrapper (P/Invoke to `Susi4.dll`)
- Time-series sampling
- BIOS reference comparison with tolerance window

This skill validates mapping correctness, value plausibility, and reference consistency.

## Objective

Verify all required voltage behaviors:
1. Declared voltage channels are exposed by API
2. Returned values are plausible and stable (not fixed exact match)
3. BIOS reference comparison is done with tolerance and time window
4. Re-init/reload keeps channel readability and coherent values

---

## Required Inputs

### A) Platform/Test metadata
- `platform_name`
- `bios_version` (recommended)
- `ec_fw_version` (optional)
- `driver_version` / `dll_version` (optional)

### B) Channel policy
- `required_channels` (e.g. `VCORE`, `3V3`, `5V`, `12V`)
- `optional_channels` (e.g. `VBAT`, `OEMx`, `5VSB`)
- `channel_id_map` (channel->SUSI ID)

### C) Sampling policy
- `sample_count`
- `sample_interval_ms`
- `plausible_range_mv` (per-channel min/max)
- `max_ripple_mv` (optional soft threshold)

### D) BIOS reference policy
- `bios_reference_enabled` (true/false)
- `bios_reference_values_mv` (channel->value + timestamp)
- `bios_tolerance_mv` (per-channel preferred)
- `max_reference_age_sec`

### E) Recovery policy
- `check_after_reinit` (true/false)
- `check_after_driver_reload` (true/false)

---

## Step 0 — Probe Gate (Mandatory)

1. Initialize SUSI library
2. Probe all required channels
3. Build available/unavailable channel lists

Decision rules:
- Required channel unsupported -> `FAIL_CAPABILITY`
- Optional channel unsupported -> record and continue
- No required channel available -> category `FAIL_CAPABILITY`

---

## Automated Test Flow

## Case A — Capability & Channel Mapping

Goal: verify channel exposure and declared mapping.

Steps:
1. Read each required channel once
2. Log channel name, ID, status, raw mV
3. Validate required channel map completeness

Pass criteria:
- All required channels readable

Fail mapping:
- unsupported required channel -> `FAIL_CAPABILITY`
- API error -> `FAIL_API`

## Case B — Repeated Sampling Stability

Goal: verify voltage read stability and plausibility over time.

Steps:
1. Sample each required channel N times
2. Record timestamp + mV value
3. Compute min/max/avg/stddev/ripple
4. Validate all values within per-channel plausible range

Pass criteria:
- Read success rate meets policy
- Values plausible
- Ripple within expected envelope (if configured)

Fail mapping:
- frequent API failures -> `FAIL_API`
- impossible values -> `FAIL_FUNCTIONAL`

## Case C — BIOS Reference Correlation (Tolerance-Based)

Goal: compare SUSI and BIOS with realistic tolerance, not exact equality.

Steps:
1. Validate BIOS reference timestamp freshness
2. Read SUSI values in near-time window
3. For mapped channels, compute absolute delta (mV)
4. Compare against channel tolerance (`bios_tolerance_mv`)

Pass criteria:
- Required mapped channels within tolerance

Fail mapping:
- missing required BIOS reference -> `BLOCKED_REFERENCE`
- stale BIOS reference -> `BLOCKED_REFERENCE`
- out-of-tolerance delta -> `FAIL_FUNCTIONAL`

Notes:
- Voltage naturally varies; exact match is NOT required
- Use tolerance + time-near sampling as mandatory method

## Case D — Recovery / Re-init Consistency

Goal: verify readings remain coherent after re-init/reload.

Steps:
1. Re-init SUSI (or reload driver if policy says so)
2. Re-read required channels
3. Validate still readable and plausible

Pass criteria:
- channels remain readable
- no corrupted/implausible values after recovery

Fail mapping:
- channel lost after re-init -> `FAIL_RECOVERY`
- post-recovery corruption -> `FAIL_RECOVERY`

## Negative Cases

- invalid channel ID read
- unsupported optional channel read

Expected:
- proper invalid/unsupported status

Fail mapping:
- invalid read unexpectedly success -> `FAIL_API`

---

## L1~L6 Mapping (HWM.Voltage)

- L1 Configuration: channel map/ranges/tolerances parseable
- L2 Capability: required channels exposed
- L3 API Command: read API status
- L4 Read-back: repeated-read consistency and data coherence
- L5 Functional: BIOS tolerance correlation and trend reasonableness
- L6 Recovery: re-init/reload continuity

---

## Safety Rules

1. Voltage checks are read-only by default
2. Never require exact BIOS equality
3. Always use tolerance + reference timestamp window
4. Keep unsupported/blocked separated from failure

---

## Output Schema (per case)

```json
{
  "case_id": "HWM.Voltage.5V.BIOS_CORRELATION",
  "category": "HWM.Voltage",
  "target": "5V",
  "parameters": {
    "sample_count": 20,
    "sample_interval_ms": 1000,
    "bios_tolerance_mv": 250
  },
  "actual": {
    "series_mv": [5010, 5024, 5008],
    "avg_mv": 5014,
    "bios_ref_mv": 5000,
    "delta_mv": 14
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
  "restore_result": "N_A",
  "evidence": [
    "hwm_voltage_series.csv",
    "bios_voltage_reference.json",
    "hwm_voltage_analysis.json"
  ],
  "suspected_layers": []
}
```

---

## Suggested Runner Order

1. Case A Capability/Mapping
2. Case B Sampling Stability
3. Case C BIOS Correlation (if enabled)
4. Case D Recovery/Re-init
5. Negative Cases
6. Final summary + verdict

---

## Platform Verdict Contribution

HWM.Voltage verdict:
- `PASS`: all required cases pass
- `CONDITIONAL`: no fail, but optional BIOS reference unavailable
- `FAIL`: any required case fails
- `ABORTED`: recovery framework failed

Always report `BLOCKED_REFERENCE` separately from `FAIL`.