# Skill: Validate ThermalProtect — Automated Flow

## Scope

Validate `[ThermalProtect]` with an automation-first method using:
- PowerShell runner
- C# wrapper (P/Invoke to `Susi4.dll`)
- Safe threshold set/get/read-back/restore flow
- Optional trigger-action test under strict safety guard

This skill is safety-sensitive. Any out-of-policy threshold operation must be blocked.

## Objective

Verify all required thermal-protection behaviors:
1. Declared channel capability and legal range are correct
2. Threshold get/set/read-back is correct
3. Original threshold can be restored exactly
4. Optional persistence behavior (driver reload/reboot) matches expectation
5. Optional trigger action works when explicitly enabled
6. Any restore failure aborts this category

---

## Required Inputs

### A) Platform/Test metadata
- `platform_name`
- `bios_version` (optional but recommended)
- `ec_fw_version` (optional)
- `driver_version` / `dll_version` (optional)

### B) Thermal policy
- `channel_id` (or channel list)
- `channel_declared_required` (true/false)
- `legal_min`, `legal_max`
- `unit`, `step`
- `approved_test_value` or `approved_delta`
- `allow_set` (true/false)
- `require_persistence_after_reload` (true/false)
- `require_persistence_after_reboot` (true/false)

### C) Trigger-action policy (optional)
- `allow_trigger_test` (true/false)
- `expected_action` (fan boost/throttle/alarm/shutdown/other)
- `action_observation_source` (SUSI values / OS telemetry / fixture)
- `safety_stop_temp_c`
- `max_stimulus_duration_sec`

### D) Safety policy
- `abort_on_restore_failure` (must be true in production flow)
- `max_set_attempts`

### E) Resume mechanism (for reboot persistence)
- `checkpoint_file` (absolute path)
- `resume_script` or startup trigger

---

## Step 0 — Probe Gate (Mandatory)

Before set/get test:
1. Initialize SUSI library
2. Query thermal-protect channel capability, range, unit, access mode
3. Validate test value against legal range and step

Decision rules:
- INI/profile requires channel but unsupported -> `FAIL_CAPABILITY`
- Missing legal range or approved test value -> `BLOCKED_PARAMETER`
- Test value out of legal range -> `BLOCKED_PARAMETER`
- Channel not required on this platform -> `N_A` or `UNSUPPORTED_PASS`

Do not run set/write actions before gate passes.

---

## Automated Test Flow

## Case A — Capability & Range Discovery

Goal: verify channel exposure and legal threshold constraints.

Steps:
1. Query declared channel(s)
2. Record range/unit/step/access mode
3. Validate profile policy against queried capability

Pass criteria:
- Channel is exposed as expected
- Range/unit/step valid
- Access mode known

Fail mapping:
- unsupported required channel -> `FAIL_CAPABILITY`
- API query error -> `FAIL_API`

## Case B — Baseline Get

Goal: verify current threshold is readable and plausible.

Steps:
1. Read current threshold/state
2. Validate within legal range
3. Save as `original_threshold`

Pass criteria:
- Read success
- Value in range

Fail mapping:
- get error -> `FAIL_API`
- out-of-range value -> `FAIL_READBACK` or `FAIL_FUNCTIONAL` (per policy)

## Case C — Safe Set / Read-back / Restore (Core)

Run only when `allow_set=true` and channel is writable.

Goal: prove threshold write path works and can be restored.

Steps:
1. Ensure `original_threshold` exists
2. Compute test target from approved value/delta (must remain legal)
3. Set threshold to test target
4. Read back and compare
5. In `finally` block:
   - restore `original_threshold`
   - read back again
   - compare exact restoration

Pass criteria:
- Set returns success
- Read-back matches expected target
- Restore matches original exactly

Fail mapping:
- set/get error -> `FAIL_API`
- read-back mismatch -> `FAIL_READBACK`
- restore mismatch/error -> `ABORTED_RESTORE_FAILURE`

## Case D — Persistence After Driver Reload (Optional)

Run when `require_persistence_after_reload=true`.

Goal: verify expected threshold persistence model across driver disable/enable.

Steps:
1. Set approved temporary threshold
2. Disable/enable SUSI driver (or re-init as platform policy)
3. Read threshold after reload
4. Compare with expected persistence behavior
5. Restore original threshold and verify

Pass criteria:
- Post-reload behavior matches expected model
- Restore passes

Fail mapping:
- reload test mismatch -> `FAIL_RECOVERY` or `FAIL_FUNCTIONAL` (per policy)
- restore failure -> `ABORTED_RESTORE_FAILURE`

