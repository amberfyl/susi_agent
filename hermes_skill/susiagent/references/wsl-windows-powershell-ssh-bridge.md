# WSL -> Windows PowerShell SSH/SCP Bridge (SUSI target-B)

When WSL cannot reach target-B (ping/ssh fail) but Windows host can, execute SSH/SCP through Windows OpenSSH from WSL.

## Preconditions
- `powershell.exe` callable from WSL.
- Windows has working SSH key for target-B.
- Prefer non-interactive options for automation:
  - `-o IdentitiesOnly=yes`
  - optional preflight: `-o BatchMode=yes -o ConnectTimeout=5`

## Canonical command pattern

### 1) Remote identity sanity check
```bash
powershell.exe -NoProfile -Command "ssh -i C:\Users\<win_user>\.ssh\id_ed25519_susi -o IdentitiesOnly=yes <user>@<host> 'whoami && hostname'"
```

### 2) Run probe BAT and wait until complete
```bash
powershell.exe -NoProfile -Command "ssh -i C:\Users\<win_user>\.ssh\id_ed25519_susi -o IdentitiesOnly=yes <user>@<host> \"cmd /c C:\\Users\\<user>\\Desktop\\suto\\V7\\run_susi_full_probe.bat\""
```

### 3) Pull report back to Windows local path
```bash
powershell.exe -NoProfile -Command "scp -i C:\Users\<win_user>\.ssh\id_ed25519_susi -o IdentitiesOnly=yes <user>@<host>:/C:/Users/<user>/Desktop/suto/V7/susi_full_probe_report.txt C:\Users\<win_user>\Desktop\susi_full_probe_report.txt"
```

## Path/quoting notes
- Keep key path and destination path in **Windows style** inside PowerShell command strings.
- For remote Windows command execution via SSH, wrap BAT launch with `cmd /c`.
- If `if exist (...)` style command produces quoting/parser errors, switch to simpler check (`cmd /c dir <file>`) to verify file existence.

## Integration note for /susiagent
- This bridge mode is a transport fallback only.
- After report retrieval, continue normal pipeline using local fetched probe artifact under project directory.
