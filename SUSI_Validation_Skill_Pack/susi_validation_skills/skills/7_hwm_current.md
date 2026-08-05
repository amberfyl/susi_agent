# Skill: Validate HWM.Current — Semi-Automated Flow

## Scope

Validate `[HWM.Current]` using:
- PowerShell runner
- C# wrapper (P/Invoke to `Susi4.dll`)
- Time-series sampling
- Optional BIOS/reference correlation
- Optional load-stimulus trend validation
- Recovery consistency checks

This category is harder for absolute correctness. API readability can be automated well, but absolute current accuracy usually needs external instrumentation.

## Objective

Verify all required current-sensor behaviors:
1. Declared current channels are exposed by API
2. Returned values are plausible and stable over sampling window
3. Reference correlation (BIOS/other source) is tolerance-based, not exact-equality
4. Optional load stimulus causes expected trend direction
5. Re-init/reload keeps channels readable and coherent
6. Reporting clearly separates API-level pass vs full functional/accuracy proof

---

## Required Inputs

### A) Platform/Test metadata
- `platform_name`
- `bios_version` (optional)
- `ec_fw_version` (optional)
- `driver_version` / `dll_version` (optional)

### B) Channel policy
- `required_current_channels` (e.g. OEM0/OEM1...)
- `optional_current_channels`
- `current_channel_id_map` (channel->SUSI ID)

### C) Sampling policy
- `sample_count`
- `sample_interval_ms`
- `plausible_min_ma`, `plausible_max_ma` (per-channel)
- `max_jitter_ma` (optional)

### D) Reference policy (optional)
- `reference_enabled` (true/false)
- `reference_values_ma` (channel->value + timestamp + source)
- `reference_tolerance_ma` (per-channel preferred)
- `max_reference_age_sec`

### E) Stimulus policy (optional)
- `allow_stimulus_test` (true/false)
- `stimulus_type` (CPU/system workload etc.)
- `stimulus_duration_sec`
- `expected_delta_ma_min`

### F) Accuracy policy
- `absolute_accuracy_required` (true/false)
- `external_meter_required` (true/false)
- `external_meter_source` (DMM/DAQ/shunt logger)

---

## Missing Conditions (do not hard-fail)

- Required channel lacks stable mapping -> `BLOCKED_REFERENCE`
- Reference source missing/stale -> `BLOCKED_REFERENCE`
- Stimulus not available for trend test -> `BLOCKED_PARAMETER`
- Absolute accuracy required but no external meter -> `BLOCKED_REFERENCE` (or `CONDITIONAL` category verdict)

---

## Step 0 — Probe Gate (Mandatory)

1. Initialize SUSI library
2. Probe required current channels
3. Build available/unavailable channel list

Decision rules:
- Required channel unsupported -> `FAIL_CAPABILITY`
- No required channel available -> category `FAIL_CAPABILITY`
- Optional channel unsupported -> record and continue

---

## Automated Test Flow

## Case A — Capability & Baseline Readability

Goal: verify channel exposure and baseline data path.

Steps:
1. Read each required channel once
2. Log raw value and decoded unit (mA)
3. Validate value plausibility against configured range

Pass criteria:
- Required channels readable
- Baseline values plausible

Fail mapping:
- read/API error -> `FAIL_API`
- required channel unsupported -> `FAIL_CAPABILITY`
- implausible baseline value -> `FAIL_FUNCTIONAL`

## Case B — Repeated Sampling Stability

Goal: verify reliability and coherence under repeated reads.

Steps:
1. Sample each required channel N times at fixed interval
2. Record timestamp/value series
3. Compute min/max/avg/stddev/jitter
4. Validate all samples within plausible range

Pass criteria:
- Sampling success rate meets policy
- No implausible spikes
- Jitter within policy if configured

Fail mapping:
- frequent read failures -> `FAIL_API`
- implausible spikes/oscillation -> `FAIL_FUNCTIONAL`

## Case C — Reference Correlation (Optional)

Goal: compare SUSI reading with available reference using tolerance.

