# SUSI Validation Skill Pack

This package contains:

- `SKILL.md`: master orchestration skill (section-based workflow, L1~L6 rules)
- `skills/*.md`: section validation skills (current set: 14 sections)
- `schemas/api-wrapper-contract.md`: stable JSON interface for the PowerShell wrapper
- `schemas/platform-profile.md`: platform-specific parameter schema

Current section set:

1. WDT
2. StorageArea
3. ThermalProtect
4. HWM.Temperature
5. HWM.Voltage
6. HWM.Fan
7. HWM.Current
8. HWM.Fan.Control
9. HWM.CaseOpen
10. VGA.Brightness
11. VGA.Backlight
12. GPIO
13. I2C
14. SMBus

Note:
- Current framework is section-based with fixed scripts and per-section configs.
- Multi-candidate config queue is not included in this pack yet.
