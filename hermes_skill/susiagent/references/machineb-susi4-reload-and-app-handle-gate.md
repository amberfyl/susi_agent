# Machine-B SUSI4 driver reload and application-handle gate

Use this when a target behaves differently between Device Manager manual disable/enable and automated validation.

## Required order

1. Treat the runtime INI as immutable when the run is explicitly a validation-only rerun. Do not deploy/copy another INI merely to test reload behavior.
2. Before PnP reload, ensure user-mode SUSI applications are closed, especially `SusiDemo4.exe`. A process holding `ROOT\SYSTEM\0001` can block device removal/ejection.
3. Run the reload against the explicit instance ID when known: `ROOT\SYSTEM\0001`.
4. Require all three state observations:
   - before: `Status=OK`, `Problem=CM_PROB_NONE`
   - after disable: `Problem=CM_PROB_DISABLED` (`Status=Error` is expected while disabled)
   - after enable: `Status=OK`, `Problem=CM_PROB_NONE`
5. Only launch the SUSI application after the final enabled-state check passes.

## Failure interpretation

- `ERROR: Generic failure` immediately after `Before: Status=OK`, with no `After disable` line, means the disable operation itself failed; do not claim a reload occurred.
- Windows Kernel-PnP Event 225 stating that `SusiDemo4.exe` stopped removal/ejection is direct evidence of an open application handle. Correlate the event timestamp with the reload log before blaming the INI.
- If `After disable` exists but `After enable` is absent, the script may have left the device disabled. Reload helpers must use a cleanup/finally path that attempts enable after any post-disable exception, and the orchestrator must verify final PnP state instead of trusting a success message.
- Restore/final reload commands must not use unchecked execution (`check=False`) as the only success criterion. Capture command exit status and perform a fresh `Get-PnpDevice` state check.

## Reboot distinction

`Disable-PnpDevice` plus `Enable-PnpDevice` is a PnP device-stack reload, not an operating-system restart. The helper must not call `Restart-Computer`, `shutdown.exe`, or `Stop-Computer`. A planned restart attributed to `mmc.exe`/Device Manager is a separate path. A BugCheck 0x3B with `0xC0000094` followed by `AutoReboot=1` is a kernel crash/recovery path, not proof that the reload helper intentionally rebooted Windows. Disabling `AutoReboot` only leaves the system at the BSOD; it does not fix the driver fault.

## Safe validation boundary

Use this sequence for a clean reproduction:

`close SusiDemo4 -> (optional) deploy approved immutable/final INI -> PnP disable/enable -> verify Status=OK/Problem=0 -> wait briefly -> launch SusiDemo4`

If the application still triggers a BugCheck after this sequence, treat reload sequencing as excluded and investigate the SUSI driver/API call path and preserved minidumps. Do not rerun the application while collecting crash evidence.
