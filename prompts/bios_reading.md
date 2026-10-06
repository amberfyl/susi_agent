# BIOS image reading rules
<!-- py fallback only: loaded by susi_gen.py when the agent did not write the artifact.
     The agent's rules are AnalysisSKill/bios_circuit_image_analysis_rule.md R-020.
     This file is the English copy of those rules; change both together. -->

## Which page counts
- Only the Hardware Monitor / PC Health page (rows with live readings) is evidence for HWM sections.
- Other BIOS pages (CPU configuration, chipset/iManager configuration, main page with versions, ...) are not hardware-monitor evidence. For them answer only: `No live hardware-monitor sensor rows are visible.` Do not list sensor names in negative sentences (write neither "no Case Open item" nor "no fan rows").

## What to read
- Report only text and values that are visible in the image. Never guess or fill in missing items.
- Voltage rail labels (for example `+12V`, `+5V`, `+5VSB`, `+3.3V`, `+9V`, `VBAT`, `VCORE`).
- Temperature sensor names (for example `CPU Temperature`, `System Temperature`, `Chipset Temperature`).
- Fan names (for example `CPU FAN`, `System FAN`, `COM Module FAN`, `Carrier Board FAN`).
- Current/Ampere live readings and the live Case Open / Chassis Intrusion status, only when shown with a value or state.

## Real-time readings versus settings
- Use only rows that show a live reading (for example `CPU Temperature : 45 C`, `CPU FAN Speed : 2480 RPM`).
- Threshold and action settings are not sensors. Do not report them as sensor names: for example `CPU Shutdown Temperature`, `Warning Temperature`, `Throttle Temperature`, fan duty or target-temperature settings.

## Values
- Case Open / Chassis Intrusion counts only as a live status (for example `Open`, `Closed`, `Yes`, `No`, `OK`). A setting such as `Case Open Detection` or `Chassis Intrusion [Disabled]` is not a reading.
- When a live value is visible, report it with its unit: voltage in `V` or `mV`, temperature in `C`, fan speed in `RPM`.
- Copy the value as shown. If no value is visible, report the label without a value.
