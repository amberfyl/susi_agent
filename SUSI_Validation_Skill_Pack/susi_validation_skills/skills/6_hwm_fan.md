# Skill: Validate HWM.Fan — Semi-Automated Flow (with Discussion-Ready Preconditions)

## Scope

Validate `[HWM.Fan]` using:
- PowerShell runner
- C# wrapper (P/Invoke to `Susi4.dll`)
- RPM time-series sampling
- BIOS reference comparison (tolerance-based)
- Optional correlation with Fan.Control or load stimulus

This category is harder than pure read-only HWM channels because fan behavior is affected by EC policy, thermal inertia, and channel mapping ambiguity.

---

## Why difficulty increases from FAN onward

Compared to temperature/voltage, FAN validation usually needs:
1. Clear physical fan-to-channel mapping (CPU/SYS/OEMx)
2. Controlled stimulus (load or fan control) to prove response
3. Enough settle time to avoid false fail from lag/hysteresis
4. Optional external reference (tachometer/BIOS trend) for stronger evidence

So yes — from FAN onward difficulty increases, and we must explicitly track prerequisites and blockers.

---

## Required Conditions (must have for full functional PASS)

### A) Minimum required for API-level validation
- `required_fan_channels` (e.g. CPU, SYSTEM)
- `fan_channel_id_map` (channel->SUSI ID)
- `sample_count`, `sample_interval_ms`
- `rpm_plausible_min`, `rpm_plausible_max` per channel

### B) Required for functional correlation PASS
- Channel mapping evidence (BIOS labels / wiring map / platform profile)
- One controlled stimulus source:
  - CPU/system load profile, or
  - `[HWM.Fan.Control]` test sequence
- `settle_time_sec` and `expected_delta_rpm_min`

### C) Optional but strongly recommended
- BIOS fan snapshot/value with timestamp
- External tachometer or fixture

---

## Missing Conditions (how to classify instead of hard fail)

If any condition below is missing, do NOT fake functional PASS:

- Missing channel mapping evidence -> `BLOCKED_REFERENCE`
- Missing controllable stimulus -> `BLOCKED_PARAMETER`
- Missing fixture for physical confirmation (when required by policy) -> `BLOCKED_FIXTURE`
- BIOS reference too old / no timestamp -> `BLOCKED_REFERENCE`

Use `PASS` only for layers actually validated.

---

## Step 0 — Probe Gate (Mandatory)

1. Initialize SUSI library
2. Probe required fan channels
3. Build available/unavailable list

Decision:
- Required channel unsupported -> `FAIL_CAPABILITY`
- Optional channel unsupported -> record and continue

---

## Automated Test Flow

## Case A — Capability & Baseline Sampling

Goal: prove channels exist and RPM values are readable/plausible.

Steps:
1. Read each required channel once
2. Sample each channel N times
3. Compute min/max/avg/stddev
4. Validate plausible RPM range

Pass criteria:
- Required channels readable
- Sample success rate meets policy
- Values plausible

Fail mapping:
- read/API error -> `FAIL_API`
- required channel unsupported -> `FAIL_CAPABILITY`
- impossible RPM values -> `FAIL_FUNCTIONAL`

## Case B — BIOS Correlation (when enabled)

Goal: correlate SUSI RPM with BIOS RPM using tolerance, not exact match.

Steps:
1. Validate BIOS reference freshness (`max_reference_age_sec`)
2. Read SUSI near BIOS capture time
3. Compare per-channel delta (`bios_tolerance_rpm`)

Pass criteria:
- Required mapped channels within tolerance

Fail mapping:
- missing/stale BIOS reference -> `BLOCKED_REFERENCE`
- out-of-tolerance -> `FAIL_FUNCTIONAL`

## Case C — Stimulus Response (recommended)

Goal: show expected RPM direction under controlled stimulus.

Stimulus options:
- Thermal load (CPU stress)
- Fan.Control setpoint sequence (if available)

Steps:
1. Record baseline RPM series
2. Apply stimulus
3. Wait `settle_time_sec`
4. Sample response window
5. Compute delta and direction

Pass criteria:
- Target channel changes in expected direction
- Delta >= `expected_delta_rpm_min`

Fail mapping:
- no stimulus source -> `BLOCKED_PARAMETER`
- wrong/no response -> `FAIL_FUNCTIONAL`

## Case D — Recovery/Re-init

Goal: verify channels remain coherent after re-init/reload.

Steps:
1. SUSI re-init (or driver reload per policy)
2. Re-read required channels
3. Validate readability + plausible range

Pass criteria:
- channels still readable and plausible

Fail mapping:
- channel lost/corrupt after recovery -> `FAIL_RECOVERY`

## Negative Cases

- invalid fan channel ID read
- unsupported optional channel read

Expected:
- proper unsupported/invalid status

---

## L1~L6 Mapping (HWM.Fan)

- L1 Configuration: channel map/sampling/tolerance/stimulus parameters complete
- L2 Capability: required channels exposed
- L3 API Command: read API status
- L4 Read-back: repeated-read consistency
- L5 Functional: BIOS correlation + stimulus response direction
- L6 Recovery: re-init/reload continuity

---

## Human Discussion Pack (for cross-team review)

When this skill reports BLOCKED/FAIL, include this checklist in report:

1. Channel mapping status
- Is CPU/SYS/OEMx mapping documented?
- Source of truth: BIOS page / schematic / platform profile

2. Stimulus availability
- Can we run safe reproducible CPU load?
- Is Fan.Control route available and stable?

3. Timing policy
- Is settle time sufficient for this platform?
- Any EC hysteresis behavior documented?

4. Reference quality
- BIOS timestamp available?
- Need external tachometer?

5. Decision request
- Accept API-level PASS only, or require functional PASS?
- If functional PASS required, who provides fixture/mapping?

---

## Output Schema (per case)

```json
{
  "case_id": "HWM.Fan.CPU.STIMULUS_RESPONSE",
  "category": "HWM.Fan",
  "target": "CPU",
  "parameters": {
    "sample_count": 20,
    "sample_interval_ms": 1000,
    "settle_time_sec": 15,
    "expected_delta_rpm_min": 300
  },
  "actual": {
    "baseline_avg_rpm": 2100,
    "response_avg_rpm": 2650,
    "delta_rpm": 550,
    "direction": "up"
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
    "hwm_fan_series.csv",
    "hwm_fan_stimulus_analysis.json",
    "bios_fan_reference.json"
  ],
  "suspected_layers": []
}
```

---

## Platform Verdict Contribution

HWM.Fan verdict:
- `PASS`: required cases pass
- `CONDITIONAL`: API/readback pass but functional case blocked by missing conditions
- `FAIL`: required case fails
- `ABORTED`: safety/recovery framework aborted

Always keep BLOCKED reasons explicit for human follow-up.