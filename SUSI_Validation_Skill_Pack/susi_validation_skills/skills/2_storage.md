# Skill: Validate StorageArea — Automated Flow

## Scope

Validate `[StorageArea]` with an automation-first method using:
- PowerShell runner
- C# wrapper (P/Invoke to `Susi4.dll`)
- Strict backup/write/read-back/restore sequence
- Optional reboot persistence flow with checkpoint resume

This skill treats StorageArea as safety-sensitive. No approved writable range, no write test.

## Objective

Verify all required storage behaviors:
1. Declared storage area capability matches platform declaration
2. Read is stable and length-correct
3. Approved write can be read back exactly
4. Original bytes are restored exactly
5. Optional persistence behavior is validated when required
6. Any restore failure aborts the category

---

## Required Inputs

### A) Platform/Test metadata
- `platform_name`
- `bios_version` (optional but recommended)
- `ec_fw_version` (optional)
- `driver_version` / `dll_version` (optional)

### B) Storage target policy
- `area_id` (or area list)
- `area_declared_required` (true/false)
- `approved_offset`
- `approved_length`
- `patterns` (e.g. `AA55`, `55AA`, random-seed pattern)
- `allow_write` (true/false)
- `require_persistence_after_reboot` (true/false)

### C) Safety policy
- `max_write_attempts`
- `abort_on_restore_failure` (must be true in production flow)
- `allow_destructive_reboot` (for persistence verification)

### D) Resume mechanism (for persistence case)
- `checkpoint_file` (absolute path)
- `resume_script` or startup trigger

---

## Step 0 — Probe Gate (Mandatory)

Before any write/read-back test:
1. Initialize SUSI library
2. Query storage area capability (existence, size, attributes)
3. Validate test parameters against area boundaries

Decision rules:
- INI/profile declares required area but API says unsupported -> `FAIL_CAPABILITY`
- Area not required on this platform profile -> `N_A` or `UNSUPPORTED_PASS`
- Missing `approved_offset`/`approved_length` -> `BLOCKED_PARAMETER`
- Requested range out of boundary -> `BLOCKED_PARAMETER`

Do not run write test before gate passes.

---

## Automated Test Flow

## Case A — Capability & Boundary Discovery

Goal: prove area exists and legal boundaries are known.

Steps:
1. Query area by `area_id`
2. Record size and access capability (read/write)
3. Validate `approved_offset + approved_length <= area_size`
4. Validate `allow_write` policy against actual write capability

Pass criteria:
- Area query success
- Boundary check pass
- Access mode known

Fail mapping:
- unsupported/missing area -> `FAIL_CAPABILITY` (if required)
- API query error -> `FAIL_API`
- bad range -> `BLOCKED_PARAMETER`

## Case B — Baseline Read

Goal: verify stable readable bytes in approved range.

Steps:
1. Read bytes at approved range
2. Verify returned length equals requested length
3. Save exact bytes as `original_buffer`
4. Optionally read N times and compare consistency

Pass criteria:
- Read success
- Length match
- Original buffer archived

Fail mapping:
- read error -> `FAIL_API`
- length mismatch -> `FAIL_READBACK`

## Case C — Write / Read-back / Restore (Core)

Run only when `allow_write=true` and area supports write.

Goal: prove write correctness without leaving residue.

Steps:
1. Ensure `original_buffer` exists
2. For each approved pattern:
   - write pattern to approved range
   - read same range back
   - byte-compare expected vs actual
   - log raw hex
3. In `finally` block (always execute):
   - restore `original_buffer`
   - read back again
   - byte-compare restore result

Pass criteria:
- Every pattern read-back exactly matches
- Restore matches original exactly

Fail mapping:
- write call error -> `FAIL_API`
- pattern mismatch -> `FAIL_READBACK`
- restore mismatch/error -> `ABORTED_RESTORE_FAILURE`

## Case D — Persistence Across Reboot (Optional)

Run only when `require_persistence_after_reboot=true` and policy allows reboot.

Goal: verify expected persistence model across reboot/re-init.

Pre-steps:
1. Save checkpoint with case metadata and expected behavior
2. Save `original_buffer`

Execution:
1. Write approved temporary pattern
2. Trigger controlled reboot path
3. Resume runner on boot
4. Read target range
5. Compare with expected persistence behavior
6. Restore original bytes and verify again
7. Close checkpoint

Pass criteria:
- Resume succeeded with valid checkpoint chain
- Persistence behavior matches profile expectation
- Restore exact match after persistence test

Fail mapping:
- no resume evidence -> `FAIL_RECOVERY`
- checkpoint mismatch -> `FAIL_RECOVERY`
- persistence mismatch -> `FAIL_FUNCTIONAL`
- restore failure -> `ABORTED_RESTORE_FAILURE`

## Negative Cases

Run non-destructive invalid calls:
- invalid area_id
- offset out of range
- length 0
- range crossing boundary
- write when access mode is read-only

Expected:
- API rejects invalid requests with expected status

Fail mapping:
- invalid request unexpectedly succeeds -> `FAIL_API`

---

## L1~L6 Mapping (StorageArea)

- L1 Configuration: area/policy/offset/length/patterns are complete and parseable
- L2 Capability: area existence, size, access mode match declaration
- L3 API Command: query/read/write/restore call status codes
- L4 Read-back: byte-level compare for write and restore
- L5 Functional: persistence behavior (when required) and no unintended neighbor corruption
- L6 Recovery: reboot resume path and post-test restoration integrity

A case is PASS only when required layers for that case pass.

---

## Safety Rules (Hard)

1. Never invent writable offset; must be approved input
2. Always backup `original_buffer` before first write
3. Always restore in `finally` block
4. Any restore failure => stop category immediately
5. Cap write retries (`max_write_attempts`), never infinite loop
6. Do not run persistence reboot case without explicit policy enablement

---

## Output Schema (per case)

```json
{
  "case_id": "StorageArea.AREA0.WRITE_READBACK_RESTORE",
  "category": "StorageArea",
  "target": "AREA0",
  "parameters": {
    "offset": 1024,
    "length": 32,
    "patterns": ["AA55...", "55AA..."]
  },
  "api_calls": [
    {"name":"StorageQuery","status":"SUSI_STATUS_SUCCESS","ts":"..."},
    {"name":"StorageRead","status":"SUSI_STATUS_SUCCESS","ts":"..."},
    {"name":"StorageWrite","status":"SUSI_STATUS_SUCCESS","ts":"..."}
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
    "original_buffer.bin",
    "pattern_readback_log.json",
    "restore_verify_log.json",
    "checkpoint_storage_persistence.json"
  ],
  "suspected_layers": []
}
```

---

## Suggested Runner Order

1. Case A Capability/Boundary
2. Case B Baseline Read
3. Case C Write/Read-back/Restore
4. Case D Persistence/Reboot (if required)
5. Negative Cases
6. Final summary + verdict

---

## Platform Verdict Contribution

StorageArea category verdict:
- `PASS`: all required cases pass
- `CONDITIONAL`: no fail, but persistence case intentionally skipped by policy
- `FAIL`: any required case fails
- `ABORTED`: restore/recovery safety failed

Always report blocked/unsupported explicitly; do not collapse to generic fail.