# Machine-B runtime-state and cross-tool diagnosis

## Scope
Use this when a vendor C validation tool passes but a PowerShell Machine-B runner fails, or when an older report disagrees with a current rerun.

## Comparison procedure
1. Compare actual API calls and sequence, not only final PASS/FAIL:
   - capability/read-only calls
   - mutating calls actually issued
   - restore/recovery calls
2. Keep three identifiers separate:
   - public SUSI API ID used by `Susi*` calls
   - encoded INI tuple channel
   - DB `channel_id`/hardware tuple
   A matching or failing tuple does not by itself prove the public API ID is wrong.
3. Compare runtime state at each run:
   - resolved runtime INI path and hash
   - required section present in the deployed INI
   - whether the driver/runtime was reloaded after deployment
   - target, DLL, architecture, and timestamp
4. Rerun the same runner against the current runtime state and pull the newly timestamped report. Never use an old failure report as evidence for a corrected deployment.
5. Diagnose from call-level evidence:
   - `SUSI_STATUS_UNSUPPORTED` on capability/read before any write usually means the section/API was not registered in the active runtime, or runtime state differs; it is not enough to declare the JSON mapping wrong.
   - A successful current rerun with the same public API ID but a different active INI/runtime state isolates the earlier failure to deployment/runtime activation.

## StorageArea example and guard
- `SUSI_ID_STORAGE_STD` is public API ID `0x00000000`; an INI tuple such as `0x80000000` is a separate encoded configuration channel.
- A C tool may test `Write -> Lock -> protected Write -> Unlock -> Write`, while the safe PowerShell runner tests `GetCaps -> Read original -> Write pattern -> verify -> restore -> verify restore`. These are not equivalent tests.
- Report `write_requested` separately from `write_executed`. `-EnableWriteTest` or JSON enablement proves intent only; actual execution requires a `StorageAreaWrite` call in `api_calls`.
- If the initial read fails, no write should occur. Classify the failure at API/runtime layer rather than `FAIL_FUNCTIONAL`.

## SMBus capability-only guard
- Repeated success from `SUSI_ID_SMBUS_SUPPORTED` validates capability-mask stability only; it does not validate a real SMBus transaction.
- If transactions require an approved slave/register fixture, use `PASS_SW` plus `CONDITIONAL/PENDING_DQA` rather than claiming end-to-end SMBus PASS.
- Validate each generated channel against its intended mask bit independently. If Channel1 and Channel2 are both checked against bit 0 while the mask is `0x3`, flag the builder/runner mapping for review; otherwise Channel2 can appear supported without testing bit 1.

## No-INI-deployment differential test
Use this mode when the user explicitly says not to modify the target runtime INI.

1. Read and record the runtime INI SHA-256 before testing.
2. Upload only the section runner, generated JSON, and required shared wrapper (for example `common_susi.ps1`) into an isolated test directory.
3. Do not copy, back up, restore, reload, or otherwise touch the runtime INI/driver. Pass the existing runtime INI path explicitly to the runner.
4. Run the canonical generated JSON first:
   - If it fails L1 because a required channel is absent, this proves generated-config/runtime-INI mismatch only.
   - Confirm `api_calls` is empty before attributing the result to SUSI API behavior.
5. To isolate API health without deploying a new INI, create a clearly labeled temporary JSON that mirrors only the channels actually present in the current runtime INI. Keep this outside canonical project outputs and mark it as target-current/no-deploy evidence.
6. Run the same PS1 against the temporary JSON and existing runtime INI:
   - A L1-L4 pass isolates the original failure to config/runtime mismatch.
   - Fixture-dependent transactions remain `PENDING_DQA`; do not call capability/GetCaps/GetFrequency success an end-to-end fixture pass.
7. Read the new timestamped reports and verify the runtime INI SHA-256 again. Report both hashes.

Observed diagnostic pattern for I2C:
- Canonical JSON required Channel1..Channel4 while the active INI contained only Channel1, producing `FAIL_CONFIG` before any API call.
- A target-current Channel1-only JSON then showed a stable supported mask of `0x1`, successful `SusiI2CGetCaps`, and successful `SusiI2CGetFrequency`.
- Correct conclusion: the runner/API path works for the active Channel1; the canonical generated configuration is not active on the target. This does not prove Channels2..4 are unsupported under a correctly deployed configuration.
