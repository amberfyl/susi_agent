#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(cmd, text=True, capture_output=True)
    if check and p.returncode != 0:
        cmd_s = " ".join(shlex.quote(x) for x in cmd)
        detail = (p.stderr or p.stdout or "").strip()
        raise RuntimeError(f"command failed ({p.returncode}):\n{cmd_s}\n{detail}")
    return p


def _to_ps_win(path: str) -> str:
    return path.replace("/", "\\")


def _to_scp_remote_path(path: str) -> str:
    # Windows OpenSSH/scp 常用格式：/C:/Users/...
    p = path.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", p):
        return "/" + p
    return p


class Remote:
    def __init__(self, host: str, user: str) -> None:
        self.host = host
        self.user = user
        self.remote = f"{user}@{host}"

    def ssh_ps(self, ps_command: str, *, check: bool = True) -> subprocess.CompletedProcess[str]:
        encoded = base64.b64encode(ps_command.encode("utf-16le")).decode("ascii")
        cmd = [
            "ssh",
            self.remote,
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ]
        return _run(cmd, check=check)

    def scp_put(self, local_path: Path, remote_path: str) -> None:
        target = f"{self.remote}:{_to_scp_remote_path(remote_path)}"
        _run(["scp", "-O", str(local_path), target])

    def scp_get(self, remote_path: str, local_path: Path) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        source = f"{self.remote}:{_to_scp_remote_path(remote_path)}"
        _run(["scp", "-O", source, str(local_path)])


