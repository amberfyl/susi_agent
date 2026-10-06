# Machine-B SMBus legacy fixture validation

Use this reference when comparing or porting the legacy QA `smb.c` fixture test into `run_smbus_validation.ps1`.

## What the three addresses mean

SUSI SMBus APIs take an **encoded 7-bit address** (`7-bit << 1`):

- `0xAC` => 7-bit `0x56`: primary legacy QA fixture profile
- `0xAE` => 7-bit `0x57`: alternate primary fixture profile
- `0x4A` => 7-bit `0x25`: fallback fixture profile used when the `0xA0..0xAE` range is protected

These values are fixture slave addresses. They are not INI channel values and not SUSI public SMBus host IDs.

## Safety and invocation contract

- Keep fixture transactions behind an explicit runner switch such as `-EnableFixtureTest`.
- Without the switch, perform only L1-L4 configuration/capability/API-mask checks and report `PASS_SW` plus `PENDING_DQA`/`CONDITIONAL`.
- The fixture path is destructive by design: it issues WriteQuick and writes known byte/word/block patterns. Never run it against an unknown device or merely because a device ACKs at one of these addresses.
- Keep all runner console text and report diagnostics in plain English/ASCII for target-machine readability. Before transaction execution, print a visible heading such as `SMBus FIXTURE VALIDATION`, the three encoded/7-bit addresses, and a write-warning.
- A fixture FAIL means: fixture may be absent, address/profile may not match, wiring may be wrong, or the transaction/API may have failed. Do not state that FAIL uniquely proves “fixture not connected.”
- Decode probe failures in the report and summary when possible: `0xFFFFFBFF` is `SUSI_STATUS_NOT_FOUND`; `0xFFFFFBFB` is `SUSI_STATUS_NOACK` in current SUSI headers. Both indicate that the selected host/address did not complete an ACKed fixture probe, not a PowerShell parser failure.
- Keep fixture outcome separate from SW verdict: use `PASS_FIXTURE`/`FAIL_FIXTURE` in L5/report detail; CI exit code remains derived from L1-L4 `sw_verdict`.

## Legacy protocol profiles

For `0xAC`/`0xAE`, the legacy QA path exercises:

1. Write/Read Byte Data at command `0x01` with `0x11`
2. Write/Read Word Data with `0x1234`
3. Send/Receive Byte with fixture-specific expected responses (`0x34`, then `0x12`)
4. Write/Read Block with bytes `00..09`
5. I2C Block write/read; legacy code tolerates `SUSI_STATUS_UNSUPPORTED`

For `0x4A`, the legacy fallback profile is smaller:

1. Write Byte Data (`Cmd=0x01`, `Data=0x11`) and expect the fixture-specific read value `0x01`
2. Send/Receive Byte `0x05`

Record every API status and comparison in timestamped JSON; also print one concise line per Bus ID/address.

## Host-ID separation

Keep three namespaces separate:

- INI tuple/internal channel
- SUSI public host ID: External=`0`, OEM0=`1`, OEM1=`2`, OEM2=`3`, OEM3=`4`
- capability bits from `SUSI_ID_SMBUS_SUPPORTED`: bit number equals public host ID

For Intel generation, Channel1 remains External. Channel2+ public mappings come from full-probe `SMBUS_OEMn ... EXISTS`; do not infer public host ID from the encoded INI tuple field.

## Windows PowerShell deployment gate

- Prefer English/ASCII-only runner output. This avoids Windows PowerShell 5 legacy code-page corruption and keeps target-machine logs readable without requiring a BOM conversion step.
- Parse `common_susi.ps1` and the runner remotely with `System.Management.Automation.Language.Parser::ParseFile` before execution.
- With Windows OpenSSH, `powershell -File 'C:\path\script.ps1'` can acquire literal extra quotes through the SSH shell and fail with “path format is not supported.” Prefer `powershell -NoProfile -ExecutionPolicy Bypass -Command "& 'C:\path\script.ps1' ...; exit $LASTEXITCODE"`, with the dollar sign escaped by the calling POSIX shell.
- Back up the target runner/helper before replacing the normal deployment copy, then compare local and remote SHA-256 values.

Do not combine encoding conversion, SCP, remote directory writes, and execution into one approval-gated shell command. Perform one state-changing action at a time, wait for approval, then verify the result.

## Verification order

1. Update contract/unit tests first to require English fixture headings and failure explanations, then confirm they fail against the old runner.
2. Patch the runner and verify it contains no non-ASCII text; run the focused test and the full local test suite.
3. Copy the runner/helper into an isolated target test directory and compare hashes.
4. Parse `common_susi.ps1` and the runner on the Windows target.
5. Run without `-EnableFixtureTest` first and verify the safe SW-only report when the user has not explicitly approved fixture writes.
6. With current-turn approval and the approved fixture physically connected, run with `-EnableFixtureTest`.
7. Pull and inspect the new timestamped JSON; never cite an older report as the new fixture result.
8. Interpret the result by layer: an overall `FAIL_FIXTURE` may still correctly yield `sw_verdict=PASS_SW`, `dqa_verdict=FAIL_DQA`, and process exit code `0`, because CI exit status is intentionally based only on L1-L4 software validation.
9. If every WriteQuick probe returns NOT_FOUND/NOACK, report that no fixture address ACKed during this run. Do not blame the PowerShell implementation unless a same-time legacy-tool run with the same DLL, wiring, power, connector, and host ID succeeds.
