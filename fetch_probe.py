"""
fetch_probe.py — A 端腳本
從 target board 觸發 probe，收回 report 並保存到本地。

支援模式：
1) ssh：透過 SSH 執行遠端 .bat，scp 拉回 report
2) winrm：透過 WinRM 執行遠端 .bat，直接讀回 report 內容
3) auto：先嘗試 WinRM（可多 host failover），失敗再嘗試 SSH
4) http：舊版 forB/agent.py API（POST /run）
"""

from __future__ import annotations

import argparse
import datetime
import json
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


B_PORT = 8765
DEFAULT_REMOTE_BAT_FULL = "C:/Users/susiaa/Desktop/suto/V7/run_susi_full_probe.bat"
DEFAULT_REMOTE_REPORT_FULL = "C:/Users/susiaa/Desktop/suto/V7/susi_full_probe_report.txt"
DEFAULT_REMOTE_BAT_SPD_IDX = "C:/Users/susiaa/Desktop/suto/V7/run_susi_spd_idx_probe.bat"
DEFAULT_REMOTE_REPORT_SPD_IDX = "C:/Users/susiaa/Desktop/suto/V7/susi_spd_idx_probe_report.txt"

# Backward-compatible aliases (full probe defaults)
DEFAULT_REMOTE_BAT = DEFAULT_REMOTE_BAT_FULL
DEFAULT_REMOTE_REPORT = DEFAULT_REMOTE_REPORT_FULL

# Practical default host pool (WinRM-first/SSH-fallback probe flow)
DEFAULT_HOST_POOL = ["192.168.100.16", "192.168.100.15", "172.22.12.77"]


def _default_out_dir(project: str | None, out_dir: str | None) -> Path:
    if out_dir:
        return Path(out_dir)
    if project:
        return Path("CASES") / project
    return Path("probe_reports")


def _default_filename(project: str | None, output_name: str | None, probe_kind: str) -> str:
    if output_name:
        return output_name
    if project:
        if probe_kind == "spd_idx":
            return f"{project}_susi_spd_idx_probe_report.txt"
        return f"{project}_susi_board_probe_report.txt"
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"probe_{ts}.txt"


def _resolve_hosts(host: str | None, hosts_csv: str | None) -> list[str]:
    hosts: list[str] = []
    if hosts_csv:
        hosts.extend([h.strip() for h in hosts_csv.split(",") if h.strip()])
    if host:
        hosts.append(host.strip())

    if not hosts:
        hosts = list(DEFAULT_HOST_POOL)

    uniq: list[str] = []
    seen: set[str] = set()
    for h in hosts:
        if h and h not in seen:
            uniq.append(h)
            seen.add(h)
    return uniq


def _to_windows_path(path_str: str) -> str:
    return path_str.replace("/", "\\")


