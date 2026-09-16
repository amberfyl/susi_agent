#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
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


def apply_option_fallback(
    ini_path: Path,
    from_option: str,
    to_option: str,
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
            if len(parts) >= 4 and parts[3].lower() == from_option.lower():
                parts[3] = to_option
                line = f"{key}={','.join(parts)}"
                changed += 1
                touched_keys.append(key.strip())

        out.append(line)

    if changed > 0:
        ini_path.write_text("\n".join(out) + "\n", encoding="utf-8")

    return {
        "changed": changed,
        "touched_keys": touched_keys,
    }


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fallback helper: if all HWM_TEMP_* are ERR, change option 0x80000001 -> 0x00000001 once."
    )
    ap.add_argument("--ini", required=True, help="Path to INI file")
    ap.add_argument("--probe", required=True, help="Path to probe report txt")
    ap.add_argument("--section", default="HWM.Temperature")
    ap.add_argument("--from-option", default="0x80000001")
    ap.add_argument("--to-option", default="0x00000001")
    ap.add_argument("--report", help="Optional path to write JSON report")
    args = ap.parse_args()

    ini_path = Path(args.ini)
    probe_path = Path(args.probe)

    probe = parse_probe_temperature_summary(probe_path)
    result = {
        "ini_path": str(ini_path),
        "probe_path": str(probe_path),
        "probe_summary": probe,
        "decision": "NO_CHANGE",
        "fallback": {
            "section": args.section,
            "from_option": args.from_option,
            "to_option": args.to_option,
            "changed": 0,
            "touched_keys": [],
        },
    }

    if probe["all_err"]:
        ch = apply_option_fallback(
            ini_path=ini_path,
            from_option=args.from_option,
            to_option=args.to_option,
            section_name=args.section,
        )
        result["decision"] = "RETRY_WITH_OPTION_FALLBACK" if ch["changed"] > 0 else "ALL_ERR_BUT_NO_MATCHING_OPTION"
        result["fallback"].update(ch)

    if args.report:
        Path(args.report).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
