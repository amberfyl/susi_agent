---
name: susiagent
description: "SUSI pipeline: analyze BIOS/schematic images into intermediate JSON, generate SUSI INI and Machine-B section JSON from config_new.db, and (with --all or --validate and a confirmed host) run Machine-B phase 1 validation through rollback."
version: 2.0.0
author: Hermes Agent
license: MIT
platforms: [linux, windows]
---

# SusiAgent

This skill is a thin adapter. It says what the agent does, in what order, and where the rules live.
Repository root: `/home/company2/AIagent_susi` (below: `<REPO>`). Project directory: `<REPO>/CASES/<PROJECT>/`.

## 1. Read first (every run)

1. `<REPO>/AnalysisSKill/orchestrator_skill.md` — cross-stage policy, section rules, status meanings.
2. Before reading BIOS images: `<REPO>/prompts/bios_reading.md`.
3. Before tracing GPIO in schematics: `<REPO>/prompts/gpio_trace.md`.
4. Before fan IN/OUT pairing or voltage-divider analysis: `<REPO>/AnalysisSKill/bios_circuit_image_analysis_rule.md` (R-013, R-015, R-019) and `<REPO>/AnalysisSKill/fan_pairing_contract.md`.
5. Before interpreting Machine-B reports: `<REPO>/AnalysisSKill/verdict_report_skill.md`.

Always read the current files; never rely on remembered summaries. When sources disagree, the order is: the user's latest explicit decision > Python code and tests > `AnalysisSKill/*.md` > `prompts/*.md` wording > runbooks.

## 2. Modes

| Command | Steps | Touches Machine-B |
|---|---|---|
| `/susiagent <PROJECT>` | 3.1–3.8: generate the INI and the Machine-B section JSON, then stop | Only 3.2 probe fetch and, on SIO, 3.7 (needs consent) |
| `/susiagent <PROJECT> --validate --host <HOST> [--user <USER>]` | 3.9 only, on the existing INI and section matrix | Yes |
| `/susiagent <PROJECT> --all --host <HOST> [--user <USER>]` | 3.1–3.9 | Yes |

- `--limit` may be added to `--validate` or `--all`: the validation is read-only (see 3.9).
- `--user` defaults to `susiaa`. The host must be given in the current invocation; never infer it from context or an unrelated SSH alias.
- Target syntax: `susiaa@<IP>` in the command means `--user susiaa --host <IP>`. The user usually gives two IPs for the same Machine-B:
  - **SSH IP** (office LAN, DHCP, e.g. `172.22.12.*` / `172.22.13.*`): the primary path.
  - **Direct-link static IP** (`192.168.100.10–50`, this host is `192.168.100.1`): the fallback.
  Both change when the test disk moves to another platform, so use only the IPs given in the current invocation.
- Transport: **SSH first, WinRM only as fallback.** Probe fetch (3.2) uses `--mode auto`, which tries SSH on every given IP, then WinRM on the direct-link IP. Validation (3.9) is SSH-only.
- `--all`, `--validate` and `--limit` belong to this skill; never pass them to `susi_gen.py`.
- `--all` / `--validate` authorize the phase 1 validation lifecycle: staging, backup, one full-INI deploy, one reload, the runners with their reversible write tests (set → read back → restore), report collection, rollback, one recovery reload. They never enable fixture (`EnableFixtureTest`) or stimulus (`EnableStimulus`) switches.
- `--validate` preflight (stop and report if any fails): `<PROJECT>-pre.ini` and `<PROJECT>-section-matrix.json` exist, `section-matrix.project` equals `<PROJECT>`, and no section is blocked. Report the INI's last-modified time so the user can see which INI is being tested. Do not re-run 3.2–3.8; the section JSON is rebuilt from the matrix by the validator itself.
- Details: orchestrator section 5 and 11.

## 3. Execution steps

Always use `<REPO>/.venv/bin/python`. Pass absolute, project-local paths for every input and output.

### 3.1 Resolve the project
- Match `<PROJECT>` against `<REPO>/CASES/` case-insensitively and use the on-disk name everywhere. Stop if there is no match or more than one.
- Use only the project named in the latest user message.