def _parse_report_path(stdout: str) -> str | None:
    # 支援格式：
    # DONE. Report: C:\...\hwm_fan_*.json
    # DONE. Result=CONDITIONAL; Report=C:\...\hwm_fan_control_*.json
    patterns = [
        r"Report\s*[:=]\s*([^\r\n;]+)",
    ]
    for pat in patterns:
        m = re.search(pat, stdout, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def _latest_report_on_remote(remote: Remote, out_dir: str, prefix: str) -> str:
    ps = (
        f"$d='{_to_ps_win(out_dir)}'; "
        f"$f=Get-ChildItem -LiteralPath $d -Filter '{prefix}_*.json' -ErrorAction SilentlyContinue "
        "| Sort-Object LastWriteTime -Descending | Select-Object -First 1; "
        "if($null -eq $f){ exit 3 }; Write-Output $f.FullName"
    )
    p = remote.ssh_ps(ps, check=False)
    if p.returncode != 0:
        raise RuntimeError(f"cannot locate remote report ({prefix}) under {out_dir}")
    path = p.stdout.strip().splitlines()[-1].strip()
    if not path:
        raise RuntimeError(f"empty remote report path for {prefix}")
    return path


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _section_result(local_report: Path) -> dict[str, Any]:
    data = _read_json(local_report)
    return {
        "result": data.get("result", "UNKNOWN"),
        "reason": data.get("reason", ""),
        "report_path": str(local_report.resolve()),
    }


def main() -> None:
    ap = argparse.ArgumentParser(
        description="A端 section 序列驗證 orchestrator（HWM.Fan + HWM.Fan.Control + SMBus）"
    )
    ap.add_argument("--project", required=True, help="專案名，例如 AIMB-289")
    ap.add_argument("--host", required=True, help="目標機IP/hostname（可變）")
    ap.add_argument("--ssh-user", default="susiaa", help="SSH 帳號（default: susiaa）")

    ap.add_argument("--case-root", default="CASES", help="本地 CASES root (default: CASES)")

    ap.add_argument(
        "--remote-verify-root",
        default="C:/Users/susiaa/Desktop/verify",
        help="目標機 verify root (default: C:/Users/susiaa/Desktop/verify)",
    )
    ap.add_argument(
        "--remote-susi-dir",
        default="C:/Windows/SUSI",
        help="目標機 SUSI 目錄 (default: C:/Windows/SUSI)",
    )

    ap.add_argument(
        "--fan-ini-name",
        default=None,
        help="本地 fan section ini檔名（預設 <project>_HWM.Fan.ini）",
    )
    ap.add_argument(
        "--fan-control-ini-name",
        default=None,
        help="本地 fan.control section ini檔名（預設 <project>_HWM.Fan.Control.ini）",
    )
    ap.add_argument(
        "--fan-bundle-ini-name",
        default=None,
        help="同時含 [Information]/[HWM.Fan]/[HWM.Fan.Control] 的合併INI（指定後 fan/fan.control 都用這一份）",
    )
    ap.add_argument(
        "--fan-config-name",
        default=None,
        help="本地 fan config json檔名（預設 <project>_fan.json）",
    )
    ap.add_argument(
        "--fan-control-config-name",
        default=None,
        help="本地 fan.control config json檔名（預設 <project>_fancontrol_enabled.json）",
    )
    ap.add_argument(
        "--smbus-ini-name",
        default=None,
        help="本地 SMBus section ini檔名（預設 <project>_SMBus.ini）",
    )
    ap.add_argument(
        "--smbus-config-name",
        default=None,
        help="本地 SMBus config json檔名（預設 <project>_smbus.json）",
    )
    ap.add_argument(
        "--skip-smbus",
        action="store_true",
        help="僅跑 HWM.Fan + HWM.Fan.Control，不跑 SMBus",
    )

    ap.add_argument(
        "--skip-backup",
        action="store_true",
        help="跳過備份/還原既有 C:/Windows/SUSI/<project>.ini",
    )

    args = ap.parse_args()

    project = args.project
    case_dir = Path(args.case_root) / project
    fan_ini_name = args.fan_ini_name or f"{project}_HWM.Fan.ini"
    fan_control_ini_name = args.fan_control_ini_name or f"{project}_HWM.Fan.Control.ini"
    if args.fan_bundle_ini_name:
        fan_ini_name = args.fan_bundle_ini_name
        fan_control_ini_name = args.fan_bundle_ini_name
    fan_cfg_name = args.fan_config_name or f"{project}_fan.json"
    fan_control_cfg_name = args.fan_control_config_name or f"{project}_fancontrol_enabled.json"
    smbus_ini_name = args.smbus_ini_name or f"{project}_SMBus.ini"
    smbus_cfg_name = args.smbus_config_name or f"{project}_smbus.json"

    local_files = {
        "fan_ini": case_dir / fan_ini_name,
        "fan_control_ini": case_dir / fan_control_ini_name,
        "fan_cfg": case_dir / fan_cfg_name,
        "fan_control_cfg": case_dir / fan_control_cfg_name,
    }
    if not args.skip_smbus:
        local_files["smbus_ini"] = case_dir / smbus_ini_name
        local_files["smbus_cfg"] = case_dir / smbus_cfg_name
    missing = [str(p) for p in local_files.values() if not p.exists()]
    if missing:
        sys.exit("[ERROR] missing local artifacts:\n- " + "\n- ".join(missing))

    ts = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    local_out_dir = (case_dir / "verify_out" / f"remote_{args.host.replace('.', '_')}_{ts}").resolve()
    local_out_dir.mkdir(parents=True, exist_ok=True)

    remote = Remote(host=args.host, user=args.ssh_user)
    remote_verify_root = args.remote_verify_root
    remote_project_dir = f"{remote_verify_root}/{project}"
    remote_out_dir = f"{remote_verify_root}/out/{project}"
    remote_susi_ini = f"{args.remote_susi_dir}/{project}.ini"
    remote_backup_ini = f"{remote_project_dir}/{project}.ini.backup_before_two_section"

    print(f"[*] target: {remote.user}@{remote.host}")
    print(f"[*] local case dir: {case_dir.resolve()}")
    print(f"[*] local output: {local_out_dir}")

    # 0) 連線 smoke check
    ping = remote.ssh_ps("$env:COMPUTERNAME; whoami", check=False)
    if ping.returncode != 0:
        detail = (ping.stderr or ping.stdout or "").strip()
        sys.exit(f"[ERROR] ssh check failed: {detail}")
    print("[*] ssh ok:")
    print((ping.stdout or "").strip())

    # 1) remote 目錄準備
    remote.ssh_ps(
        " ; ".join(
            [
                f"New-Item -ItemType Directory -Path '{_to_ps_win(remote_verify_root)}' -Force | Out-Null",
                f"New-Item -ItemType Directory -Path '{_to_ps_win(remote_project_dir)}' -Force | Out-Null",
                f"New-Item -ItemType Directory -Path '{_to_ps_win(remote_out_dir)}' -Force | Out-Null",
            ]
        )
    )

    # 2) 上傳本次要用的檔案（section ini + config json）
    for key, p in local_files.items():
        remote_dst = f"{remote_project_dir}/{p.name}"
        remote.scp_put(p, remote_dst)
        print(f"[*] uploaded {key}: {p} -> {remote_dst}")

    # 2b) 同步 machineB 驗證腳本到 remote verify root，避免目標機腳本版本落後
    validation_dir = Path(__file__).resolve().parent / "targetB_task" / "machineB_validation"
    runner_scripts = [
        "common_susi.ps1",
        "reload_susi4_driver.ps1",
        "run_hwm_fan_validation.ps1",
        "run_hwm_fan_control_validation.ps1",
        "run_hwm_fan_control_validation_section.ps1",
    ]
    if not args.skip_smbus:
        runner_scripts.append("run_smbus_validation.ps1")
    for script_name in runner_scripts:
        src = validation_dir / script_name
        if not src.exists():
            sys.exit(f"[ERROR] missing local validation script: {src}")
        remote_dst = f"{remote_verify_root}/{script_name}"
        remote.scp_put(src, remote_dst)
        print(f"[*] synced runner: {src} -> {remote_dst}")

    original_ini_exists = False

    try:
        # 3) backup 既有 SUSI ini（若存在）
        if not args.skip_backup:
            ps_backup = (
                f"$src='{_to_ps_win(remote_susi_ini)}'; "
                f"$dst='{_to_ps_win(remote_backup_ini)}'; "
                "if (Test-Path -LiteralPath $src -PathType Leaf) { "
                "  Copy-Item -LiteralPath $src -Destination $dst -Force; "
                "  Write-Output 'ORIGINAL_EXISTS=1' "
                "} else { Write-Output 'ORIGINAL_EXISTS=0' }"
            )
            backup_p = remote.ssh_ps(ps_backup)
            original_ini_exists = "ORIGINAL_EXISTS=1" in (backup_p.stdout or "")
            print(f"[*] original SUSI ini exists: {original_ini_exists}")

        sections = [
            {
                "name": "HWM.Fan",
                "remote_section_ini": f"{remote_project_dir}/{fan_ini_name}",
                "runner": f"{remote_verify_root}/run_hwm_fan_validation.ps1",
                "runner_args": (
                    f"-ConfigPath '{_to_ps_win(remote_project_dir + '/' + fan_cfg_name)}' "
                    f"-IniPath '{_to_ps_win(remote_susi_ini)}' "
                    f"-OutDir '{_to_ps_win(remote_out_dir)}'"
                ),
                "report_prefix": "hwm_fan",
            },
            {
                "name": "HWM.Fan.Control",
                "remote_section_ini": f"{remote_project_dir}/{fan_control_ini_name}",
                "runner": f"{remote_verify_root}/run_hwm_fan_control_validation.ps1",
                "runner_args": (
                    f"-ConfigPath '{_to_ps_win(remote_project_dir + '/' + fan_control_cfg_name)}' "
                    f"-IniPath '{_to_ps_win(remote_susi_ini)}' "
                    f"-FanConfigPath '{_to_ps_win(remote_project_dir + '/' + fan_cfg_name)}' "
                    f"-FanIniPath '{_to_ps_win(remote_project_dir + '/' + fan_ini_name)}' "
                    f"-OutDir '{_to_ps_win(remote_out_dir)}' -AllowControl"
                ),
                "report_prefix": "hwm_fan_control",
            },
        ]
        if not args.skip_smbus:
            sections.append(
                {
                    "name": "SMBus",
                    "remote_section_ini": f"{remote_project_dir}/{smbus_ini_name}",
                    "runner": f"{remote_verify_root}/run_smbus_validation.ps1",
                    "runner_args": (
                        f"-ConfigPath '{_to_ps_win(remote_project_dir + '/' + smbus_cfg_name)}' "
                        f"-IniPath '{_to_ps_win(remote_susi_ini)}' "
                        f"-OutDir '{_to_ps_win(remote_out_dir)}'"
                    ),
                    "report_prefix": "smbus",
                }
            )

        status_map: dict[str, dict[str, Any]] = {}

        for section in sections:
            name = section["name"]
            print(f"\n=== [{name}] start ===")

            ps_runner_check = (
                f"if (-not (Test-Path -LiteralPath '{_to_ps_win(section['runner'])}' -PathType Leaf)) "
                f"{{ Write-Output 'MISSING_RUNNER'; exit 4 }}"
            )
            runner_check = remote.ssh_ps(ps_runner_check, check=False)
            if runner_check.returncode != 0:
                status_map[name] = {
                    "result": "ERROR",
                    "reason": f"missing runner script on target: {section['runner']}",
                }
                print(f"[!] {name} blocked: missing runner {section['runner']}")
                break

            # 4) 覆蓋 C:\\Windows\\SUSI\\<project>.ini
            ps_deploy = (
                f"Copy-Item -LiteralPath '{_to_ps_win(section['remote_section_ini'])}' "
                f"-Destination '{_to_ps_win(remote_susi_ini)}' -Force"
            )
            remote.ssh_ps(ps_deploy)
            print(f"[*] deployed: {section['remote_section_ini']} -> {remote_susi_ini}")

            # 5) reload SUSI driver
            ps_reload = f"& '{_to_ps_win(remote_verify_root + '/reload_susi4_driver.ps1')}'"
            reload_p = remote.ssh_ps(ps_reload, check=False)
            if reload_p.returncode != 0:
                status_map[name] = {
                    "result": "ERROR",
                    "reason": "reload_susi4_driver failed",
                    "reload_stdout": (reload_p.stdout or "").strip(),
                    "reload_stderr": (reload_p.stderr or "").strip(),
                }
                print(f"[!] {name} blocked by reload failure")
                break

            # 6) 跑 section validator
            ps_run = f"& '{_to_ps_win(section['runner'])}' {section['runner_args']}"
            run_p = remote.ssh_ps(ps_run, check=False)
            run_stdout = (run_p.stdout or "").strip()
            run_stderr = (run_p.stderr or "").strip()
            print(run_stdout)
            if run_stderr:
                print(run_stderr)

            # 7) 找 report path + 立刻拉回
            report_remote = _parse_report_path(run_stdout)
            if not report_remote:
                report_remote = _latest_report_on_remote(remote, remote_out_dir, section["report_prefix"])
            report_name = Path(report_remote.replace("\\", "/")).name
            local_report = local_out_dir / report_name
            remote.scp_get(report_remote, local_report)
            print(f"[*] pulled report: {report_remote} -> {local_report}")

            # 8) 解析結果，更新 status map
            entry = _section_result(local_report)
            entry["runner_exit_code"] = run_p.returncode
            entry["runner_stdout"] = run_stdout
            entry["runner_stderr"] = run_stderr

            # PowerShell script exit code語意：
            # 0=PASS, 2=CONDITIONAL/BLOCKED, 1=FAIL/ERROR
            # 以 JSON result 欄位為主
            status_map[name] = entry

            print(f"[+] {name} => {entry['result']} | {entry['reason']}")

        summary = {
            "project": project,
            "target_host": args.host,
            "ssh_user": args.ssh_user,
            "timestamp": ts,
            "remote": {
                "verify_root": remote_verify_root,
                "project_dir": remote_project_dir,
                "susi_ini": remote_susi_ini,
                "out_dir": remote_out_dir,
                "original_ini_exists": original_ini_exists,
                "backup_ini": remote_backup_ini if (original_ini_exists and not args.skip_backup) else None,
            },
            "sections": status_map,
        }
        summary_path = local_out_dir / f"{project}_section_status_summary.json"
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

        print("\n=== SUMMARY ===")
        for sec, st in status_map.items():
            print(f"- {sec}: {st.get('result')} | {st.get('reason', '')}")
        print(f"[*] summary json: {summary_path}")

    finally:
        if args.skip_backup:
            print("[*] skip restore because --skip-backup")
        else:
            try:
                if original_ini_exists:
                    ps_restore = (
                        f"Copy-Item -LiteralPath '{_to_ps_win(remote_backup_ini)}' "
                        f"-Destination '{_to_ps_win(remote_susi_ini)}' -Force"
                    )
                    remote.ssh_ps(ps_restore)
                    remote.ssh_ps(f"& '{_to_ps_win(remote_verify_root + '/reload_susi4_driver.ps1')}'", check=False)
                    print("[*] restored original SUSI ini and reloaded driver")
                else:
                    ps_cleanup = (
                        f"if (Test-Path -LiteralPath '{_to_ps_win(remote_susi_ini)}' -PathType Leaf) "
                        f"{{ Remove-Item -LiteralPath '{_to_ps_win(remote_susi_ini)}' -Force }}"
                    )
                    remote.ssh_ps(ps_cleanup, check=False)
                    remote.ssh_ps(f"& '{_to_ps_win(remote_verify_root + '/reload_susi4_driver.ps1')}'", check=False)
                    print("[*] no original ini; removed test SUSI ini and reloaded driver")
            except Exception as e:
                print(f"[WARN] restore/cleanup failed: {e}")


if __name__ == "__main__":
    main()
