"""
fetch_probe.py — A 端腳本
從 target board 觸發 probe，收回 susi_board_probe_report.txt 並保存到本地。

支援兩種模式：
1) ssh（預設）：
   - 透過 SSH 執行遠端 .bat
   - 透過 scp -O 把 report 拉回本地
2) http：
   - 舊版 forB/agent.py API（POST /run）

常用範例：
  # SSH 流程：存到 CASES/SOM-9590/SOM-9590_susi_board_probe_report.txt
  python3 fetch_probe.py --mode ssh --host 172.22.12.77 --ssh-user susiaa --project SOM-9590

  # SSH 流程：自訂輸出檔名
  python3 fetch_probe.py --mode ssh --host 172.22.12.77 --ssh-user susiaa \
    --project SOM-9590 --output-name susi_board_probe_report.txt

  # HTTP 舊流程（相容）
  python3 fetch_probe.py --mode http --host 172.22.12.133 --project SOM-6884
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import shlex
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path


B_PORT = 8765
DEFAULT_REMOTE_BAT = "C:/Users/susiaa/Desktop/suto/run_susi_full_probe.bat"
DEFAULT_REMOTE_REPORT = "C:/Users/susiaa/Desktop/suto/susi_board_probe_report.txt"


def _default_out_dir(project: str | None, out_dir: str | None) -> Path:
    if out_dir:
        return Path(out_dir)
    if project:
        return Path("CASES") / project
    return Path("probe_reports")


def _default_filename(project: str | None, output_name: str | None) -> str:
    if output_name:
        return output_name
    if project:
        return f"{project}_susi_board_probe_report.txt"
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"probe_{ts}.txt"


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
        sys.exit(f"[ERROR] Cannot reach Machine B ({host}:{B_PORT}): {e}")

    if "report" not in data:
        sys.exit(f"[ERROR] Unexpected response: {data}")

    return data["report"]


def _run(cmd: list[str]) -> None:
    p = subprocess.run(cmd, text=True, capture_output=True)
    if p.returncode != 0:
        cmd_s = " ".join(shlex.quote(x) for x in cmd)
        err = (p.stderr or p.stdout or "").strip()
        sys.exit(f"[ERROR] Command failed ({p.returncode}):\n{cmd_s}\n{err}")


def fetch_probe_ssh(
    host: str,
    ssh_user: str,
    remote_bat: str,
    remote_report: str,
    out_path: Path,
) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)

    remote = f"{ssh_user}@{host}"

    # 1) 觸發遠端 probe
    _run(["ssh", remote, remote_bat])

    # 2) 拉回 report（-O: legacy scp protocol，Windows OpenSSH 常需要）
    _run(["scp", "-O", f"{remote}:{remote_report}", str(out_path)])


def save_report_text(content: str, out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(content, encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch SUSI probe report from target board")
    ap.add_argument("--mode", choices=["ssh", "http"], default="ssh", help="Fetch mode (default: ssh)")
    ap.add_argument("--host", required=True, help="Target host IP or hostname")

    # 路徑與命名
    ap.add_argument("--project", default=None, help="Project name (e.g., SOM-9590), used for default output path/name")
    ap.add_argument("--dir", default=None, help="Output directory (default: CASES/<project>/ or probe_reports/)")
    ap.add_argument("--output-name", default=None, help="Output filename (default: <project>_susi_board_probe_report.txt)")

    # SSH 參數
    ap.add_argument("--ssh-user", default=None, help="SSH username (required in ssh mode)")
    ap.add_argument("--remote-bat", default=DEFAULT_REMOTE_BAT, help=f"Remote BAT path (default: {DEFAULT_REMOTE_BAT})")
    ap.add_argument("--remote-report", default=DEFAULT_REMOTE_REPORT, help=f"Remote report path (default: {DEFAULT_REMOTE_REPORT})")

    args = ap.parse_args()

    out_dir = _default_out_dir(args.project, args.dir)
    filename = _default_filename(args.project, args.output_name)
    out_path = out_dir / filename

    if args.mode == "ssh":
        if not args.ssh_user:
            sys.exit("[ERROR] --ssh-user is required in ssh mode")

        print(f"[*] SSH mode: triggering probe on {args.ssh_user}@{args.host}")
        print(f"[*] Remote BAT: {args.remote_bat}")
        print(f"[*] Remote report: {args.remote_report}")
        fetch_probe_ssh(
            host=args.host,
            ssh_user=args.ssh_user,
            remote_bat=args.remote_bat,
            remote_report=args.remote_report,
            out_path=out_path,
        )
        print(f"[OK] Report saved: {out_path}")
        return

    # HTTP mode (legacy)
    print(f"[*] HTTP mode: fetching probe report from {args.host}:{B_PORT}")
    content = fetch_probe_http(args.host)
    save_report_text(content, out_path)
    print(f"[OK] Report saved: {out_path}")


if __name__ == "__main__":
    main()