def fetch_probe_http(host: str) -> str:
    url = f"http://{host}:{B_PORT}/run"
    payload = json.dumps({"task": "get_probe_report"}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
    except urllib.error.URLError as e:
        raise RuntimeError(f"[ERROR] Cannot reach Machine B ({host}:{B_PORT}): {e}") from e

    if "report" not in data:
        raise RuntimeError(f"[ERROR] Unexpected response: {data}")

    return data["report"]


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(cmd, text=True, capture_output=True)
    if p.returncode != 0:
        cmd_s = " ".join(shlex.quote(x) for x in cmd)
        err = (p.stderr or p.stdout or "").strip()
        raise RuntimeError(f"[ERROR] Command failed ({p.returncode}):\n{cmd_s}\n{err}")
    return p


def _ps_single_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _build_ps_credential(win_user: str | None, win_pass: str | None) -> tuple[str, str]:
    if win_user and win_pass:
        pre = (
            f"$sec = ConvertTo-SecureString {_ps_single_quote(win_pass)} -AsPlainText -Force; "
            f"$cred = New-Object System.Management.Automation.PSCredential({_ps_single_quote(win_user)}, $sec); "
        )
        cred_arg = "-Credential $cred "
        return pre, cred_arg
    return "", ""


def _run_powershell(script: str) -> subprocess.CompletedProcess[str]:
    cmd = [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-Command",
        script,
    ]
    p = subprocess.run(cmd, text=True, capture_output=True)
    if p.returncode != 0:
        err = (p.stderr or p.stdout or "").strip()
        raise RuntimeError(f"[ERROR] PowerShell failed ({p.returncode}):\n{err}")
    return p


def _winrm_preflight(host: str, win_user: str | None, win_pass: str | None) -> bool:
    pre, cred_arg = _build_ps_credential(win_user, win_pass)
    script = (
        "$ErrorActionPreference = 'Stop'; "
        + pre
        + f"Test-WSMan -ComputerName {_ps_single_quote(host)} {cred_arg}| Out-Null; "
        + "Write-Output 'WINRM_OK'"
    )
    try:
        p = _run_powershell(script)
    except RuntimeError:
        return False
    return "WINRM_OK" in (p.stdout or "")


def fetch_probe_ssh(
    host: str,
    ssh_user: str,
    remote_bat: str,
    remote_report: str,
    out_path: Path,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    remote = f"{ssh_user}@{host}"

    # 1) 觸發遠端 probe（Windows 上建議用 cmd /c 執行 .bat）
    bat_win = _to_windows_path(remote_bat)
    _run(["ssh", remote, f'cmd /c "{bat_win}"'])

    # 2) 拉回 report（-O: legacy scp protocol，Windows OpenSSH 常需要）
    _run(["scp", "-O", f"{remote}:{remote_report}", str(out_path)])


def fetch_probe_winrm(
    host: str,
    remote_bat: str,
    remote_report: str,
    out_path: Path,
    win_user: str | None,
    win_pass: str | None,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    bat_win = _to_windows_path(remote_bat)
    report_win = _to_windows_path(remote_report)
    pre, cred_arg = _build_ps_credential(win_user, win_pass)

    script = (
        "$ErrorActionPreference = 'Stop'; "
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
        + pre
        + f"$hostName = {_ps_single_quote(host)}; "
        + f"$bat = {_ps_single_quote(bat_win)}; "
        + f"$report = {_ps_single_quote(report_win)}; "
        + "$sbRun = { param($p) cmd /c $p | Out-Null; return [int]$LASTEXITCODE }; "
        + f"$exitCode = Invoke-Command -ComputerName $hostName {cred_arg}-ScriptBlock $sbRun -ArgumentList $bat; "
        + "if ([int]$exitCode -ne 0) { throw \"Remote BAT exited with code $exitCode\" }; "
        + "$sbRead = { param($p) Get-Content -Path $p -Raw -Encoding UTF8 }; "
        + f"$txt = Invoke-Command -ComputerName $hostName {cred_arg}-ScriptBlock $sbRead -ArgumentList $report; "
        + "Write-Output $txt"
    )

    p = _run_powershell(script)
    content = p.stdout or ""
    if not content.strip():
        raise RuntimeError("[ERROR] WinRM succeeded but report content is empty")
    out_path.write_text(content, encoding="utf-8")


def save_report_text(content: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch SUSI probe report from target board")
    ap.add_argument(
        "--mode",
        choices=["ssh", "http", "winrm", "auto"],
        default="ssh",
        help="Fetch mode (ssh/http/winrm/auto, default: ssh)",
    )
    ap.add_argument("--host", default=None, help="Target host IP or hostname (optional; default host pool used when omitted)")
    ap.add_argument("--hosts", default=None, help="Fallback host list, comma-separated (optional). Default pool: 192.168.100.16,192.168.100.15,172.22.12.77")
    ap.add_argument(
        "--probe-kind",
        choices=["full", "spd_idx"],
        default="full",
        help="Probe script profile for default remote paths (default: full)",
    )

    # 路徑與命名
    ap.add_argument("--project", default=None, help="Project name (e.g., SOM-9590), used for default output path/name")
    ap.add_argument("--dir", default=None, help="Output directory (default: CASES/<project>/ or probe_reports/)")
    ap.add_argument(
        "--output-name",
        default=None,
        help="Output filename (default: <project>_susi_board_probe_report.txt or <project>_susi_spd_idx_probe_report.txt by --probe-kind)",
    )

    # SSH 參數
    ap.add_argument("--ssh-user", default=None, help="SSH username (required in ssh mode)")

    # WinRM 參數（可選；若不提供，使用目前 Windows 登入憑證情境）
    ap.add_argument("--win-user", default=None, help="WinRM username (optional)")
    ap.add_argument("--win-pass", default=None, help="WinRM password (optional; prefer secret env in production)")

    ap.add_argument("--remote-bat", default=None, help="Remote BAT path (default depends on --probe-kind)")
    ap.add_argument("--remote-report", default=None, help="Remote report path (default depends on --probe-kind)")

    args = ap.parse_args()

    hosts = _resolve_hosts(args.host, args.hosts)

    out_dir = _default_out_dir(args.project, args.dir)
    filename = _default_filename(args.project, args.output_name, args.probe_kind)
    out_path = out_dir / filename

    if args.probe_kind == "spd_idx":
        remote_bat = args.remote_bat or DEFAULT_REMOTE_BAT_SPD_IDX
        remote_report = args.remote_report or DEFAULT_REMOTE_REPORT_SPD_IDX
    else:
        remote_bat = args.remote_bat or DEFAULT_REMOTE_BAT_FULL
        remote_report = args.remote_report or DEFAULT_REMOTE_REPORT_FULL

    last_err: Exception | None = None

    if args.mode == "http":
        # HTTP 只吃單一 host；若給多個，依序嘗試
        for h in hosts:
            try:
                print(f"[*] HTTP mode: fetching probe report from {h}:{B_PORT}")
                content = fetch_probe_http(h)
                save_report_text(content, out_path)
                print(f"[OK] Report saved: {out_path}")
                return
            except Exception as e:
                last_err = e
                print(f"[WARN] HTTP failed on {h}: {e}")
        sys.exit(str(last_err) if last_err else "[ERROR] HTTP failed")

    def try_winrm() -> bool:
        nonlocal last_err
        for h in hosts:
            print(f"[*] WinRM preflight: {h}")
            if not _winrm_preflight(h, args.win_user, args.win_pass):
                print(f"[WARN] WinRM unreachable or auth failed: {h}")
                continue
            try:
                print(f"[*] WinRM mode: triggering probe on {h}")
                print(f"[*] Remote BAT: {remote_bat}")
                print(f"[*] Remote report: {remote_report}")
                fetch_probe_winrm(
                    host=h,
                    remote_bat=remote_bat,
                    remote_report=remote_report,
                    out_path=out_path,
                    win_user=args.win_user,
                    win_pass=args.win_pass,
                )
                print(f"[OK] Report saved: {out_path}")
                print(f"[OK] Selected host: {h} (WinRM)")
                return True
            except Exception as e:
                last_err = e
                print(f"[WARN] WinRM failed on {h}: {e}")
        return False

    def try_ssh() -> bool:
        nonlocal last_err
        if not args.ssh_user:
            last_err = RuntimeError("[ERROR] --ssh-user is required for ssh mode")
            return False
        for h in hosts:
            try:
                print(f"[*] SSH mode: triggering probe on {args.ssh_user}@{h}")
                print(f"[*] Remote BAT: {remote_bat}")
                print(f"[*] Remote report: {remote_report}")
                fetch_probe_ssh(
                    host=h,
                    ssh_user=args.ssh_user,
                    remote_bat=remote_bat,
                    remote_report=remote_report,
                    out_path=out_path,
                )
                print(f"[OK] Report saved: {out_path}")
                print(f"[OK] Selected host: {h} (SSH)")
                return True
            except Exception as e:
                last_err = e
                print(f"[WARN] SSH failed on {h}: {e}")
        return False

    if args.mode == "winrm":
        if try_winrm():
            return
        sys.exit(str(last_err) if last_err else "[ERROR] WinRM failed")

    if args.mode == "ssh":
        if try_ssh():
            return
        sys.exit(str(last_err) if last_err else "[ERROR] SSH failed")

    # auto: WinRM first, SSH fallback
    print("[*] Auto mode: try WinRM first, then SSH fallback")
    if try_winrm():
        return
    print("[WARN] Auto mode WinRM path failed, fallback to SSH")
    if try_ssh():
        return
    sys.exit(str(last_err) if last_err else "[ERROR] Auto mode failed")


if __name__ == "__main__":
    main()
