# Platform Profile Schema

## Purpose

The INI identifies enabled SUSI functions, but it usually does not provide all information needed for safe and meaningful functional validation.

The platform profile supplies approved platform-specific parameters while keeping validator scripts unchanged.

Current pack status:
- Validation is section-based with fixed scripts.
- Profile/config inputs are single-active mapping per run (no candidate queue in this schema yet).

## Example

```yaml
platform:
  name: SOM-6833
  required_bios_patterns:
    - "68330000060X006"
    - "68330000060X007"
  minimum_ec_fw: "V01004404"

hwm:
  HWM_TEMP_CPU:
    min: 0
    max: 105
    samples: 10
    interval_ms: 500
    reference: bios
    tolerance: 5

  HWM_VOLTAGE_5V:
    min: 4750
    max: 5250
    samples: 5

storage:
  area0:
    writable: true
    safe_offset: 256
    length: 16
    reboot_persistence: true

wdt:
  timeout_seconds: 30
  refresh_interval_seconds: 10
  tolerance_seconds: 10
  destructive_reset_case: true

thermal_protect:
  channel0:
    writable: true
    allowed_min: 60
    allowed_max: 95
    test_delta: -2
    trigger_test: false

gpio:
  voltage_level: 3.3
  loopback_pairs:
    - output: 0
      input: 1
    - output: 2
      input: 3

vga:
  brightness_values: [0, 25, 50, 75, 100]
  settle_ms: 1500
  lux_fixture: false

i2c:
  channel0:
    slave_7bit: 0x48
    register: 0x0F
    register_width: 1
    read_length: 1
    expected_hex: "A1"
    write_allowed: false

smbus:
  channel0:
    slave_7bit: 0x50
    protocol: read_byte
    command: 0x00
    expected_hex: "92"

fan_control:
  FCPU:
    rpm_id: HWM_FAN_CPU
    values: [20, 40, 60, 80, 100]
    settle_seconds: 5
    minimum_total_rpm_change: 500
```

## Rules

- Profile values must be approved by QA or the responsible engineering team.
- Missing safe-write parameters must produce `BLOCKED_PARAMETER`.
- The AI may propose a profile draft but may not automatically approve it.
- Numeric ranges must indicate units.
- All writable items must define restore behavior.
- Destructive cases must be explicitly enabled.
