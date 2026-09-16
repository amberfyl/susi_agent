#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path


def parse_probe_temperature_summary(probe_path: Path) -> dict:
    total = 0
    ok = 0
    err = 0
    in_block = False

    for raw in probe_path.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
        line = raw.strip()
        if line.startswith("==="):
            in_block = "HWM Temperature" in line
            continue
        if not in_block:
            continue
        m = re.match(r"^\[(OK|ERR)\]\s+HWM_TEMP_[A-Z0-9_]+\b", line)
        if not m:
            continue
        total += 1
        if m.group(1) == "OK":
            ok += 1
        else:
            err += 1

    return {
        "total": total,
        "ok": ok,
        "err": err,
        "all_err": (total > 0 and ok == 0 and err == total),
    }


def _parse_candidate_token(raw: str) -> dict | None:
    token = (raw or "").strip()
    if not token:
        return None
    if "," in token:
        parts = [p.strip() for p in token.split(",", 1)]
    elif ":" in token:
        parts = [p.strip() for p in token.split(":", 1)]
    else:
        return None
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    return {"io_port": parts[0], "options": parts[1]}


def load_candidates(candidate_tokens: list[str], candidate_file: Path | None) -> list[dict]:
    out: list[dict] = []

    for tok in candidate_tokens:
        c = _parse_candidate_token(tok)
        if c:
            out.append(c)

    if candidate_file and candidate_file.exists():
        raw = json.loads(candidate_file.read_text(encoding="utf-8"))
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                io_port = str(item.get("io_port") or "").strip()
                options = str(item.get("options") or item.get("option") or "").strip()
                if io_port and options:
                    out.append({"io_port": io_port, "options": options})

    if not out:
        out = [
            {"io_port": "0x2E", "options": "0x80000001"},
            {"io_port": "0x2E", "options": "0x00000001"},
            {"io_port": "0", "options": "0x80000001"},
            {"io_port": "0", "options": "0x00000001"},
        ]

    dedup: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for c in out:
        k = (c["io_port"], c["options"])
        if k in seen:
            continue
        seen.add(k)
        dedup.append(c)
    return dedup


