# Skill: Validate GPIO — Semi-Automated Flow

## Scope

Validate `[GPIO]` using:
- PowerShell runner
- C# wrapper (P/Invoke to `Susi4.dll`)
- Direction + output/input API verification
- Optional loopback/fixture functional validation
- Restore/recovery checks

GPIO can be highly automated at API level. Full functional PASS usually needs wiring (loopback or fixture).

## Objective

Verify all required GPIO behaviors:
1. Declared GPIO channels are exposed with correct capability bits
2. Direction set/get is coherent
3. Output set/get is coherent
4. Functional electrical response is correct (when loopback/fixture exists)
5. Channel mapping/isolation is correct
6. Original direction/level is restored safely

---

## Required Inputs

### A) Platform/Test metadata
- `platform_name`
- `bios_version` (optional)
- `ec_fw_version` (optional)
- `driver_version` / `dll_version` (optional)

### B) GPIO policy
- `required_gpio_list`
- `gpio_id_map` (logical pin->SUSI ID)
- `capability_policy` (input/output availability)
- `safe_output_allowed` (per pin)
- `settle_time_ms`

### C) Functional validation policy
- `functional_mode` (`none` | `loopback` | `fixture`)
- `loopback_pairs` (if loopback mode)
- `fixture_map` (if fixture mode)
- `expected_level_encoding` (high/low mapping)

### D) Safety policy
- `abort_on_restore_failure` (true)
- `pins_forbidden_to_drive` (power/reset/strap-critical pins)

---

## Missing Conditions (do not hard-fail)

- No loopback/fixture -> allow API-level pass, set L5 as `N_A`/`BLOCKED_FIXTURE`
- GPIO capability map unclear -> `BLOCKED_REFERENCE`
- Unsafe pin requested for drive -> `BLOCKED_PARAMETER`

---

## Step 0 — Probe Gate (Mandatory)

1. Initialize SUSI library
2. Probe required GPIO channels
3. Query capability (input/output support)
4. Validate requested tests against safety policy

Decision rules:
- Required GPIO unsupported -> `FAIL_CAPABILITY`
- Requested output on forbidden pin -> `BLOCKED_PARAMETER`
- Missing capability reference for required pin -> `BLOCKED_REFERENCE`

---

## Automated Test Flow

## Case A — Capability & Direction Set/Get

Goal: verify channel exposure and direction control path.

Steps:
1. For each required pin, read capability bits
2. Read original direction
3. If output supported and safe: set output, read-back direction
4. If input supported: set input, read-back direction
5. Restore original direction

Pass criteria:
- Capability matches profile
- Direction set/get coherent
- Restore success

Fail mapping:
- unsupported required pin -> `FAIL_CAPABILITY`
- set/get API error -> `FAIL_API`
- read-back mismatch -> `FAIL_READBACK`
- restore failure -> `ABORTED_RESTORE_FAILURE`

## Case B — Output Level Set/Get (API-Level)

Goal: verify output high/low control path and read-back coherence.

Steps:
1. Save original direction/level
2. Set pin output mode
3. Write sequence (0,1,0,1)
4. Read-back level after each write
5. Restore original level/direction in finally

Pass criteria:
- write/read-back coherent for sequence
- restore verified

Fail mapping:
- write/read API error -> `FAIL_API`
- read-back mismatch -> `FAIL_READBACK`
- restore failure -> `ABORTED_RESTORE_FAILURE`

Note:
- This proves API/FW state path, not guaranteed physical voltage at pad.

## Case C — Functional Loopback Validation (Optional)

Run when `functional_mode=loopback` and pair list exists.

Goal: prove driven output is observed at mapped input pin.

Steps:
1. Configure output pin A, input pin B
2. Drive A with sequence (0,1,0,1)
3. Read B after settle delay
4. Compare expected vs observed
5. Optionally swap A/B and repeat

Pass criteria:
- Input follows output for required transitions

Fail mapping:
- no pair definition -> `BLOCKED_REFERENCE`
- transitions mismatch -> `FAIL_FUNCTIONAL`

## Case D — Fixture Electrical Validation (Optional)

Run when `functional_mode=fixture`.

Goal: prove physical level correctness using independent measurement.

Steps:
1. Drive DUT output pin
2. Fixture measures electrical level
3. Fixture drives DUT input pin
4. DUT read matches fixture signal

Pass criteria:
- DUT commands and fixture measurements agree

Fail mapping:
- fixture unavailable -> `BLOCKED_FIXTURE`
- electrical mismatch -> `FAIL_FUNCTIONAL`

## Case E — Channel Isolation

Goal: ensure targeting one pin does not unintentionally alter others.

Steps:
1. Toggle target pin sequence
2. Sample non-target observed pins
3. Verify no correlated unintended toggling beyond threshold

Pass criteria:
- non-target pins remain stable

Fail mapping:
- unintended correlated changes -> `FAIL_FUNCTIONAL`

## Case F — Recovery/Re-init

Goal: verify GPIO path remains coherent after recovery operations.

Steps:
1. SUSI re-init or driver reload (policy)
2. Run short direction + level set/get sample
3. Restore original state

Pass criteria:
- path remains usable
- restore passes

Fail mapping:
- post-recovery breakage -> `FAIL_RECOVERY`

## Negative Cases

- invalid GPIO ID
- set direction not supported by capability
- output write on input-only pin

Expected:
- proper invalid/unsupported status

---

## L1~L6 Mapping (GPIO)

- L1 Configuration: pin map/capability/safety/functional mode complete
- L2 Capability: required pins exposed and capability aligned
- L3 API Command: direction/write/read statuses
- L4 Read-back: direction/level and restore coherence
- L5 Functional: loopback/fixture electrical proof
- L6 Recovery: re-init/reload continuity

---

## Human Discussion Pack (for blocked/fail)

Include in report:
1. Pin mapping source (schematic/board doc/BIOs note)
2. Safety exceptions (forbidden pins)
3. Functional method selected (none/loopback/fixture)
4. Required support from HW team (pair list, fixture map)
5. Decision: accept API-level pass only or require L5 functional pass

---

## Output Schema (per case)

```json
{
  "case_id": "GPIO.GPIO12.LOOPBACK_TOGGLE",
  "category": "GPIO",
  "target": "GPIO12",
  "parameters": {
    "sequence": [0,1,0,1],
    "settle_time_ms": 100,
    "functional_mode": "loopback"
  },
  "actual": {
    "direction_readback": ["out","out","out","out"],
    "output_readback": [0,1,0,1],
    "input_observed": [0,1,0,1]
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
    "gpio_setget_log.json",
    "gpio_loopback_series.csv",
    "gpio_recovery_log.json"
  ],
  "suspected_layers": []
}
```

---

## Platform Verdict Contribution

GPIO verdict:
- `PASS`: required cases pass
- `CONDITIONAL`: API/readback pass, but functional method unavailable
- `FAIL`: any required case fails
- `ABORTED`: restore/recovery safety failed

Always keep `BLOCKED_*` explicit for follow-up.