### 3.2 Fetch the probe report (skip in local-probe mode, see 5.1)
```
<REPO>/.venv/bin/python <REPO>/fetch_probe.py --mode auto --ssh-user susiaa --project <PROJECT> \
  --host <SSH_IP> [--hosts <DIRECT_LINK_IP>] \
  --remote-bat "C:/Users/susiaa/Desktop/suto/V7/run_susi_full_probe.bat" \
  --remote-report "C:/Users/susiaa/Desktop/suto/V7/susi_full_probe_report.txt" \
  --dir <REPO>/CASES/<PROJECT> --output-name <PROJECT>_susi_board_probe_report.txt
```
- `auto` order: SSH on `<SSH_IP>`, then SSH on `<DIRECT_LINK_IP>`, then WinRM on `<DIRECT_LINK_IP>`. WinRM is never tried on an IP outside `192.168.100.10–50` and never guessed from a default pool. Use `--mode winrm --host <DIRECT_LINK_IP>` only when the user explicitly asks for WinRM. Add `--probe-kind spd_idx` (output `<PROJECT>_susi_spd_idx_probe_report.txt`) for AMD platforms.
- If fetching fails, report the exact error and stop. Connection problems: see the runbook index (section 6).

### 3.3 Extract and understand the request form
```
<REPO>/.venv/bin/python <REPO>/susi_gen.py --stage extract --project <REPO>/CASES/<PROJECT> --in-pdf <PDF> --out-json <REPO>/CASES/<PROJECT>/<PROJECT>.json
<REPO>/.venv/bin/python <REPO>/susi_gen.py --stage understand --project <REPO>/CASES/<PROJECT> --in-json <REPO>/CASES/<PROJECT>/<PROJECT>.json --spec-out <REPO>/CASES/<PROJECT>/<PROJECT>-spec.json
```
- If `<PROJECT>.pdf` does not exist, pass the project's request-form PDF explicitly with `--in-pdf`; keep output names as `<PROJECT>.*`.

### 3.4 Decide the chip route (source of truth: orchestrator 9.1)
- Chip key comes from the spec; if it cannot be inferred, pass `--chip-name <ProductChip.chip_name>` (the DB key, not the marketing name).

| Route | How to tell | Section values | Schematic analysis |
|---|---|---|---|
| EC | probe reports an EC version (EIO-201/211, IT-xxxx, ...) | DB query is the base; probe `[OK]` items filter the count; BIOS cache fills Name | **None** |
| EIO-300 / `NCT6694B*` compound | chip is EIO-300 or `NCT6694B*` | Same as EC. Query `SMBus`/`I2C` with `NCT6694B`, all other sections with `EIO-300` | **`[GPIO]` only** (from the `NCT6694B` side, `EC_P*_GPIO*`) |
| SIO | `NCT6106D/NCT6116D/NCT6126D` | Generator's SIO logic (probe rarely reports) | `[GPIO]`, HWM.Voltage divider, Fan/Fan.Control pairing |

- Do not analyze schematics beyond this table. On EC and EIO-300/`NCT6694B*` boards, never produce `fan-pairing.json` or voltage-divider evidence; the generator ignores fan pairing on EC routes anyway.
- Use `circuit*.pdf` only when `circuit*` images are insufficient.

### 3.5 Analyze images and write the intermediate JSON (agent work)
Write each artifact into the project directory before running generation (3.6). The generator uses existing artifacts as-is. For the BIOS cache and the GPIO trace only, a missing or incomplete artifact makes the generator analyze the images itself (fallback via the agent CLI, section 7); that fallback is a one-shot analysis and weaker than yours, so do not rely on it.

For every schematic analysis allowed by 3.4: locate the hits by keyword first, then judge only from focused high-resolution crops where the text is clearly readable. A full-page render is for navigation only. Save crops in the project directory (see `prompts/gpio_trace.md`, "Reading PDF schematics and small text").

