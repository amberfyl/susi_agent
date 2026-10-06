# GPIO public-ID versus physical-pin contract

## Why this preflight exists

The generated `[GPIO]` INI has two independent identities:

- `GPIO<number>` key suffix: SUSI public GPIO API ID. Machine-B builds its bank/mask runner contract from this value and accepts only decimal `0..127`.
- Tuple fields 5 and 6 (zero-based fields 4 and 5): physical chip `group,pin`, derived from schematic evidence.

Do not derive the public ID by concatenating the physical values.

Example: a schematic-confirmed `GPIOD0` means physical group `13`, bit `0`. `GPIO130` is not valid: it is interpreted as public API ID 130 and is rejected. It also has no proven semantic relationship to the actual public API namespace.

## Required evidence before Machine-B build

1. Trace `external signal -> chip GPIO function label -> physical group,pin` from focused schematic evidence.
2. Separately obtain a logical/public mapping from the request form, board API documentation, or an explicit user decision.
3. Validate every INI key is `GPIO<number>` and its decimal suffix is in `0..127` before `build_machineB_section_configs.py`.
4. Preserve the physical `group,pin` in the tuple; do not alter it to make the public ID fit.

## Incomplete logical mapping

A form that gives a GPIO count but only a partial list of logical pins is insufficient. Mark `PENDING_LOGICAL_PUBLIC_ID_MAPPING`; do not invent sequential IDs, remap physical pins, deploy the full INI, reload the driver, or launch Machine-B validation.

## Minimal validation command

```bash
<REPO>/.venv/bin/python <REPO>/build_machineB_section_configs.py \
  --matrix <REPO>/CASES/<PROJECT>/<PROJECT>-section-matrix.json \
  --output-dir <REPO>/CASES/<PROJECT>
```

Treat a `public ID must be in range 0..127` failure as a generation/configuration blocker, not as a reason to retry remote validation.
