# Skill: Validate WDT (Watchdog) — Automated Flow

## Scope

Validate `[WDT]` with an automation-first method using:
- PowerShell runner
- C# wrapper (P/Invoke to `Susi4.dll`)
- Checkpoint-based reboot resume

This skill is for real functional verification, not API smoke test only.

## Objective

Verify all required watchdog behaviors:
1. Capability and parameter range are correct
2. Start/Stop works (no unintended reset)
3. Refresh/KeepAlive works (prevents reset)
4. Timeout reset actually happens when refresh is stopped
5. Post-reboot resume can prove the reset came from WDT scenario
6. Cleanup and recovery are safe and repeatable

---

## Required Inputs

### A) Platform/Test metadata
- `platform_name`
- `bios_version` (optional but recommended)
- `ec_fw_version` (optional)
- `driver_version` / `dll_version` (optional)

### B) WDT policy
- `wdt_channel` (or channel list)
- `timeout_min`, `timeout_max` (if discoverable from API, can auto-fill)
- `test_timeout_sec` (must be in legal range)
- `refresh_interval_sec` (must be < timeout)
- `timeout_tolerance_sec` (expected reset tolerance)
- `allow_destructive_reset` (true/false)

### C) Resume mechanism
- `checkpoint_file` (absolute path)
- `resume_script` or startup trigger to continue after reboot

### D) Safety policy
- `max_reset_attempts`
- `abort_on_checkpoint_mismatch` (true/false)
- `abort_on_restore_failure` (true/false)

---

## Step 0 — Probe Gate (Mandatory)

Before running WDT cases:
1. Initialize SUSI library
2. Query WDT capability/channel support via wrapper APIs
3. If INI declares WDT but capability is unsupported:
   - result: `FAIL_CAPABILITY` (required case)
4. If WDT is not required on this platform profile:
   - result: `N_A` or `UNSUPPORTED_PASS` (per project policy)

Do not run start/refresh/reset tests before capability gate passes.

---

## Automated Test Flow

## Case A — Capability & Range Discovery

Goal: prove API can expose WDT channel and legal parameter range.

Steps:
1. Query supported WDT channel(s)
2. Verify `wdt_channel` is supported
3. Query/get legal timeout range and unit
4. Validate `test_timeout_sec` and `refresh_interval_sec`

Pass criteria:
- Channel is supported
- Timeout range valid
- Test parameters valid

Fail mapping:
- unsupported channel -> `FAIL_CAPABILITY`
- range query/API error -> `FAIL_API`
- input out of range -> `BLOCKED_PARAMETER`

## Case B — Start/Stop (No Reset Expected)

Goal: confirm watchdog can start then stop safely.

Steps:
1. Ensure clean init state
2. Start WDT with `test_timeout_sec`
3. Wait a short interval (e.g. timeout/3)
4. Stop WDT
5. Wait longer than timeout (e.g. timeout + margin)
6. Confirm system remains alive and script continues

Pass criteria:
- Start success
- Stop success
- No reboot happened

Fail mapping:
- start/stop call fail -> `FAIL_API`
- reboot still happened after stop -> `FAIL_FUNCTIONAL`

## Case C — Refresh Loop (No Reset Expected)

Goal: prove keepalive/refresh prevents timeout reset.

Steps:
1. Start WDT with `test_timeout_sec`
2. For >= 3 timeout windows:
   - sleep `refresh_interval_sec`
   - call refresh API
   - log each refresh timestamp/status
3. Stop WDT
4. Confirm no reboot during the loop

Pass criteria:
- All refresh calls succeed
- No reboot during test window

Fail mapping:
- refresh error -> `FAIL_API`
- reset despite valid refresh loop -> `FAIL_FUNCTIONAL`

## Case D — Timeout Reset + Reboot Resume (Destructive)

Run only when `allow_destructive_reset=true`.

Goal: prove real watchdog timeout reset and recover evidence after reboot.

Pre-steps:
1. Write checkpoint file with:
   - `case_id`
   - `phase=before_timeout`
   - `start_timestamp`
   - `expected_timeout_sec`
2. Register resume script/task (if not persistent already)

Execution:
1. Start WDT with `test_timeout_sec`
2. Do not refresh
3. Wait for reset

Post-boot resume logic:
1. Runner restarts and reads checkpoint
2. Verify `phase=before_timeout` exists
3. Record `resume_timestamp`
4. Compute elapsed time from checkpoint to resume
5. Optionally read reset reason/event registers if available
6. Mark checkpoint consumed/closed

Pass criteria:
- System reboot observed
- Resume reached automatically
- Elapsed time within tolerance
- Checkpoint chain consistent

Fail mapping:
- no reset -> `FAIL_FUNCTIONAL`
- reset happened but no resume proof -> `FAIL_RECOVERY`
- checkpoint mismatch -> `FAIL_RECOVERY`

## Negative Cases (API Robustness)

Run non-destructive invalid calls:
- timeout < min
- timeout > max
- invalid channel
- refresh before start
- stop before start

Expected result:
- API must reject invalid operations with expected status

Fail mapping:
- invalid input accepted silently -> `FAIL_API`

---

## L1~L6 Mapping (WDT)

- L1 Configuration: INI/profile/parameters present and parseable
- L2 Capability: channel/range support matches declaration
- L3 API Command: start/stop/refresh/time-setting call status
- L4 Read-back: parameter echo/read-back where API provides it
- L5 Functional: real reset happens (or does not happen when it should not)
- L6 Recovery: reboot resume/checkpoint chain/re-init success

A case is PASS only when all required layers for that case pass.

---

## Safety Rules (Hard)

1. Destructive reset case requires explicit opt-in (`allow_destructive_reset=true`)
2. Max reset retries must be bounded (`max_reset_attempts`)
3. Every destructive run must create checkpoint before start
4. If checkpoint pipeline is broken, abort destructive case
5. Never loop infinite reset attempts

---

## Output Schema (per case)

```json
{
  "case_id": "WDT.CH0.TIMEOUT_RESET",
  "category": "WDT",
  "parameters": {
    "channel": 0,
    "timeout_sec": 30,
    "refresh_interval_sec": 5
  },
  "api_calls": [
    {"name":"WdtStart","status":"SUSI_STATUS_SUCCESS","ts":"..."},
    {"name":"WdtRefresh","status":"SUSI_STATUS_SUCCESS","ts":"..."}
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
  "restore_result": "PASS",
  "evidence": [
    "checkpoint_before_timeout.json",
    "resume_log.txt",
    "wdt_case_log.json"
  ],
  "suspected_layers": []
}
```

---

## Suggested Runner Order

1. Case A Capability/Range
2. Case B Start/Stop
3. Case C Refresh Loop
4. Case D Timeout Reset+Resume (if approved)
5. Negative Cases
6. Final summary + verdict

---

## Platform Verdict Contribution

WDT category verdict:
- `PASS`: required cases all pass
- `CONDITIONAL`: no failure, but destructive case intentionally skipped by policy
- `FAIL`: any required case fails
- `ABORTED`: safety/recovery framework failed (e.g., checkpoint corruption)

Use explicit state, never hide skipped/blocked conditions.