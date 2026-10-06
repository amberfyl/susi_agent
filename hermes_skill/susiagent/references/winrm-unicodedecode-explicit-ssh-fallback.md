# WinRM UnicodeDecodeError crash -> explicit SSH fallback (canonical output lock)

When running `/susiagent` fetch stage from WSL, `fetch_probe.py --mode auto` may crash during WinRM preflight with a Python traceback:

- `UnicodeDecodeError: 'utf-8' codec can't decode byte ...`
- crash location often includes `_run_powershell(... subprocess.run(... text=True, capture_output=True))`

This is a WinRM stderr decoding/tooling-path blocker for that run. Treat it as **WinRM path failed**, not fetch success.

## Required recovery sequence

1. Run SSH auth preflight first (non-interactive):

```bash
ssh -o BatchMode=yes -o ConnectTimeout=10 susiaa@<HOST> "echo ok"
```

2. If preflight returns `ok`, rerun fetch in explicit SSH mode (do not retry auto in loop):

```bash
/home/company2/AIagent_susi/.venv/bin/python /home/company2/AIagent_susi/fetch_probe.py \
  --mode ssh \
  --host <HOST> \
  --ssh-user susiaa \
  --project <PROJECT> \
  --dir /home/company2/AIagent_susi/CASES/<PROJECT> \
  --output-name <PROJECT>_susi_board_probe_report.txt \
  --remote-bat "C:/Users/susiaa/Desktop/suto/V7/run_susi_full_probe.bat" \
  --remote-report "C:/Users/susiaa/Desktop/suto/V7/susi_full_probe_report.txt"
```

3. Continue pipeline with explicit canonical paths for `susi_gen.py` outputs (`--out-json/--spec-out/--out-ini`) to avoid artifact drift.

## Notes

- `--dir` + `--output-name` are mandatory in this recovery to keep probe artifacts under `CASES/<PROJECT>/`.
- If SSH preflight fails with `Permission denied (publickey,password)`, classify as auth blocker and stop for credentials/local-probe override.
