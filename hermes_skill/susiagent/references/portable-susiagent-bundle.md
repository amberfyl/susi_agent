# Portable `/susiagent` bundle

## Scope

A complete transfer requires both the Hermes skill and the executable AIagent_susi project. Copying `SKILL.md` alone does not transfer generators, DB queries, Machine-B runners, tests, or orchestration code.

## Bundle shape

```text
susiagent-bundle/
├── AIagent_susi/
├── hermes-skill/susiagent/
├── README_INSTALL.md
└── MANIFEST.sha256
```

Include:

- core Python entrypoints and helpers;
- active configuration DB;
- `AnalysisSKill/`;
- validation skill pack;
- Machine-B PowerShell/BAT runners and shared helpers;
- tests and deterministic tools;
- `/susiagent` `SKILL.md` plus every referenced file.

Exclude:

- `.git`, `.venv`, `node_modules`, `__pycache__` and temporary files;
- CASES project inputs/results unless explicitly requested;
- credentials, SSH keys, `.env`, OAuth/auth stores and secrets;
- stale/retired databases and golden-diff utilities that are no longer part of the active architecture.

## Hardcoded-path gate

When code or skill instructions still contain `/home/company2/AIagent_susi`, the receiving machine must install the project at exactly that path. Do not describe the archive as generally portable until paths are changed to a configurable repo root.

## Verification

Before delivery:

1. Generate SHA-256 for every bundled file and an outer archive checksum.
2. Extract into a temporary directory.
3. Verify the internal checksum manifest.
4. Run the bundled unit tests using an available project-compatible interpreter.
5. Exercise `--help` on core entrypoints.
6. Run one real read-only DB query.
7. Assert that `SKILL.md` and a key orchestration reference are present.

Report archive path, size, file count, checksum, exclusions, hardcoded installation path, and observed test result.
