# Skill: Validate HWM.CaseOpen — Semi-Automated Flow

## Scope

Validate `[HWM.CaseOpen]` using:
- PowerShell runner
- C# wrapper (P/Invoke to `Susi4.dll`)
- Event-state sampling
- Optional external trigger fixture / manual switch action

This category is input-event validation. Difficulty is medium-high because functional proof usually needs physical trigger.

## Objective

Verify all required CaseOpen behaviors:
1. Declared CaseOpen channels are exposed by API
2. Baseline state is readable and stable
3. Trigger action causes expected state transition
4. Restore/clear behavior is correct after event
5. Re-init/reload preserves coherent read behavior

---

## Required Inputs

### A) Platform/Test metadata
- `platform_name`
- `bios_version` (recommended)
- `ec_fw_version` (optional)
- `driver_version` / `dll_version` (optional)

### B) Channel policy
- `required_caseopen_channels`
- `caseopen_id_map` (channel->SUSI ID)
- `state_encoding` (0/1 or platform-specific)

### C) Trigger policy
- `trigger_mode` (`fixture` | `manual`)
- `trigger_count`
- `settle_time_ms`
- `expected_transition` (close->open / open->close)

### D) Recovery policy
- `clear_policy` (auto-clear / explicit-clear / sticky-latch)
- `check_after_reinit` (true/false)

---

## Missing Conditions (do not hard-fail)

- No trigger method available -> `BLOCKED_FIXTURE` (or `BLOCKED_PARAMETER`)
- State encoding unclear -> `BLOCKED_REFERENCE`
- Channel mapping unclear -> `BLOCKED_REFERENCE`

---

## Step 0 — Probe Gate (Mandatory)

1. Initialize SUSI library
2. Probe all required CaseOpen channels
3. Validate readable status and state encoding config

Decision rules:
- Required channel unsupported -> `FAIL_CAPABILITY`
- No required channel available -> category `FAIL_CAPABILITY`

---

## Automated Test Flow

## Case A — Capability & Baseline Stability

Goal: ensure channel is readable and idle state is stable.

Steps:
1. Read each required channel once
2. Sample N times in idle condition
3. Check state stability (no random flips)

Pass criteria:
- Required channels readable
- Baseline stable per policy

Fail mapping:
- API read error -> `FAIL_API`
- random state flips without trigger -> `FAIL_FUNCTIONAL`

## Case B — Trigger Transition (Functional)

Goal: prove physical event is reflected by API state.

Steps:
1. Capture pre-trigger baseline
2. Apply trigger (fixture pulse or manual action)
3. Wait `settle_time_ms`
4. Read state transition
5. Repeat for configured `trigger_count`

Pass criteria:
- Transition direction matches expected
- Repeatability meets policy

Fail mapping:
- no state change despite trigger -> `FAIL_FUNCTIONAL`
- trigger unavailable -> `BLOCKED_FIXTURE`

## Case C — Clear/Restore Behavior

Goal: verify post-event state handling.

Steps:
1. After trigger, observe whether state auto-clears or latches
2. If explicit clear required, execute clear path and re-read
3. Verify behavior matches `clear_policy`

Pass criteria:
- Clear/restore behavior matches profile

Fail mapping:
- clear path mismatch -> `FAIL_FUNCTIONAL`
- clear API failure -> `FAIL_API`

## Case D — Recovery/Re-init

Goal: verify state read path survives recovery operations.

Steps:
1. SUSI re-init or driver reload (per policy)
2. Re-read channel state
3. Validate readability and encoding consistency

Pass criteria:
- channel remains readable and coherent

Fail mapping:
- channel lost after recovery -> `FAIL_RECOVERY`
- state corruption after recovery -> `FAIL_RECOVERY`

## Negative Cases

- invalid channel ID read
- unsupported channel read

Expected:
- proper unsupported/invalid status

---

## L1~L6 Mapping (HWM.CaseOpen)

- L1 Configuration: channel map/encoding/trigger policy complete
- L2 Capability: required channels exposed
- L3 API Command: read/clear API statuses
- L4 Read-back: baseline and post-trigger state coherence
- L5 Functional: physical trigger-to-state transition proof
- L6 Recovery: re-init/reload continuity

---

## Human Discussion Pack (for blocked/fail)

Include these in report for team discussion:
1. Trigger method status (fixture/manual)
2. State encoding source (BIOS/doc/firmware note)
3. Clear policy expectation vs observed behavior
4. Required support from HW/BIOS/EC team

---

## Output Schema (per case)

```json
{
  "case_id": "HWM.CaseOpen.OEM0.TRIGGER_TRANSITION",
  "category": "HWM.CaseOpen",
  "target": "OEM0",
  "parameters": {
    "trigger_mode": "manual",
    "trigger_count": 3,
    "settle_time_ms": 500
  },
  "actual": {
    "baseline_state": 0,
    "post_trigger_states": [1,1,1],
    "clear_state": 0
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
    "caseopen_series.csv",
    "caseopen_trigger_log.json",
    "caseopen_recovery_log.json"
  ],
  "suspected_layers": []
}
```

---

## Platform Verdict Contribution

HWM.CaseOpen verdict:
- `PASS`: required cases pass
- `CONDITIONAL`: API/readback pass, but functional trigger blocked
- `FAIL`: any required case fails
- `ABORTED`: recovery/safety framework aborted

Always keep `BLOCKED_*` explicit for follow-up.