| Artifact | Required when | Rules | Notes |
|---|---|---|---|
| `<PROJECT>-bios-image-cache.json` | BIOS images exist (`bios*.png/jpg/jpeg`, case-insensitive) | `prompts/bios_reading.md` | `{"items":[{"path","filename","sha256","analysis_status":"DONE_VISION_ANALYZE","analysis_text","voltage_label_hints","voltage_value_hints","temperature_value_hints","fan_value_hints","current_value_hints","caseopen_hints"}]}`; value hints are `{label,value,unit}`, caseopen hints are `{label,state}`. Only the Hardware Monitor page has readings; for every other BIOS page leave all hint arrays empty and write `No live hardware-monitor sensor rows are visible.` (HWM sections are enabled only by readings on the monitor page). `sha256` must be the SHA-256 of the image file (`sha256sum`): the generator matches items by image hash and treats unmatched images as not analyzed. |
| `<PROJECT>-gpio-trace.json` | `[GPIO]` on SIO and EIO-300/`NCT6694B*` routes | `prompts/gpio_trace.md` | `{"topology_status","items":[{"report_name","signal","function_label","group","bit","package_pin","status","evidence","name"}],"meta":{"images_analyzed":[...]}}`. `meta.images_analyzed` must list every `circuit*` image used; otherwise the generator treats the trace as incomplete and re-traces. INI keys (`GPIO00`, `GPIO01`, ...) are numbered by the generator in signal order; put the chip function label (e.g. `GPIOD0`) in `function_label`, not in `report_name`. |
| `<PROJECT>-fan-pairing.json` | Fan sections on the **SIO route only** | R-019, `fan_pairing_contract.md` | Normalize with `build_fan_pairing.py --project <PROJECT> --input <analysis file>`. Fan idx is the SUSI fan order, not a chip pin number such as `TA4`/`PWM4`. On SIO, if missing, the generator silently falls back to one-to-one pairing — do not skip it. One-to-many boards come with a separate Fan Control schematic. |
| `voltage_route_hints` (inside the cache item of the circuit image) | SIO route: AIMB + NCT6126D V5SB→V33 (R-013) | R-013 | Known gap: the generator currently drops this field when it rebuilds the cache (`skill_only_checklist.md` A-2). |

### 3.6 Generate
```
<REPO>/.venv/bin/python <REPO>/susi_gen.py --stage generate --split \
  --project <REPO>/CASES/<PROJECT> --root <REPO>/CASES \
  --in-json <REPO>/CASES/<PROJECT>/<PROJECT>.json --spec-out <REPO>/CASES/<PROJECT>/<PROJECT>-spec.json \
  --out-ini <REPO>/CASES/<PROJECT>/<PROJECT>-pre.ini --probe <PROBE_REPORT> [--chip-name <KEY>]
```
- `--split` and `--sections` are mutually exclusive.
- Read `<PROJECT>-section-matrix.json` (its `sections` is an array). Check `prompt_issues` and every section status.

### 3.7 v2 convergence (NCT6106D / NCT6116D / NCT6126D, HWM.Voltage and HWM.Temperature)
- Converged only when the route contains `+SUPERIO_V2_BIOS_PROBE_ALIAS` (Voltage) and `+SUPERIO_TEMP_V2_BIOS_PROBE_ALIAS` (Temperature).
- Otherwise, once: deploy `<PROJECT>-pre.ini` to the target runtime INI, reload the SUSI4 driver, re-fetch the probe (3.2), re-run 3.6, re-check.
- This needs remote side-effect consent (5.4). If still not converged, report the blocker (BIOS labels missing / probe has no `[OK]` channels / no consent) and do not claim completion.

### 3.8 Build Machine-B section JSON
```
<REPO>/.venv/bin/python <REPO>/build_machineB_section_configs.py \
  --matrix <REPO>/CASES/<PROJECT>/<PROJECT>-section-matrix.json --output-dir <REPO>/CASES/<PROJECT>
```
- Only `GENERATED` sections get JSON. Report section buckets (`GENERATED` / `SKIPPED_*` / `PENDING_*`) with absolute paths.
- GPIO preflight: Machine-B consumes the INI key suffix as the SUSI public GPIO API ID, which must be decimal `0..127`. Physical tuple `group,pin` is separate evidence and may include hexadecimal groups (for example `GPIOD0` -> tuple group 13, pin 0). Never encode a physical group/bit by decimal concatenation (`GPIO130`) as the public ID. Require an evidence-backed logical/public ID mapping; if the request form supplies only a count or partial logical entries, mark GPIO `PENDING_LOGICAL_PUBLIC_ID_MAPPING` and do not deploy or run `--all` for that generated artifact. See `references/gpio-public-id-contract.md`.
- Without `--all`, stop here.

