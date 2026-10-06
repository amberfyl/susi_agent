# Machine-B current-runtime read-only diagnostics

## Purpose

Use this mode when the user asks to “just run the PS1 and see the current result” while explicitly forbidding INI deployment and driver reload. This is a diagnostic of the **currently active runtime**, not formal validation of a newly generated section INI.

## Hard scope boundary

For this mode:

- Do not copy, overwrite, merge, back up, restore, or otherwise touch the live SUSI INI.
- Do not reload/restart the SUSI driver, service, device, or OS.
- Do not pass functional/write switches such as `-EnableFunctionalTest`, `-EnableWriteTest`, or fixture gates.
- Upload only the runner, its JSON, and the matching `common_susi.ps1` into an isolated test directory.
- Pass the existing runtime INI explicitly with `-IniPath`.
- Record the runtime INI SHA-256 before and after; require equality.

## Generated JSON mismatch handling

A generated JSON can describe the intended section while the live runtime INI still contains an older or smaller channel set. Keep these two questions separate:

1. **Does the generated JSON match the live INI?**
   - Run the generated JSON only if the user wants configuration-drift evidence.
   - A missing channel is `FAIL_CONFIG`/L1 failure and normally prevents API calls.
2. **What do the currently active APIs return?**
   - Create a clearly labelled temporary JSON outside project source, scoped to the exact live INI channels/tuples.
   - Mark it `TEMPORARY_TARGET_CURRENT_INI_NO_DEPLOY` (or equivalent).
   - Do not overwrite the builder-produced project JSON.
   - Do not report this temporary run as proof that the intended/generated configuration is correct.

This split avoids misdiagnosing an L1 channel mismatch as an API failure while preserving the intended configuration as the formal source of truth.

## Execution sequence

1. Read the live section and hash the runtime INI.
2. Inspect runner gates and confirm no Set/Write path can run without an omitted explicit switch.
3. If needed, create a temporary current-runtime JSON containing only live channels and exact tuples.
4. Upload files one at a time to an isolated target directory and verify local/remote hashes.
5. Run read-only with explicit `-ConfigPath`, `-IniPath`, and `-OutDir`.
6. Pull the newest timestamped report.
7. Verify the runtime INI hash is unchanged.
8. Report API status separately from functional/DQA status.

## Backlight/Brightness interpretation

- Omit `-EnableFunctionalTest`; verify report field `functional_test.attempted=false`.
- `VgaGetBacklightEnable` returning `0xFFFFFCFF` means the current runtime reports the API as unsupported. Any accompanying output value is undefined and must not be interpreted.
- `VgaGetBacklightBrightness` success with value `0` proves only that the read API succeeded and returned zero. It does not prove visible brightness control until an explicitly approved reversible functional test is run.
- Keep Backlight ON/OFF and Brightness verdicts separate; one may be unsupported while the other succeeds.

## I2C interpretation

- If intended JSON requires more channels than the live INI, generated-config execution may fail at L1 before any API call.
- A temporary live-channel JSON can then test current capability mask, caps, and frequency without deploying INI or running fixture transactions.
- Keep fixture addresses/transactions pending unless explicitly approved.

## Reporting language

State prominently that this is a current-runtime observation. Include:

- whether INI/reload/write actions were skipped;
- pre/post INI hash equality;
- exact API name, API ID, status code, and valid returned value;
- `functional_test.attempted`;
- the target report path.
