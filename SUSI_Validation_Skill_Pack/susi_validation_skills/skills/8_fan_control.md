# Skill: Validate HWM.Fan.Control — Automated Flow

## Scope

Validate `[HWM.Fan.Control]` with automation-first method using:
- PowerShell runner
- C# wrapper (P/Invoke to `Susi4.dll`)
- Control set/get sequence
- Correlated RPM observation from `[HWM.Fan]`
- Restore/recovery checks

This category is control-path validation. API success alone is not enough; functional proof needs RPM response evidence.

## Objective

Verify all required fan-control behaviors:
1. Declared control channels are exposed and writable as expected
2. Set/Get returns coherent control values
3. Controlled fan RPM changes in expected direction and magnitude
4. Wrong fan is not primary responder (channel isolation)
5. Original mode/value can be restored safely
6. Re-init/reload still keeps control/readback coherent

---

## Dependency with HWM.Fan (Important)

Yes, this skill explicitly depends on `[HWM.Fan]` for functional validation:
- L3/L4 (API + read-back) can run without RPM
- L5 functional PASS requires correlated RPM evidence from `HWM.Fan.*`

If control API works but no usable RPM channel is available:
- do not force FAIL
- classify as `CONDITIONAL` with `BLOCKED_REFERENCE` (or `BLOCKED_FIXTURE` per policy)

---

## Required Inputs

### A) Platform/Test metadata
- `platform_name`
- `bios_version` (recommended)
- `ec_fw_version` (optional)
- `driver_version` / `dll_version` (optional)

### B) Control policy
- `control_channels` (required list)
- `control_id_map` (channel->SUSI ID)
- `mode_policy` (manual/auto or platform-defined)
- `control_min`, `control_max`, `control_step`
- `test_sequence` (e.g. 20/40/60/80)
- `settle_time_sec`

### C) RPM correlation policy (from HWM.Fan)
- `rpm_channel_map` (control channel -> expected fan RPM channel)
- `rpm_sample_count`
- `rpm_sample_interval_ms`
- `expected_delta_rpm_min`
- `isolation_ratio_threshold` (to detect wrong-fan response)

### D) Safety policy
- `abort_on_restore_failure` (true)
- `max_set_attempts`
- `skip_zero_rpm_channels` (policy-based)

---

## Step 0 — Probe Gate (Mandatory)

Before control tests:
1. Initialize SUSI library
2. Probe all required control channels
3. Probe mapped RPM channels (`HWM.Fan`) for correlation

Decision rules:
- Required control channel unsupported -> `FAIL_CAPABILITY`
- Required control channel read-only -> `FAIL_CAPABILITY` or `BLOCKED_PARAMETER` (policy)
- No mapped RPM channel available -> allow API-level test, mark functional branch blocked

---

## Automated Test Flow

## Case A — Capability & Range Discovery

Goal: verify control channel exposure and legal range.

Steps:
1. Query control channel capability
2. Read range/mode info
3. Validate `test_sequence` within legal range

Pass criteria:
- Required control channels writable/usable
- Sequence values legal

Fail mapping:
- unsupported required channel -> `FAIL_CAPABILITY`
- range query error -> `FAIL_API`
- illegal sequence value -> `BLOCKED_PARAMETER`

## Case B — Set/Get Consistency

Goal: verify control write and read-back coherence.

Steps:
1. Read and store original mode/value
2. For each value in sequence:
   - set control value
   - read back control value
   - compare against expected
3. In finally:
   - restore original mode/value
   - read-back restore verification

Pass criteria:
- Set/Get coherent for all sequence points
- Restore verified

Fail mapping:
- set/get API error -> `FAIL_API`
- read-back mismatch -> `FAIL_READBACK`
- restore mismatch/error -> `ABORTED_RESTORE_FAILURE`

## Case C — RPM Functional Correlation (L5)

Goal: prove control affects the intended fan.

Steps:
1. For each sequence point:
   - set control value
   - wait `settle_time_sec`
   - sample mapped RPM channel N times
   - compute avg RPM
2. Analyze direction and delta across sequence

Pass criteria:
- Expected direction matches policy (usually control up => RPM up)
- Max-min delta >= `expected_delta_rpm_min`

Fail mapping:
- control changes but RPM unchanged -> `FAIL_FUNCTIONAL`
- RPM data unavailable -> `BLOCKED_REFERENCE` / `BLOCKED_FIXTURE`

## Case D — Channel Isolation

Goal: verify the intended fan is primary responder.

Steps:
1. Control channel A with sequence
2. Record all observable fan RPM channels
3. Compare target delta vs non-target deltas

Pass criteria:
- target fan shows dominant response above threshold

Fail mapping:
- wrong fan dominant -> `FAIL_FUNCTIONAL`
- cannot observe enough channels -> `BLOCKED_REFERENCE`

## Case E — Recovery / Re-init

Goal: verify control path remains coherent after recovery operations.

Steps:
1. SUSI re-init (or driver reload per platform policy)
2. Re-run a short set/get sample
3. Verify restore path still works

Pass criteria:
- channel remains controllable/readable
- restore remains valid

Fail mapping:
- control path broken after recovery -> `FAIL_RECOVERY`

## Negative Cases

- set below min / above max
- invalid control channel ID
- set while in disallowed mode (if defined)

Expected:
- API rejects invalid operations with expected status

Fail mapping:
- invalid set accepted silently -> `FAIL_API`

---

## L1~L6 Mapping (HWM.Fan.Control)

- L1 Configuration: channel maps/sequence/timing/tolerance parseable
- L2 Capability: control channels exposed and writable
- L3 API Command: set/get/mode calls status
- L4 Read-back: control value and restore verification
- L5 Functional: RPM response + channel isolation via `HWM.Fan`
- L6 Recovery: re-init/reload continuity

A case is PASS only when required layers for that case pass.

---

## Safety Rules (Hard)

1. Always backup original mode/value before first set
2. Always restore in `finally` block
3. Restore failure => immediate `ABORTED_RESTORE_FAILURE`
4. Do not claim full PASS from set/get only when L5 is required
5. Keep blocked reasons explicit for human follow-up

---

## Output Schema (per case)

```json
{
  "case_id": "HWM.Fan.Control.CPU.SEQUENCE_CORRELATION",
  "category": "HWM.Fan.Control",
  "target": "CPU",
  "parameters": {
    "test_sequence": [20, 40, 60, 80],
    "settle_time_sec": 10,
    "rpm_channel": "HWM_FAN_CPU"
  },
  "actual": {
    "control_readback": [20, 40, 60, 80],
    "rpm_avg": [1800, 2200, 2600, 3000],
    "delta_rpm": 1200
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
    "fan_control_setget_log.json",
    "fan_rpm_correlation.csv",
    "fan_isolation_analysis.json"
  ],
  "suspected_layers": []
}
```

---

## Platform Verdict Contribution

HWM.Fan.Control verdict:
- `PASS`: required cases pass (including required L5)
- `CONDITIONAL`: API/readback pass, but L5 blocked by missing RPM evidence
- `FAIL`: any required case fails
- `ABORTED`: safety/recovery restore failure

Always separate `BLOCKED_*` from `FAIL` in final summary.