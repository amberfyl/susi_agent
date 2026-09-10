#!/usr/bin/env python3
import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def latest_file(folder: Path, pattern: str, exclude_prefix: str | None = None) -> Path | None:
    files = []
    for p in folder.glob(pattern):
        name = p.name.lower()
        if exclude_prefix and name.startswith(exclude_prefix.lower()):
            continue
        files.append(p)
    files = sorted(files, key=lambda p: p.stat().st_mtime)
    return files[-1] if files else None


def normalize_label(s: str) -> str:
    return " ".join((s or "").strip().upper().split())


@dataclass
class Cfg:
    fan_pct_tol: float = 0.15
    fan_abs_tol: float = 300.0
    zero_rpm_threshold: float = 30.0


def build_bios_fan_map(cache: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    for item in cache.get("items", []):
        if not isinstance(item, dict):
            continue
        if not str(item.get("filename", "")).lower().startswith("bios"):
            continue
        for v in item.get("fan_value_hints", []) or []:
            if not isinstance(v, dict):
                continue
            label = normalize_label(str(v.get("label", "")))
            value = v.get("value")
            if label and isinstance(value, (int, float)):
                out[label] = float(value)
    return out


def load_name_map(project_dir: Path, project: str) -> dict[str, str]:
    p = project_dir / f"{project}-fan-name-hints.json"
    if not p.exists():
        return {}
    data = load_json(p)
    by_key = data.get("by_key") if isinstance(data, dict) else {}
    if not isinstance(by_key, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in by_key.items():
        if not isinstance(v, str):
            continue
        out[normalize_label(v)] = str(k).strip().upper()
    # common aliases
    if "CPU FAN1 SPEED" not in out:
        out["CPU FAN1 SPEED"] = "FCPU"
    if "SYS FAN1 SPEED" not in out:
        out["SYS FAN1 SPEED"] = "FSYS"
    return out


def fan_baseline_from_report(report: dict) -> dict[str, float]:
    out: dict[str, float] = {}
    baseline = (((report or {}).get("metrics") or {}).get("baseline") or {})
    if not isinstance(baseline, dict):
        return out
    for key, node in baseline.items():
        if not isinstance(node, dict):
            continue
        stats = node.get("stats") or {}
        avg = stats.get("avg")
        if isinstance(avg, (int, float)):
            out[str(key).strip().upper()] = float(avg)
    return out


def fan_control_points(report: dict) -> dict[str, list[dict[str, float]]]:
    out: dict[str, list[dict[str, float]]] = {}
    cr = (((report or {}).get("metrics") or {}).get("control_readback") or {})
    if not isinstance(cr, dict):
        return out
    for key, arr in cr.items():
        if not isinstance(arr, list):
            continue
        k = str(key).strip().upper()
        pts = []
        for it in arr:
            if not isinstance(it, dict):
                continue
            pwm = it.get("target_pwm")
            avg = ((((it.get("rpm") or {}).get("stats") or {}).get("avg")))
            if isinstance(pwm, (int, float)) and isinstance(avg, (int, float)):
                pts.append({"pwm": float(pwm), "rpm_avg": float(avg)})
        if pts:
            out[k] = pts
    return out


def choose_report_value(bios_rpm: float, key: str, baseline: dict[str, float], control_pts: dict[str, list[dict[str, float]]], cfg: Cfg) -> tuple[float | None, str]:
    k = key.upper()
    b = baseline.get(k)
    if isinstance(b, (int, float)):
        # Prefer read baseline when it is plausible for pre-control screenshot.
        if bios_rpm <= cfg.zero_rpm_threshold:
            return float(b), "fan_baseline"
        if b > cfg.zero_rpm_threshold:
            return float(b), "fan_baseline"

    pts = control_pts.get(k, [])
    if pts:
        best = min(pts, key=lambda x: abs(x["rpm_avg"] - bios_rpm))
        return float(best["rpm_avg"]), f"control_readback_closest_pwm_{int(best['pwm'])}"

    if isinstance(b, (int, float)):
        return float(b), "fan_baseline"
    return None, "no_report_value"


def judge_fan(bios_rpm: float, report_rpm: float | None, key: str, no_fan_keys: set[str], cfg: Cfg) -> tuple[str, dict[str, Any]]:
    meta: dict[str, Any] = {}
    if report_rpm is None:
        return "N_A_NO_REPORT_VALUE", meta

    if key.upper() in no_fan_keys and bios_rpm <= cfg.zero_rpm_threshold and report_rpm <= cfg.zero_rpm_threshold:
        meta["note"] = "expected no-fan population"
        return "N_A_EXPECTED_NO_FAN", meta

    abs_delta = abs(report_rpm - bios_rpm)
    threshold = max(cfg.fan_abs_tol, abs(bios_rpm) * cfg.fan_pct_tol)
    meta["abs_delta"] = round(abs_delta, 3)
    meta["threshold"] = round(threshold, 3)
    meta["pct_tol"] = cfg.fan_pct_tol
    meta["abs_tol"] = cfg.fan_abs_tol
    return ("PASS" if abs_delta <= threshold else "FAIL"), meta


def main() -> int:
    ap = argparse.ArgumentParser(description="Compare BIOS fan values in bios-image-cache against verify_out reports.")
    ap.add_argument("--project", required=True)
    ap.add_argument("--cases-root", default="/home/company2/AIagent_susi/CASES")
    ap.add_argument("--verify-dir", default=None)
    ap.add_argument("--bios-cache", default=None)
    ap.add_argument("--no-fan-keys", default="", help="comma-separated, e.g. FSYS")
    ap.add_argument("--fan-pct-tol", type=float, default=0.15)
    ap.add_argument("--fan-abs-tol", type=float, default=300.0)
    ap.add_argument("--zero-rpm-threshold", type=float, default=30.0)
    ap.add_argument("--out", default=None)
    args = ap.parse_args()

    project_dir = Path(args.cases_root) / args.project
    verify_dir = Path(args.verify_dir) if args.verify_dir else project_dir / "verify_out"
    bios_cache = Path(args.bios_cache) if args.bios_cache else project_dir / f"{args.project}-bios-image-cache.json"

    if not bios_cache.exists():
        raise SystemExit(f"bios cache not found: {bios_cache}")
    if not verify_dir.exists():
        raise SystemExit(f"verify dir not found: {verify_dir}")

    fan_report_path = latest_file(verify_dir, "hwm_fan_*.json", exclude_prefix="hwm_fan_control_")
    control_report_path = latest_file(verify_dir, "hwm_fan_control_*.json")

    fan_report = load_json(fan_report_path) if fan_report_path else {}
    control_report = load_json(control_report_path) if control_report_path else {}
    cache = load_json(bios_cache)

    bios_fan = build_bios_fan_map(cache)
    label_to_key = load_name_map(project_dir, args.project)

    baseline = fan_baseline_from_report(fan_report)
    control_pts = fan_control_points(control_report)

    cfg = Cfg(args.fan_pct_tol, args.fan_abs_tol, args.zero_rpm_threshold)
    no_fan_keys = {x.strip().upper() for x in args.no_fan_keys.split(",") if x.strip()}

    items = []
    verdicts = []
    for bios_label, bios_rpm in sorted(bios_fan.items()):
        fan_key = label_to_key.get(normalize_label(bios_label), "")
        report_rpm, source = choose_report_value(bios_rpm, fan_key, baseline, control_pts, cfg) if fan_key else (None, "no_key_mapping")
        status, meta = judge_fan(bios_rpm, report_rpm, fan_key, no_fan_keys, cfg) if fan_key else ("FAIL_MAPPING", {})
        verdicts.append(status)
        items.append({
            "bios_label": bios_label,
            "fan_key": fan_key,
            "bios_rpm": bios_rpm,
            "report_rpm": report_rpm,
            "report_source": source,
            "status": status,
            "meta": meta,
        })

    if any(v == "FAIL" or v == "FAIL_MAPPING" for v in verdicts):
        overall = "FAIL"
    elif any(v.startswith("N_A") for v in verdicts):
        overall = "CONDITIONAL"
    else:
        overall = "PASS"

    summary = {
        "project": args.project,
        "overall": overall,
        "policy": {
            "fan_pct_tol": args.fan_pct_tol,
            "fan_abs_tol": args.fan_abs_tol,
            "zero_rpm_threshold": args.zero_rpm_threshold,
            "no_fan_keys": sorted(no_fan_keys),
            "bios_capture_mode": "pre_control",
        },
        "sources": {
            "bios_cache": str(bios_cache),
            "fan_report": str(fan_report_path) if fan_report_path else None,
            "fan_control_report": str(control_report_path) if control_report_path else None,
        },
        "verify_functional": {
            "fan_result": fan_report.get("result"),
            "fan_reason": fan_report.get("reason"),
            "fan_control_result": control_report.get("result"),
            "fan_control_reason": control_report.get("reason"),
        },
        "fan_compare": items,
        "notes": [
            "Temperature/Voltage value-correlation is not compared here because verify_out fan reports do not include those channels.",
            "For pre-control BIOS screenshots, script prefers fan baseline when plausible; otherwise falls back to closest control-readback point.",
        ],
    }

    out_path = Path(args.out) if args.out else (verify_dir / f"{args.project}-bios-vs-verify-summary.json")
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(str(out_path))
    print(json.dumps({"overall": overall, "fan_items": len(items)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