### 3.9 Full validation (only with `--all` or `--validate`)
```
<REPO>/.venv/bin/python <REPO>/run_machineB_full_validation.py --project <PROJECT> --repo-root <REPO> \
  --run-id <UNIQUE_RUN_ID> --execute --host <MACHINE_B_HOST> --user <USER> [--no-write-tests]
```
- `<MACHINE_B_HOST>` is the SSH IP. Validation has no WinRM path. If SSH to that IP fails, report it and ask before retrying over SSH on the direct-link IP; never switch IPs silently in the middle of a run.
- Add `--no-write-tests` only when the user gave `--limit`. Then no set/write/control switch reaches any runner; sections with a Set API that were not exercised report `CONDITIONAL`, and the summary's Scope line says `READ-ONLY`.
- Use a new unique run ID each time; never delete or reuse an existing run directory.
- Never run this when generation is blocked or artifacts belong to another project.
- Report `completed` plus the summary path once rollback/recovery closes safely; individual section failures stay in the summary. Interpret results with `verdict_report_skill.md`.
- **Enabled-switch propagation gate:** a manifest's `enabled_switches` is only an intent record, not proof that a runner received a PowerShell switch. When enabling a control/functional/write switch (for example `AllowControl`), first verify the orchestrator passes each enabled switch as a boolean runner argument, with a focused regression test. In the collected section report, confirm the corresponding operation actually ran (L3/L4/L6 evidence); do not describe a `BLOCKED_SAFETY` or "requires switch" result as a completed enabled test. If the declaration and invocation disagree, fix the deterministic Python data flow and test it before any target retry.
- Post-validation route fallback (`--converge --fallback-plan <file>`) follows orchestrator 11.5. It normally needs the user's go-ahead; however, an already-recorded user standing authorization for GPIO post-validation fallback is sufficient when the strict trigger contract is met. Before acting, verify the baseline report has every required `SusiGPIOGetCaps` input/output probe, all of those probes failed, and there is no fixture/safety/infrastructure blocker. Use only the registry whitelist, preserve the baseline after every rejected candidate, and report the convergence artifact.
- GPIO capability mask missing only some bits (for example `0x9FFF` instead of `0xFFFF`): the route (channel/IOPort/option) is correct; the missing bits are GPIOs whose traced group/pin is probably wrong (usually the adjacent pin). Report those GPIOs (key, signal, current function label and group/pin) for human review; do not re-trace or change their values yourself. Never trim those GPIOs, change the route, or treat it as unsupported hardware (verdict_report_skill.md section 4, item 6).
- A fallback candidate that returns a partial capability mask is not a successful convergence. Record the candidate and exact missing public GPIO bit(s), map each bit back to the generated `GPIO00`-style key and trace evidence, restore/preserve the baseline before trying the next whitelist candidate, and continue the registry. If all candidates are exhausted but rollback and recovery reload close safely, report the lifecycle as `completed` with the GPIO failure and convergence artifact in the summary; do not claim GPIO validation passed.
- After `--converge`, `<PROJECT>-machineB-summary.json/.txt` is rewritten as the single final report: converged sections take the winning attempt's verdict, and the text ends with a Details list of every evidence file. The first-pass summary is preserved as `<PROJECT>-machineB-summary.baseline.json/.txt`. Give the user only the final summary path; do not ask them to read `fallback-convergence.json` or the baseline separately.

## 4. Done criteria
- `section-matrix.project` equals `<PROJECT>`; all artifacts are inside the project directory.
- Every one of the 14 sections has a status; skipped/pending sections are reported, not hidden.
- Never claim success from stale artifacts or a failed step.

## 5. Agent judgement (decide from the user's words)

### 5.1 Local-probe mode
- Triggers: the user says the network is down, the probe report is already in the project folder, or not to SSH.
- Then run no network commands at all (ssh, scp, WinRM, `fetch_probe.py`). Use the existing report with `--probe`; do not rename it.

### 5.2 Request-form bypass
- If the user says there is no request form and gives the chip, create a minimal spec (project identity, DB chip key, the features the user enabled) and run `--stage generate`. Never re-enable sections the user left unchecked.
- If `--stage all` fails because the PDF has too many embedded images, report it and continue with `--stage generate`.

