#!/home/company2/AIagent_susi/.venv/bin/python
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

VALID_STATUS = {
    "FAN_ONE_TO_ONE_CONFIRMED",
    "FAN_ONE_TO_MANY_CONFIRMED",
    "FAN_PAIRING_NEEDS_FANCONTROL_EVIDENCE",
    "FAN_PAIRING_AMBIGUOUS",
}

KEY_RE = re.compile(r"^(FCPU2|FCPU|FSYS|FOEM\d+)$", re.I)
STATUS_RE = re.compile(
    r"FAN_ONE_TO_ONE_CONFIRMED|FAN_ONE_TO_MANY_CONFIRMED|FAN_PAIRING_NEEDS_FANCONTROL_EVIDENCE|FAN_PAIRING_AMBIGUOUS"
)


def _to_int(v: Any) -> int | None:
    if v is None:
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    s = str(v).strip()
    if not s:
        return None
    try:
        if s.lower().startswith("0x"):
            return int(s, 16)
        return int(s)
    except Exception:
        return None


def _norm_key(s: Any) -> str | None:
    t = str(s or "").strip().upper()
    if not t:
        return None
    if not KEY_RE.match(t):
        return None
    return t


def _guess_status_from_text(text: str) -> str:
    m = STATUS_RE.search(text or "")
    if m:
        return m.group(0)
    return "FAN_PAIRING_AMBIGUOUS"


def _normalize_item(it: dict[str, Any], fallback_key: str | None = None) -> dict[str, Any] | None:
    key = _norm_key(it.get("key") or it.get("item_name") or it.get("alias") or fallback_key)
    if not key:
        return None

    fanin_idx = _to_int(
        it.get("fanin_idx_candidate")
        if "fanin_idx_candidate" in it
        else it.get("fanin_idx")
    )
    control_idx = _to_int(
        it.get("control_idx_candidate")
        if "control_idx_candidate" in it
        else it.get("control_idx")
    )

    out: dict[str, Any] = {"key": key}
    if fanin_idx is not None:
        out["fanin_idx_candidate"] = fanin_idx
    if control_idx is not None:
        out["control_idx_candidate"] = control_idx

    return out


def _parse_text_items(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        km = re.search(r"\b(FCPU2|FCPU|FSYS|FOEM\d+)\b", line, re.I)
        if not km:
            continue
        key = km.group(1).upper()

        fm = re.search(r"(?:fanin_idx_candidate|fanin_idx|fanin)\s*[:=]\s*(0x[0-9a-fA-F]+|\d+)", line)
        cm = re.search(r"(?:control_idx_candidate|control_idx|control|pwm_idx)\s*[:=]\s*(0x[0-9a-fA-F]+|\d+)", line)
        fanin_raw = fm.group(1) if fm else None
        control_raw = cm.group(1) if cm else None

        if fanin_raw is None and control_raw is None:
            nums = re.findall(r"0x[0-9a-fA-F]+|\d+", line)
            if len(nums) >= 2:
                fanin_raw, control_raw = nums[0], nums[1]

        item = {
            "key": key,
            "fanin_idx_candidate": _to_int(fanin_raw),
            "control_idx_candidate": _to_int(control_raw),
            "evidence": line,
        }
        items.append(item)

    return items


def _normalize_from_json_obj(obj: Any, source_name: str) -> dict[str, Any]:
    status = None
    items: list[dict[str, Any]] = []

    if isinstance(obj, dict):
        status = (obj.get("topology_status") or obj.get("status") or "").strip() or None

        raw_items = obj.get("items")
        if isinstance(raw_items, list):
            for it in raw_items:
                if isinstance(it, dict):
                    n = _normalize_item(it)
                    if n:
                        items.append(n)
        elif isinstance(raw_items, dict):
            for k, v in raw_items.items():
                if isinstance(v, dict):
                    n = _normalize_item(v, fallback_key=k)
                    if n:
                        items.append(n)

        # fallback shape: dict keyed by FCPU/FSYS/FOEMx
        if not items:
            for k, v in obj.items():
                kk = _norm_key(k)
                if kk and isinstance(v, dict):
                    n = _normalize_item(v, fallback_key=kk)
                    if n:
                        items.append(n)

    elif isinstance(obj, list):
        for it in obj:
            if isinstance(it, dict):
                n = _normalize_item(it)
                if n:
                    items.append(n)

    if status not in VALID_STATUS:
        status = "FAN_PAIRING_AMBIGUOUS"

    dedup: dict[str, dict[str, Any]] = {}
    for it in items:
        dedup[it["key"]] = it
    items = [dedup[k] for k in sorted(dedup.keys())]

    payload: dict[str, Any] = {
        "topology_status": status,
        "items": items,
    }
    return payload


def _normalize_from_text(text: str, source_name: str) -> dict[str, Any]:
    status = _guess_status_from_text(text)
    items = _parse_text_items(text)

    dedup: dict[str, dict[str, Any]] = {}
    for it in items:
        dedup[it["key"]] = it

    return {
        "topology_status": status,
        "items": [dedup[k] for k in sorted(dedup.keys())],
    }


def _load_input(input_path: Path) -> tuple[str, str]:
    raw = input_path.read_text(encoding="utf-8")
    return raw, input_path.name


def _resolve_project_dir(root: Path, project: str) -> tuple[str, Path]:
    p = project.strip()
    exact = root / p
    if exact.is_dir():
        return exact.name, exact

    matches = [d for d in root.iterdir() if d.is_dir() and d.name.lower() == p.lower()]
    if len(matches) == 1:
        return matches[0].name, matches[0]
    if not matches:
        raise RuntimeError(f"Project not found under {root}: {project}")
    raise RuntimeError(f"Project name ambiguous under {root}: {project}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Normalize vision fan analysis to {PROJECT}-fan-pairing.json")
    parser.add_argument("--project", required=True, help="Project name (case-insensitive resolve under --root)")
    parser.add_argument("--root", default="/home/company2/AIagent_susi/CASES", help="CASES root")
    parser.add_argument("--input", required=True, help="Vision output file (.json or .txt)")
    parser.add_argument("--out", help="Output path; default CASES/<PROJECT>/<PROJECT>-fan-pairing.json")
    parser.add_argument("--status", help="Force topology_status override")
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    input_path = Path(args.input).expanduser().resolve()
    if not input_path.exists():
        raise RuntimeError(f"Input file not found: {input_path}")

    canonical_project, project_dir = _resolve_project_dir(root, args.project)
    out_path = Path(args.out).expanduser().resolve() if args.out else (project_dir / f"{canonical_project}-fan-pairing.json")

    raw, source_name = _load_input(input_path)

    payload: dict[str, Any]
    parsed_json = None
    try:
        parsed_json = json.loads(raw)
    except Exception:
        parsed_json = None

    if parsed_json is not None:
        payload = _normalize_from_json_obj(parsed_json, source_name)
    else:
        payload = _normalize_from_text(raw, source_name)

    if args.status:
        forced = args.status.strip()
        if forced not in VALID_STATUS:
            raise RuntimeError(f"Invalid forced status: {forced}")
        payload["topology_status"] = forced

    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Wrote: {out_path}")
    print(f"project={canonical_project}")
    print(f"topology_status={payload.get('topology_status')}")
    print(f"item_count={len(payload.get('items', []))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
