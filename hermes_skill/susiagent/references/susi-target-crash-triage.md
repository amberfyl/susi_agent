# SUSI target crash triage after validation

When a target appears to reboot immediately after `SusiDemo4` or a Machine-B validation run:

1. Stop reproduction. Do not relaunch the GUI, reload SUSI4, run WDT PWRCYCLE/timeout, or run ThermalProtect SetConfig until evidence is preserved.
2. Use SSH/WinRM read-only collection to capture System events 1074, 41, 6008, 1001 and WER/minidump paths.
3. Distinguish a planned `mmc.exe` hardware-install/driver-reload restart (Event 1074) from a BugCheck reboot (Event 1001 + Event 41). Never label both as a SUSI power-setting change.
4. For `0x3B` with parameter `0xC0000094`, classify as a kernel divide-by-zero candidate. If the dump contains `SusiDemo4.exe`, this is evidence of trigger correlation only; require dump-stack analysis before naming the faulty SUSI.sys function.
5. Record `CrashControl\AutoReboot`; `1` explains the immediate reboot appearance but is not root cause.
6. Correlate runtime INI deployment and validation logs by exact timestamps. A changed `C:\Windows\SUSI\<project>.ini` is runtime configuration evidence, not proof that Windows power policy was changed.
7. Audit destructive gates in the actual runner code. WDT `SusiWDogStart`/timeout/PWRCYCLE and ThermalProtect `SetConfig`/shutdown may be present in comments or configs but must not be blamed unless the branch was reachable and the report proves execution.

Preserve exact event text, dump filenames, runner paths, INI timestamps, and driver state in the report. See the related SSH read-only crash-diagnosis procedure in the `remote-kvm-operation` skill.