### 5.3 Scope
- Read-only requests ("看一下", "確認有沒有寫進去", "不要真的跑"): read and report only; run nothing.
- "簡單跑一下" does not authorize fixes, deployment, reload or functional tests.
- After a context compaction/handoff, act only on the latest explicit user request.

### 5.4 Consent and denials
- Remote side effects (SCP/SSH/WinRM writes, remote scripts, INI deployment, driver reload) need explicit consent in the current turn. `/susiagent ... --all --host` is consent for the safe-default lifecycle only.
- Run one state-changing remote command at a time and verify the result before the next.
- If a command is denied, stop and report; never retry through another command shape or tool.
- Do not prepend destructive cleanup (`rm -f`, `rm -rf`) unless the user asked for it.

## 6. Runbook index (operations only)
Files in `~/.hermes/skills/software-development/susiagent/references/`. Use them only for the situation listed.

| Situation | Runbook |
|---|---|
| WSL cannot reach Machine-B but Windows can | `wsl-windows-powershell-ssh-bridge.md` |
| Multiple hosts / WinRM-first failover / backing up before editing fetch code | `winrm-auto-failover-and-backup-guard.md` |
| SSH host-key conflict, or the user asks to first check existing SSH setup | `ssh-readonly-inventory-before-hostkey-remediation.md` |
| Machine-B rebooted or blue-screened after a run | `susi-target-crash-triage.md` |
| SUSI4 driver reload fails or the device stays disabled | `machineb-susi4-reload-and-app-handle-gate.md` |
| Run a runner against the current target INI without deploying/reloading | `machineb-current-runtime-readonly-diagnostics.md` |
| Vendor C tool passes but a PowerShell runner fails | `machineb-runtime-state-and-cross-tool-diagnosis.md` |
| SMBus fixture (write) test with the QA fixture connected | `machineb-smbus-legacy-fixture-validation.md` |
| Package the whole setup for another machine | `portable-susiagent-bundle.md` |

## 7. Fixed defaults
- Python: `<REPO>/.venv/bin/python` (never system `python3`).
- Machine-B IPs: none are fixed. The same test disk moves between platforms and each NIC gets a different IP, so `fetch_probe.py` requires `--host`; always use the IPs from the current invocation. SSH user `susiaa`.
- WinRM: fallback only, on user-named static IPs in `192.168.100.10–50`; this host is `192.168.100.1`. SSH is also open on the direct-link IP.
- Remote full probe: `C:/Users/susiaa/Desktop/suto/V7/run_susi_full_probe.bat` → `susi_full_probe_report.txt`.
- Remote AMD SPD probe: `C:/Users/susiaa/Desktop/suto/V7/run_susi_spd_idx_probe.bat` → `susi_spd_idx_probe_report.txt`.
- LLM calls made by the program (PDF extract, request-form understanding, vision fallback) all go through `agent_llm.py`, which runs the agent CLI from env `SUSI_AGENT_CMD` (legacy `SUSI_VISION_CMD`; default `hermes -z {prompt} -t vision`; `{prompt}` = prompt text, `{image}` = first image path). Provider, model and credentials are whatever Hermes is currently configured with; the program never sets them, and `LLM_*` env vars are not read.
- User communication: concise Traditional Chinese, result first. Target-machine console/report text stays English ASCII.

## 8. Where new knowledge goes (do not scatter notes)
When a run reveals something worth keeping, put it in exactly one of these places. Do not create other reference notes.

| What it is | Where it goes |
|---|---|
| Operating procedure for the environment (connection, transport, target recovery, packaging) | Update the matching runbook in `references/`; create a new runbook only for a new situation, and add one row to section 6 |
| A decision the agent makes from the user's words | Section 5 of this file |
| A rule for judging evidence, a section rule, or a cross-stage policy | The matching `AnalysisSKill/*.md` (image-reading rules for the generator: `prompts/*.md`) — **ask the user before changing rules** |
| Anything deterministic (condition, template, encoding, mapping) | Python + regression test, not documents |
| Hardware rows/values | `config_new.db` |
| A step that only happens because this file says so (the code does not enforce it) | Append to `<REPO>/skill_only_checklist.md` |

Business rules, templates, numeric mappings and DB facts must never live in this file or in `references/`.