Steps:
1. Verify reference freshness (`max_reference_age_sec`)
2. Read SUSI values near reference time window
3. Compute absolute delta per mapped channel
4. Compare against `reference_tolerance_ma`

Pass criteria:
- Required mapped channels within tolerance

Fail mapping:
- missing/stale required reference -> `BLOCKED_REFERENCE`
- out-of-tolerance delta -> `FAIL_FUNCTIONAL`

Note:
- Current can vary with workload and power states; exact equality is not required.

## Case D — Stimulus Trend Validation (Optional)

Run when `allow_stimulus_test=true`.

Goal: verify expected current-direction response under controlled load.

Steps:
1. Capture baseline series before stimulus
2. Apply workload stimulus
3. Sample during stimulus window
4. Stop stimulus and sample recovery window
5. Compare baseline vs stimulus averages and trend direction

Pass criteria:
- Required channel(s) show expected direction
- Delta >= `expected_delta_ma_min`

Fail mapping:
- stimulus unavailable -> `BLOCKED_PARAMETER`
- no expected trend -> `FAIL_FUNCTIONAL`

## Case E — Recovery/Re-init Consistency

Goal: ensure channels remain coherent after recovery operations.

Steps:
1. SUSI re-init (or driver reload per policy)
2. Re-read required channels
3. Validate readability and plausible range

Pass criteria:
- channels remain readable/coherent

Fail mapping:
- channel lost after recovery -> `FAIL_RECOVERY`
- post-recovery corrupted values -> `FAIL_RECOVERY`

## Case F — Absolute Accuracy (Optional, Instrumented)

Run only when `absolute_accuracy_required=true` and external meter source exists.

Goal: validate absolute current accuracy against independent measurement.

Steps:
1. Align sampling window between SUSI and external meter
2. Collect synchronized pairs
3. Compute MAE/max error
4. Compare with accuracy policy

Pass criteria:
- error metrics within specified limit

Fail mapping:
- no external instrument -> `BLOCKED_REFERENCE`
- out-of-spec error -> `FAIL_FUNCTIONAL`

---

## L1~L6 Mapping (HWM.Current)

- L1 Configuration: channels/ranges/reference/stimulus policy complete
- L2 Capability: required channels exposed
- L3 API Command: read call status reliability
- L4 Read-back: repeated-read coherence and stability
- L5 Functional: reference correlation + stimulus trend + optional accuracy proof
- L6 Recovery: re-init/reload continuity

A case is PASS only when required layers for that case pass.

---

## Safety Rules

1. HWM.Current checks are read-only by default
2. Do not claim accuracy PASS without external reference when accuracy is required
3. Keep blocked reasons explicit (`BLOCKED_REFERENCE`, `BLOCKED_PARAMETER`)
4. Avoid exact-value assertions without synchronized timestamp windows

---

## Human Discussion Pack (for blocked/fail)

Include in report:
1. Channel mapping confidence and source
2. Reference source availability/freshness
3. Stimulus method and repeatability
4. Whether project requires trend-only vs absolute-accuracy validation
5. External instrumentation request (if needed)

---

## Output Schema (per case)

```json
{
  "case_id": "HWM.Current.OEM0.TREND_CORRELATION",
  "category": "HWM.Current",
  "target": "OEM0",
  "parameters": {
    "sample_count": 20,
    "sample_interval_ms": 1000,
    "reference_tolerance_ma": 150
  },
  "actual": {
    "series_ma": [420, 430, 435],
    "avg_ma": 428,
    "reference_ma": 450,
    "delta_ma": 22
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
    "hwm_current_series.csv",
    "hwm_current_reference_compare.json",
    "hwm_current_stimulus_analysis.json"
  ],
  "suspected_layers": []
}
```

---

## Platform Verdict Contribution

HWM.Current verdict:
- `PASS`: required cases pass
- `CONDITIONAL`: API/readback pass, but absolute-accuracy proof not available by policy/setup
- `FAIL`: any required case fails
- `ABORTED`: recovery/safety framework aborted

Always separate `BLOCKED_*` from `FAIL` in final summary.