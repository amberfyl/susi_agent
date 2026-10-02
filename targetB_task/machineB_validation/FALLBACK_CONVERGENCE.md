# Machine-B fallback convergence

This workflow runs only after the first full-INI validation report has been collected.

## Responsibility split

- AI reads the baseline summary and raw section reports, excludes partial/fixture/infrastructure cases, and writes `fallback-plan.json`.
- Python validates hashes and the candidate whitelist, mutates one section per attempt, deploys, reloads, invokes one existing PowerShell section runner, stops, restores, records evidence, and converges formal artifacts.
- PowerShell continues to own SUSI API validation for one section. No fallback candidate logic belongs in a runner.

## Trigger contract

Fallback is eligible only when all are true:

1. The generated matrix and baseline full INI say the section exists.
2. The section runner completed and produced a fresh JSON report.
3. API layer verdict is failed.
4. All reported channels failed and zero channels succeeded.
5. The reason is not fixture, capability-only, SSH, reload, stale/missing report, or another infrastructure failure.

`PARTIAL_FAIL` never triggers fallback. Any successful channel is sufficient to reject the trigger and sufficient to accept an attempted route.

## Candidate contract

`fallback_candidate_registry.json` is the only candidate source. Python preserves registry order, removes the baseline and numeric duplicates, and rejects invented/reordered values.

Version 1 changes only tuple field 3 (`IOPort/Address`). It must not change key, HWID, channel ID, Option, Name, or any other tuple field. SMBus `Channel1` is immutable. Option candidates are documented but disabled.

## Attempt lifecycle

For each planned section:

1. Start from the last accepted full INI.
2. Change only the planned section's route field.
3. Upload and verify candidate SHA-256.
4. Replace runtime INI, reload the driver, and verify readiness.
5. Run exactly that section's existing safe-default runner.
6. Pull a fresh report and preserve API/channel status evidence.
7. Stop on the first report containing any successful channel.
8. If all candidates fail, restore the last accepted full INI and reload.
9. On infrastructure failure, stop that convergence session and restore.

After the session, always restore the Machine-B runtime INI backup created by staging.

## Formal convergence

A successful route is merged into `CASES/<PROJECT>/<PROJECT>-config-overrides.json`. The generator applies this project override to both full and split INIs, adds `PROJECT_ROUTE_OVERRIDE` provenance to the matrix, and the section config builder regenerates JSON. Failed candidates are never persisted.

## CLI

The AI-created plan is executed with:

```text
python3 run_machineB_full_validation.py \
  --project <PROJECT> \
  --repo-root <REPO> \
  --run-id <BASELINE_RUN_ID> \
  --converge \
  --fallback-plan <fallback-plan.json> \
  --host <HOST> \
  --user <USER>
```

The plan's project, run ID, full-INI SHA-256, baseline summary SHA-256, per-section report SHA-256, trigger code, and exact registry candidate order are checked before target access.

## Evidence outputs

- `validation_runs/<run-id>/fallback/<section>/attempt-NNN/candidate-full.ini`
- per-attempt targeted reports under the same attempt directory
- `fallback-effective-full.ini`
- `fallback-convergence.json`
- project-scoped config override for successful sections

Legacy `tools/hwm_temperature_option_fallback.py` is not called by the generator and is not part of this workflow.