def apply_candidate_to_ini(
    ini_path: Path,
    io_port: str,
    option: str,
    section_name: str = "HWM.Temperature",
) -> dict:
    lines = ini_path.read_text(encoding="utf-8", errors="ignore").splitlines()
    out: list[str] = []
    in_section = False
    changed = 0
    touched_keys: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            in_section = stripped[1:-1].strip().lower() == section_name.lower()
            out.append(line)
            continue

        if in_section and stripped and not stripped.startswith(";") and "=" in line:
            key, val = line.split("=", 1)
            parts = [p.strip() for p in val.split(",")]
            if len(parts) >= 4:
                old_io = parts[2]
                old_opt = parts[3]
                if old_io != io_port or old_opt.lower() != option.lower():
                    parts[2] = io_port
                    parts[3] = option
                    line = f"{key}={','.join(parts)}"
                    changed += 1
                    touched_keys.append(key.strip())

        out.append(line)

    if changed > 0:
        ini_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    return {"changed": changed, "touched_keys": touched_keys}


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Temperature fallback helper: when baseline HWM_TEMP_* are all ERR, "
            "iterate candidate (io_port, options) tuples, rewrite INI, and optionally "
            "run validation/probe command until one candidate passes."
        )
    )
    ap.add_argument("--ini", action="append", required=True, help="Path to INI file (repeatable)")
    ap.add_argument("--probe", required=True, help="Path to probe report txt (updated by runner when looping)")
    ap.add_argument("--section", default="HWM.Temperature")
    ap.add_argument(
        "--candidate",
        action="append",
        default=[],
        help="Candidate tuple in 'io_port,options' or 'io_port:options' format (repeatable)",
    )
    ap.add_argument("--candidate-file", help="Optional JSON list file: [{\"io_port\":...,\"options\":...}, ...]")
    ap.add_argument(
        "--runner-cmd",
        help=(
            "Optional command executed after each candidate write; command should deploy/reload/probe, "
            "and refresh --probe file for pass/fail check."
        ),
    )
    ap.add_argument("--runner-timeout", type=int, default=300)
    ap.add_argument("--report", help="Optional path to write JSON report")
    ap.add_argument(
        "--restore-on-fail",
        action="store_true",
        default=True,
        help="Restore original INI files when all candidates fail (default: true)",
    )
    ap.add_argument(
        "--no-restore-on-fail",
        dest="restore_on_fail",
        action="store_false",
        help="Keep last candidate in INI even when all candidates fail",
    )
    args = ap.parse_args()

    ini_paths = [Path(p) for p in args.ini]
    probe_path = Path(args.probe)
    candidate_file = Path(args.candidate_file) if args.candidate_file else None

    baseline = parse_probe_temperature_summary(probe_path)
    candidates = load_candidates(args.candidate, candidate_file)

    result: dict = {
        "ini_paths": [str(p) for p in ini_paths],
        "probe_path": str(probe_path),
        "section": args.section,
        "baseline_probe_summary": baseline,
        "candidates": candidates,
        "runner_cmd": args.runner_cmd,
        "decision": "NO_CHANGE",
        "attempts": [],
        "selected_candidate": None,
    }

    if not baseline["all_err"]:
        result["decision"] = "BASELINE_NOT_ALL_ERR"
        if args.report:
            Path(args.report).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    originals: dict[str, str] = {}
    for p in ini_paths:
        originals[str(p)] = p.read_text(encoding="utf-8", errors="ignore")

    for idx, cand in enumerate(candidates):
        per_ini: list[dict] = []
        total_changed = 0
        for p in ini_paths:
            ch = apply_candidate_to_ini(
                ini_path=p,
                io_port=str(cand["io_port"]),
                option=str(cand["options"]),
                section_name=args.section,
            )
            total_changed += int(ch.get("changed") or 0)
            per_ini.append({"ini_path": str(p), **ch})

        attempt: dict = {
            "index": idx,
            "candidate": cand,
            "ini_updates": per_ini,
            "total_changed": total_changed,
        }

        if not args.runner_cmd:
            result["decision"] = "CANDIDATE_PREPARED_NEEDS_VALIDATION"
            result["selected_candidate"] = cand
            result["attempts"].append(attempt)
            break

        cp = subprocess.run(
            args.runner_cmd,
            shell=True,
            text=True,
            capture_output=True,
            timeout=max(1, int(args.runner_timeout)),
        )
        attempt["runner"] = {
            "exit_code": cp.returncode,
            "stdout": (cp.stdout or "").strip(),
            "stderr": (cp.stderr or "").strip(),
        }
        if cp.returncode != 0:
            result["attempts"].append(attempt)
            result["decision"] = "RUNNER_ERROR"
            break

        now = parse_probe_temperature_summary(probe_path)
        attempt["probe_summary_after_run"] = now
        result["attempts"].append(attempt)

        if now.get("ok", 0) > 0 and not now.get("all_err", False):
            result["decision"] = "CANDIDATE_VALIDATED_PASS"
            result["selected_candidate"] = cand
            break

    if result["decision"] == "NO_CHANGE":
        result["decision"] = "NO_CANDIDATE_APPLIED"

    if result["decision"] not in ("CANDIDATE_VALIDATED_PASS", "CANDIDATE_PREPARED_NEEDS_VALIDATION"):
        if args.runner_cmd and result["decision"] != "RUNNER_ERROR":
            result["decision"] = "ALL_CANDIDATES_FAILED"
        if args.restore_on_fail:
            for p in ini_paths:
                p.write_text(originals[str(p)], encoding="utf-8")
            result["restored_original_ini"] = True
        else:
            result["restored_original_ini"] = False

    if args.report:
        Path(args.report).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
