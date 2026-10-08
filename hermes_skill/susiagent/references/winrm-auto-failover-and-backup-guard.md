# WinRM-first auto failover + backup guard

Use when updating `/susiagent` fetch flow or `fetch_probe.py` for mixed-target environments.

## Durable practice

1) Prefer `fetch_probe.py --mode auto` with multi-host candidates (`--hosts a,b` + optional `--host c`).
- Runtime order: WinRM first per host, then SSH fallback.
- Goal: reduce manual host switching when one target is unreachable.

2) Keep canonical SUSI output naming/path in project directory.
- `CASES/<PROJECT>/<PROJECT>_susi_board_probe_report.txt`
- `CASES/<PROJECT>/<PROJECT>_susi_spd_idx_probe_report.txt`

3) Before editing skill/code, create timestamped rollback backups (`.bk_<ts>`).
- Required when user explicitly asks rollback safety.
- Example:
  - `cp fetch_probe.py fetch_probe.py.bk_<ts>`
  - `cp ~/.hermes/skills/software-development/susiagent/SKILL.md ~/.hermes/skills/software-development/susiagent/SKILL.md.bk_<ts>`

4) WinRM verification gate before declaring success.
- Remote BAT must return ExitCode=0.
- Report pull/read must produce non-empty content.

## Suggested command templates

Full probe:
`/home/company2/AIagent_susi/.venv/bin/python /home/company2/AIagent_susi/fetch_probe.py --mode auto --hosts <HOST1>,<HOST2> --host <HOST3> --project <PROJECT> --ssh-user <user> --dir /home/company2/AIagent_susi/CASES/<PROJECT> --output-name <PROJECT>_susi_board_probe_report.txt`

SPD idx probe:
`/home/company2/AIagent_susi/.venv/bin/python /home/company2/AIagent_susi/fetch_probe.py --mode auto --probe-kind spd_idx --hosts <HOST1>,<HOST2> --host <HOST3> --project <PROJECT> --ssh-user <user> --dir /home/company2/AIagent_susi/CASES/<PROJECT> --output-name <PROJECT>_susi_spd_idx_probe_report.txt`

Optional explicit WinRM credential:
`--win-user <WIN_USER> --win-pass <WIN_PASS>`
