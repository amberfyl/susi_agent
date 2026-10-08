# GPIO public-ID versus physical-pin contract

## Why this preflight exists

The generated `[GPIO]` INI has two independent identities:

- `GPIO<number>` key suffix: SUSI public GPIO API ID. Machine-B builds its bank/mask runner contract from this value and accepts only decimal `0..127`.
- Tuple fields 5 and 6 (zero-based fields 4 and 5): physical chip `group,pin`, derived from schematic evidence.

Do not derive the key by concatenating the physical values.

Example: a schematic-confirmed `GPIOD0` means physical group `13`, bit `0`. `GPIO130` is not valid: it is interpreted as public API ID 130 and is rejected. It also has no proven semantic relationship to the actual public API namespace.

## Key format (strict, generator-owned)

- Every key is `GPIO` + a two-digit decimal sequence number: `GPIO00`, `GPIO01`, `GPIO02`, ... in order (AnalysisSKill/orchestrator_skill.md 10.4 rule 3).
- The generator assigns the number from the signal order: `SIO_GPIOn` by `n`; `EC_P1_GPIO0..7` -> `GPIO00..07`, `EC_P2_GPIO0..7` -> `GPIO08..15`. The request form does not need to provide a logical mapping, and a form that only gives a GPIO count is sufficient.
- Agents never choose, rename, or renumber keys. Do not use the chip function label (`GPIOD0`, `GP42`) or the `report_name` you wrote in the trace JSON as a key.
- The decimal suffix must stay in `0..127`.

## What the agent decides

Only the physical `group,pin` (tuple fields 5 and 6) comes from the schematic trace:

1. Trace `external signal -> chip GPIO function label -> physical group,pin` from focused schematic evidence (R-016/R-017) and write the trace JSON.
2. Preserve the physical `group,pin` in the tuple; never alter it to make a key fit.
3. Ambiguous trace -> `GPIO_TRACE_AMBIGUOUS`, no `[GPIO]` output; do not guess.

## Preflight before Machine-B build

Validate that every INI key matches `GPIO\d{2}` with a decimal suffix in `0..127` and that numbering is sequential, before `build_machineB_section_configs.py`.

## Minimal validation command

```bash
<REPO>/.venv/bin/python <REPO>/build_machineB_section_configs.py \
  --matrix <REPO>/CASES/<PROJECT>/<PROJECT>-section-matrix.json \
  --output-dir <REPO>/CASES/<PROJECT>
```

Treat a `public ID must be in range 0..127` failure as a generation/configuration blocker, not as a reason to retry remote validation.