## Case E — Persistence After Reboot (Optional)

Run when `require_persistence_after_reboot=true` and reboot policy allows.

Goal: verify reboot persistence with checkpoint continuity.

Pre-steps:
1. Write checkpoint with case metadata and expected behavior
2. Set approved temporary threshold
3. Trigger controlled reboot path

Post-boot resume:
1. Resume runner and validate checkpoint chain
2. Read threshold value
3. Compare with expected reboot persistence behavior
4. Restore original threshold and verify
5. Close checkpoint

Pass criteria:
- Resume evidence exists
- Behavior matches expectation
- Restore exact match

Fail mapping:
- checkpoint/resume failure -> `FAIL_RECOVERY`
- persistence mismatch -> `FAIL_FUNCTIONAL`
- restore failure -> `ABORTED_RESTORE_FAILURE`

## Case F — Trigger Action Test (Optional, Safety-Guarded)

Run only when `allow_trigger_test=true`.

Goal: verify threshold crossing causes expected platform action.

Steps:
1. Set safe, reachable threshold target
2. Apply controlled stimulus (e.g., CPU load)
3. Observe temperature and action evidence over time
4. Stop stimulus at `safety_stop_temp_c` or max duration
5. Restore original threshold/policy

Pass criteria:
- Threshold crossing observed
- Expected action observed from defined evidence source
- No safety violation
- Restore passes

Fail mapping:
- crossing occurred but no action -> `FAIL_FUNCTIONAL`
- cannot observe action due to missing fixture/source -> `BLOCKED_FIXTURE` or `BLOCKED_REFERENCE`
- safety violation -> `ABORTED`

## Negative Cases

Run non-destructive invalid calls:
- set below legal_min
- set above legal_max
- wrong step alignment
- invalid channel
- set when access mode is read-only

Expected:
- API rejects invalid requests with expected status

Fail mapping:
- invalid request accepted silently -> `FAIL_API`

---

## L1~L6 Mapping (ThermalProtect)

- L1 Configuration: channel/range/step/policy/test value are complete and parseable
- L2 Capability: declared channel exposure and writable mode match profile/INI
- L3 API Command: get/set/restore/reload/reinit call statuses
- L4 Read-back: set/get and restore value comparisons
- L5 Functional: trigger action behavior and directionality (when required)
- L6 Recovery: reload/reboot resume continuity and post-test restoration

A case is PASS only when required layers for that case pass.

---

## Safety Rules (Hard)

1. Never set threshold outside legal range
2. Never run trigger case without explicit enablement
3. Always save original threshold before first set
4. Always restore in `finally` block
5. Any restore failure => stop category immediately
6. Trigger tests must enforce stop condition (`safety_stop_temp_c`, max duration)

---

## Output Schema (per case)

```json
{
  "case_id": "ThermalProtect.CH0.SET_GET_RESTORE",
  "category": "ThermalProtect",
  "target": "CH0",
  "parameters": {
    "original_threshold": 85,
    "test_threshold": 80,
    "unit": "C"
  },
  "api_calls": [
    {"name":"ThermalGet","status":"SUSI_STATUS_SUCCESS","ts":"..."},
    {"name":"ThermalSet","status":"SUSI_STATUS_SUCCESS","ts":"..."},
    {"name":"ThermalGet","status":"SUSI_STATUS_SUCCESS","ts":"..."}
  ],
  "validation_layers": {
    "L1_configuration": "PASS",
    "L2_capability": "PASS",
    "L3_api": "PASS",
    "L4_readback": "PASS",
    "L5_functional": "N_A",
    "L6_recovery": "PASS"
  },
  "result": "PASS",
  "restore_result": "PASS",
  "evidence": [
    "thermal_set_get_log.json",
    "thermal_restore_log.json",
    "thermal_trigger_series.csv",
    "thermal_reboot_checkpoint.json"
  ],
  "suspected_layers": []
}
```

---

## Suggested Runner Order

1. Case A Capability/Range
2. Case B Baseline Get
3. Case C Safe Set/Read-back/Restore
4. Case D Reload Persistence (if required)
5. Case E Reboot Persistence (if required)
6. Case F Trigger Action (if enabled)
7. Negative Cases
8. Final summary + verdict

---

## Platform Verdict Contribution

ThermalProtect category verdict:
- `PASS`: all required cases pass
- `CONDITIONAL`: no fail, but optional trigger/persistence case skipped by policy
- `FAIL`: any required case fails
- `ABORTED`: safety/recovery framework failed

Always report blocked/unsupported explicitly; do not hide them as generic fail.