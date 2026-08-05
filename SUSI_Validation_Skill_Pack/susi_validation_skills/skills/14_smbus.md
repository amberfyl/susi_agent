# Skill: Validate SMBus — Semi-Automated Flow

## Scope

Validate `[SMBus]` using:
- PowerShell runner
- C# wrapper (P/Invoke to `Susi4.dll`)
- Capability gate + protocol transaction tests
- Optional approved write/read-back/restore
- PEC/error/recovery checks

SMBus full validation is usually harder than I2C due to protocol variants, byte order, block rules, and PEC behavior.

## Objective

Verify all required SMBus behaviors:
1. Declared SMBus channels are exposed by capability
2. Required protocol transactions succeed with correct data format
3. Byte order/block length handling is correct
4. Approved write path (if allowed) supports read-back and restore
5. PEC behavior (if required) is correct
6. Bus recovers after error scenarios

---

## Required Inputs

### A) Platform/Test metadata
- `platform_name`
- `bios_version` (optional)
- `driver_version` / `dll_version` (optional)

### B) Channel policy
- `required_smbus_channels`
- `smbus_channel_map`

### C) Golden protocol profile (per channel)
- `slave_addr_7bit`
- `protocol_type` (receive byte/read byte/read word/block read/process call...)
- `command_code`
- `expected_value` or `expected_range`
- `expected_length` (for block)
- `word_byte_order` (LSB/MSB policy)
- `pec_required` (true/false)

### D) Optional write policy
- `allow_write` (true/false)
- `approved_writable_command`
- `write_mask` (if bit-limited)
- `restore_required` (true)

### E) Timing/recovery policy
- `timeout_ms`
- `retry_count`
- `bus_recovery_method`

---

## Missing Conditions (do not hard-fail)

- Missing golden protocol profile -> `BLOCKED_REFERENCE`
- No PEC-capable fixture while PEC required -> `BLOCKED_FIXTURE`
- Write requested but no approved writable command -> `BLOCKED_PARAMETER`

---

## Step 0 — Probe Gate (Mandatory)

1. Initialize SUSI library
2. Query SMBus capability mask
3. Validate required channels are supported

Decision rules:
- required channel unsupported -> `FAIL_CAPABILITY`
- channel supported but no golden profile -> `BLOCKED_REFERENCE`

---

## Automated Test Flow

## Case A — Capability & Channel Mapping

Goal: verify declared channels are available.

Steps:
1. Read SMBus capability mask
2. Compare to required channel list
3. Log supported/unsupported channels

Pass criteria:
- all required channels supported

Fail mapping:
- required channel unsupported -> `FAIL_CAPABILITY`
- capability API error -> `FAIL_API`

## Case B — Protocol Transaction Validation (Core)

Goal: verify each required protocol path with known-good transaction.

Steps:
1. Execute configured transaction by protocol type
2. Validate status code
3. Validate returned length (if applicable)
4. Validate value/range and word byte order
5. Repeat N times for stability

Pass criteria:
- status/length/value/byte-order checks pass

Fail mapping:
- transaction error -> `FAIL_API`
- length mismatch -> `FAIL_READBACK`
- value/order mismatch -> `FAIL_FUNCTIONAL`

## Case C — Approved Write / Read-back / Restore (Optional)

Run only when `allow_write=true`.

Goal: verify safe write path and no residual modification.

Steps:
1. Read original command value
2. Modify only allowed bits/fields
3. Write test value
4. Read back and compare
5. In finally: restore original and verify

Pass criteria:
- write/read-back coherent
- restore verified

Fail mapping:
- write/read error -> `FAIL_API`
- mismatch -> `FAIL_READBACK`
- restore failure -> `ABORTED_RESTORE_FAILURE`

## Case D — PEC Validation (Optional/Required by profile)

Goal: verify Packet Error Code behavior.

Steps:
1. Run valid PEC transaction (expect success)
2. Run invalid PEC scenario via fixture/inject path (expect reject)
3. Verify status mapping

Pass criteria:
- valid PEC accepted
- invalid PEC rejected

Fail mapping:
- PEC required but not testable -> `BLOCKED_FIXTURE`
- behavior incorrect -> `FAIL_FUNCTIONAL`

## Case E — Error Handling & Recovery

Goal: verify robustness and post-error usability.

Negative scenarios:
- invalid channel
- NACK address
- invalid command
- invalid block length
- timeout

Steps:
1. Execute negative calls
2. Verify expected statuses
3. Run known-good transaction after errors

Pass criteria:
- invalid operations rejected correctly
- bus remains usable

Fail mapping:
- invalid call unexpectedly success -> `FAIL_API`
- bus stuck/unusable after error -> `FAIL_RECOVERY`

## Case F — Stress (Optional)

Goal: reliability under repeated protocol calls.

Steps:
1. Repeat known-good transaction M times
2. Track timeout/error/mismatch rates

Pass criteria:
- reliability metrics above threshold

Fail mapping:
- excessive failures -> `FAIL_FUNCTIONAL` or `FAIL_RECOVERY`

---

## L1~L6 Mapping (SMBus)

- L1 Configuration: channel/protocol profile/PEC policy complete
- L2 Capability: required channels exposed
- L3 API Command: protocol/write/read statuses
- L4 Read-back: length/value/byte-order/restore consistency
- L5 Functional: protocol semantics + PEC correctness
- L6 Recovery: post-error bus usability and continuity

---

## Human Discussion Pack (for blocked/fail)

Include:
1. Missing golden protocol details (addr/protocol/cmd/len/expected)
2. Byte-order expectation source
3. PEC requirement and testability status
4. Writable command approval status
5. Recovery support from FW/driver team

---

## Output Schema (per case)

```json
{
  "case_id": "SMBus.CH0.READ_WORD_ENDIAN",
  "category": "SMBus",
  "target": "CH0",
  "parameters": {
    "protocol_type": "read_word",
    "slave_addr_7bit": 22,
    "command_code": 9,
    "word_byte_order": "LSB_FIRST"
  },
  "actual": {
    "raw_bytes": [52, 18],
    "decoded_word": 4660,
    "status": "SUSI_STATUS_SUCCESS"
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
    "smbus_protocol_log.json",
    "smbus_pec_log.json",
    "smbus_recovery_log.json"
  ],
  "suspected_layers": []
}
```

---

## Platform Verdict Contribution

SMBus verdict:
- `PASS`: required cases pass
- `CONDITIONAL`: capability/API pass, but missing PEC/golden profile for full proof
- `FAIL`: any required case fails
- `ABORTED`: restore/recovery safety failed

Always separate `BLOCKED_REFERENCE`/`BLOCKED_FIXTURE` from `FAIL`.