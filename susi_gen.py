import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from extract_pdf import extract_pdf_to_json, resolve_paths as resolve_extract_paths
from generate_ini import resolve_paths as resolve_generate_paths
from parse_probe import parse_probe_report
from query_config_db import query_section
from understand import understand


SPLIT_SECTIONS = [
    "SMBus", "I2C", "VGA.Backlight", "VGA.Brightness",
    "HWM.Voltage", "HWM.Current", "HWM.Temperature", "HWM.Fan",
    "HWM.Fan.Control", "HWM.CaseOpen", "WDT", "GPIO",
    "StorageArea", "ThermalProtect",
]


def _parse_sections_arg(raw: str | None) -> list[str] | None:
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    invalid = [p for p in parts if p not in SPLIT_SECTIONS]
    if invalid:
        raise ValueError(
            f"Invalid section(s): {', '.join(invalid)}. "
            f"Allowed: {', '.join(SPLIT_SECTIONS)}"
        )
    return parts


def _project_to_product_name(project: str) -> str:
    # SOM-6833 -> SOM, AIMB-205 -> AIMB
    token = project.strip().split("/")[-1].split("-")[0].upper()
    return token or project.upper()


def _load_spec_json(in_json_path: Path) -> dict | None:
    spec_path = in_json_path.parent / f"{in_json_path.stem.replace('-pre', '')}-spec.json"
    if not spec_path.exists():
        return None
    with open(spec_path, "r", encoding="utf-8") as f:
        return json.load(f)


def _infer_chip_name(spec: dict | None) -> str | None:
    if not spec:
        return None

    # 1) explicit chips.* values
    chips = spec.get("chips") if isinstance(spec, dict) else None
    if isinstance(chips, dict):
        for k in ("ec", "hwm", "gpio", "smbus", "i2c"):
            v = chips.get(k)
            if isinstance(v, str) and v.strip():
                m = re.search(r"(EIO-\d+|IT-\d+|IT\d+)", v.upper())
                if m:
                    x = m.group(1)
                    if x.startswith("IT") and not x.startswith("IT-") and x[2:].isdigit():
                        x = f"IT-{x[2:]}"
                    return x

    # 2) scan gpio pin chip hints
    gpio = spec.get("gpio") if isinstance(spec, dict) else None
    if isinstance(gpio, dict):
        for pin in gpio.get("pins", []) if isinstance(gpio.get("pins"), list) else []:
            chip = pin.get("chip") if isinstance(pin, dict) else None
            if isinstance(chip, str):
                m = re.search(r"(EIO-\d+|IT-\d+|IT\d+)", chip.upper())
                if m:
                    x = m.group(1)
                    if x.startswith("IT") and not x.startswith("IT-") and x[2:].isdigit():
                        x = f"IT-{x[2:]}"
                    return x

    return None


def _build_information_lines(project: str, probe_spec: dict | None, spec: dict | None) -> list[str]:
    bios = ""
    if probe_spec and probe_spec.get("bios_version"):
        bios = probe_spec["bios_version"]
    elif isinstance(spec, dict):
        info = spec.get("information")
        if isinstance(info, dict):
            bios = info.get("BIOSVersion") or ""

    # Probe-first policy: BOARD_PLATFORM_REV_VAL is the authoritative
    # PlatformVersion when the probe returned a value; fall back only when
    # the probe field is unavailable.
    platform = ""
    if probe_spec and probe_spec.get("platform_version") is not None:
        platform = str(probe_spec.get("platform_version") or "").strip()
    if not platform and isinstance(spec, dict):
        info = spec.get("information")
        if isinstance(info, dict):
            platform = str(info.get("PlatformVersion") or "").strip()
    if not platform and isinstance(spec, dict):
        platform = str(spec.get("platform") or spec.get("project") or "").strip()

    return [
        "[Information]",
        "IniVersion=1.0.1.0",
        f"PlatformVersion={platform}",
        f"BIOSVersion={bios}",
        "SusiAi=0",
        "FollowConfigure=1",
        "",
    ]


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_voltage_label_hints(text: str) -> list[str]:
    u = (text or "").upper()
    patterns = [
        r"HWM_VOLTAGE_[A-Z0-9_]+",
        r"\+12VSB|\+5VSB|\+3\.3VSB",
        r"\+12V|\+5V|\+3\.3V",
        r"VBAT|VCORE2?|VTT|5VSB|3VSB|12V|5V|3V3",
    ]
    hits: list[str] = []
    for p in patterns:
        hits.extend(re.findall(p, u))
    dedup: list[str] = []
    seen: set[str] = set()
    for x in hits:
        if x not in seen:
            seen.add(x)
            dedup.append(x)
    return dedup


def _build_bios_image_cache(project_dir: Path, project_name: str) -> Path:
    bios_pngs = sorted(project_dir.glob("bios*.png"))
    circuit_pngs = sorted(project_dir.glob("circuit*.png"))
    pngs = bios_pngs + [p for p in circuit_pngs if p not in bios_pngs]

    out = project_dir / f"{project_name}-bios-image-cache.json"

    old_by_sha: dict[str, dict] = {}
    if out.exists():
        try:
            old = _load_json(out)
            for it in old.get("items", []) if isinstance(old, dict) else []:
                if not isinstance(it, dict):
                    continue
                sha = (it.get("sha256") or "").strip()
                if sha:
                    old_by_sha[sha] = it
        except Exception:
            old_by_sha = {}

    items: list[dict] = []
    for p in pngs:
        st = p.stat()
        sha = _sha256_file(p)
        prev = old_by_sha.get(sha, {})

        analysis_text = prev.get("analysis_text") if isinstance(prev, dict) else ""
        hints = prev.get("voltage_label_hints") if isinstance(prev, dict) else None
        if not isinstance(hints, list):
            hints = _extract_voltage_label_hints(p.name)

        analysis_status = (prev.get("analysis_status") if isinstance(prev, dict) else None) or "PENDING_VISION_ANALYZE"
        label_hint_source = (prev.get("label_hint_source") if isinstance(prev, dict) else None) or (
            "vision_analyze" if analysis_status == "DONE_VISION_ANALYZE" else "filename_regex"
        )

        items.append({
            "path": str(p),
            "filename": p.name,
            "sha256": sha,
            "size": st.st_size,
            "mtime_utc": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
            "analysis_engine": "hermes_vision_analyze",
            "analysis_status": analysis_status,
            "analysis_text": analysis_text or "",
            "voltage_label_hints": hints,
            "label_hint_source": label_hint_source,
        })

    overall_status = "DONE" if items and all(i.get("analysis_status") == "DONE_VISION_ANALYZE" for i in items) else "PENDING"
    payload = {
        "project": project_name,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "image_glob": ["bios*.png", "circuit*.png"],
        "image_count": len(items),
        "analysis_engine": "hermes_vision_analyze",
        "analysis_status": overall_status,
        "items": items,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _load_json(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_ec_voltage_base_file(project_dir: Path, project_name: str, ec_query: dict) -> Path:
    """Item-2 contract: persist EC voltage base payload for downstream alias analysis."""
    hwid = ""
    prod_chip = ec_query.get("prod_chip") if isinstance(ec_query, dict) else None
    if isinstance(prod_chip, dict):
        hwid = (prod_chip.get("hardware_id") or "").strip()

    defaults = ec_query.get("defaults") if isinstance(ec_query, dict) else None
    if not isinstance(defaults, dict):
        defaults = {
            "io_port": "0",
            "options": "0x80000000",
            "resistor1": "0",
            "resistor2": "0",
            "offset": "0",
        }

    items: list[dict] = []
    for row in ec_query.get("rows") or []:
        if not isinstance(row, dict):
            continue
        report_name = (row.get("report_name") or "").strip()
        channel_id = (row.get("channel_id") or row.get("channel") or "").strip()
        items.append({
            "hwid": hwid,
            "report_name": report_name,
            "channel_id": channel_id,
            "defaults": {
                "io_port": defaults.get("io_port", "0"),
                "options": defaults.get("options", "0x80000000"),
                "resistor1": defaults.get("resistor1", "0"),
                "resistor2": defaults.get("resistor2", "0"),
                "offset": defaults.get("offset", "0"),
            },
            "raw_item": row,
        })

    payload = {
        "schema_version": "1.0",
        "project": project_name,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "query_key": ec_query.get("query_key") if isinstance(ec_query, dict) else None,
        "status": ec_query.get("status") if isinstance(ec_query, dict) else None,
        "row_count": len(items),
        "items": items,
        "missing_report_name_mappings": ec_query.get("missing_report_name_mappings", []),
    }

    out = project_dir / f"{project_name}-hwm-voltage-ec-base.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _pick_alias_from_bios_labels(report_name: str, labels: set[str]) -> tuple[str | None, str]:
    rn = (report_name or "").upper()

    def _canon(x: str) -> str:
        y = (x or "").upper().strip().replace(" ", "")
        y = y.replace("+V5SB", "+5VSB").replace("+V12", "+12V").replace("+V5", "+5V")
        y = y.replace("+V3.3", "+3.3V").replace("+VBAT", "VBAT")
        return y

    canon_labels = {_canon(x) for x in labels if isinstance(x, str)}
    has_5vsb = any(x in canon_labels for x in {"+5VSB", "5VSB", "5VSTANDBY", "+5VSTANDBY"})
    has_5v_plain = any(x in canon_labels for x in {"+5V", "5V"})
    has_12v = any(x in canon_labels for x in {"+12V", "12V"})
    has_33v = any(x in canon_labels for x in {"+3.3V", "3V3"})
    has_vbat = "VBAT" in canon_labels

    # 回填 Name 使用 BIOS 直接可讀標籤（例如 +12V/+5V/VBAT）
    if "VBAT" in rn and has_vbat:
        return "VBAT", "BIOS_LABEL_MATCH_VBAT"
    if "12V" in rn and has_12v:
        return "+12V", "BIOS_LABEL_MATCH_12V"
    if "3V" in rn and has_33v:
        return "+3.3V", "BIOS_LABEL_MATCH_3V3"
    if "5V" in rn:
        # 若 BIOS 同時出現 +5V 與 +5VSB，優先用 BIOS plain +5V 顯示名稱
        if has_5v_plain:
            return "+5V", "BIOS_LABEL_MATCH_5V_PLAIN"
        if has_5vsb:
            return "+5VSB", "BIOS_LABEL_MATCH_5VSB"

    return None, "NO_CONFIDENT_ALIAS"


def _build_voltage_alias_bridge(ec_base: dict, bios_cache: dict) -> dict:
    """Item-3 skill bridge: ec-base + bios-cache -> alias_map + unresolved (strict schema)."""
    bios_items = bios_cache.get("items") if isinstance(bios_cache, dict) else []
    if not isinstance(bios_items, list):
        bios_items = []

    image_label_map: dict[str, list[str]] = {}
    merged_labels: set[str] = set()
    for item in bios_items:
        if not isinstance(item, dict):
            continue
        image = item.get("filename") or item.get("path") or ""
        labels = []
        labels.extend(_extract_voltage_label_hints(item.get("analysis_text") or ""))
        hints = item.get("voltage_label_hints")
        if isinstance(hints, list):
            labels.extend([str(x).upper() for x in hints])
        labels = [x.upper() for x in labels if isinstance(x, str) and x.strip()]
        dedup: list[str] = []
        seen: set[str] = set()
        for x in labels:
            if x not in seen:
                seen.add(x)
                dedup.append(x)
                merged_labels.add(x)
        image_label_map[str(image)] = dedup

    def first_evidence_image(alias: str) -> str | None:
        targets = {
            "+5VSB": {"+5VSB", "5VSB", "5V STANDBY", "+5V STANDBY", "+V5SB"},
            "+5V": {"+5V", "5V", "+V5"},
            "+12V": {"+12V", "12V", "+V12"},
            "+3.3V": {"+3.3V", "3V3", "+V3.3"},
            "VBAT": {"VBAT", "+VBAT"},
        }.get(alias, set())
        for image, labels in image_label_map.items():
            if any(t in labels for t in targets):
                return image
        return None

    alias_map: list[dict] = []
    unresolved: list[dict] = []

    for item in ec_base.get("items") or []:
        if not isinstance(item, dict):
            continue
        report_name = str(item.get("report_name") or "")
        channel_id = str(item.get("channel_id") or "")
        alias, reason = _pick_alias_from_bios_labels(report_name, merged_labels)
        if alias:
            alias_map.append({
                "report_name": report_name,
                "channel_id": channel_id,
                "alias": alias,
                "confidence": "high",
                "reason": reason,
                "evidence_image": first_evidence_image(alias),
            })
        else:
            unresolved.append({
                "report_name": report_name,
                "channel_id": channel_id,
                "reason": reason,
            })

    payload = {
        "schema_version": "1.0",
        "project": ec_base.get("project") or bios_cache.get("project") or "",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inputs": {
            "ec_base_row_count": len(ec_base.get("items") or []),
            "bios_image_count": len(bios_items),
        },
        "alias_map": alias_map,
        "unresolved": unresolved,
    }

    # strict schema check (shape only)
    required_top = {"schema_version", "project", "generated_at_utc", "inputs", "alias_map", "unresolved"}
    if set(payload.keys()) != required_top:
        raise RuntimeError("voltage-alias-bridge schema mismatch")
    return payload


def _write_voltage_alias_bridge_file(project_dir: Path, project_name: str,
                                     ec_base_path: Path, bios_cache_path: Path) -> Path:
    ec_base = _load_json(ec_base_path)
    bios_cache = _load_json(bios_cache_path)
    bridge = _build_voltage_alias_bridge(ec_base, bios_cache)
    out = project_dir / f"{project_name}-voltage-alias-bridge.json"
    out.write_text(json.dumps(bridge, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def _merge_voltage_alias_into_rows(query_result: dict, alias_bridge_path: Path | None) -> dict:
    """Item-4: merge alias_map back into HWM.Voltage row disp_name(Name field).

    Also keep item key (left side) semantically aligned with display alias.
    Example: alias '+5V' should output item key 'V50' (not 'V5SB').
    """
    if not alias_bridge_path:
        return query_result
    rows = query_result.get("rows")
    if not isinstance(rows, list) or not rows:
        return query_result

    try:
        bridge = _load_json(alias_bridge_path)
    except Exception:
        return query_result

    alias_map = bridge.get("alias_map") if isinstance(bridge, dict) else []
    if not isinstance(alias_map, list) or not alias_map:
        return query_result

    by_key: dict[tuple[str, str], str] = {}
    by_report: dict[str, str] = {}
    for item in alias_map:
        if not isinstance(item, dict):
            continue
        alias = str(item.get("alias") or "").strip()
        confidence = str(item.get("confidence") or "").lower().strip()
        if not alias:
            continue
        if confidence and confidence != "high":
            # low-confidence alias must not fill Name.
            continue

        report_name = str(item.get("report_name") or "").strip()
        channel_id = str(item.get("channel_id") or "").strip()
        if report_name and channel_id:
            by_key[(report_name, channel_id)] = alias
        if report_name:
            by_report[report_name] = alias

    if not by_key and not by_report:
        return query_result

    alias_to_item_key = {
        "+12V": "V120",
        "+5V": "V50",
        "+5VSB": "V5SB",
        "+3.3V": "V33",
        "VBAT": "VBAT",
    }

    merged = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        report_name = str(row.get("report_name") or "").strip()
        channel_id = str(row.get("channel_id") or row.get("channel") or "").strip()

        alias = by_key.get((report_name, channel_id))
        if alias is None:
            alias = by_report.get(report_name)
        if alias:
            row["disp_name"] = alias
            mapped_key = alias_to_item_key.get(alias)
            if mapped_key:
                row["item_name"] = mapped_key
            merged += 1

    query_result["alias_merged_count"] = merged
    query_result["alias_bridge_path"] = str(alias_bridge_path)
    return query_result


def _canonical_voltage_item_name(report_name: str, fallback: str = "") -> str:
    """Map report_name to conventional HWM.Voltage item key (left side of '=') when possible."""
    rn = (report_name or "").upper().strip()
    conventional = {
        "VCORE", "VCORE2", "V25", "V33", "V50", "V120", "V5SB", "V3SB", "VBAT",
        "VN50", "VN120", "VTT", "V240", "DC", "DCSTBY", "VBATLI", "V15", "V18",
        "V105", "VOEM0", "VOEM1", "VOEM2", "VOEM3",
    }
    direct_map = {
        "HWM_VOLTAGE_12V": "V120",
        "HWM_VOLTAGE_5V": "V50",
        "HWM_VOLTAGE_5VSB": "V5SB",
        "HWM_VOLTAGE_3V3": "V33",
        "HWM_VOLTAGE_3VSB": "V3SB",
        "HWM_VOLTAGE_VBAT": "VBAT",
        "HWM_VOLTAGE_VCORE": "VCORE",
        "HWM_VOLTAGE_VCORE2": "VCORE2",
        "HWM_VOLTAGE_V25": "V25",
        "HWM_VOLTAGE_VTT": "VTT",
        "HWM_VOLTAGE_V240": "V240",
        "HWM_VOLTAGE_DC": "DC",
        "HWM_VOLTAGE_DCSTBY": "DCSTBY",
        "HWM_VOLTAGE_VBATLI": "VBATLI",
        "HWM_VOLTAGE_V15": "V15",
        "HWM_VOLTAGE_V18": "V18",
        "HWM_VOLTAGE_V105": "V105",
        "HWM_VOLTAGE_VOEM0": "VOEM0",
        "HWM_VOLTAGE_VOEM1": "VOEM1",
        "HWM_VOLTAGE_VOEM2": "VOEM2",
        "HWM_VOLTAGE_VOEM3": "VOEM3",
        "HWM_VOLTAGE_N5V": "VN50",
        "HWM_VOLTAGE_N12V": "VN120",
    }
    if rn in direct_map:
        return direct_map[rn]

    # fallback: if suffix already equals a conventional key, use it directly
    suffix = rn.replace("HWM_VOLTAGE_", "") if rn.startswith("HWM_VOLTAGE_") else rn
    if suffix in conventional:
        return suffix

    fb = (fallback or "").strip().upper()
    return fb if fb else fallback


def _probe_ok_voltage_report_names(probe_path: Path | None) -> list[str]:
    if not probe_path or not probe_path.exists():
        return []

    names: list[str] = []
    seen: set[str] = set()
    in_voltage_block = False

    with open(probe_path, "r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("==="):
                in_voltage_block = ("HWM Voltage" in line)
                continue
            if not in_voltage_block:
                continue

            # Example: [OK]  HWM_VOLTAGE_12V  Id=... Value=11880 mV
            m = re.match(r"^\[OK\]\s+(HWM_VOLTAGE_[A-Z0-9_]+)\b.*\bValue=", line)
            if not m:
                continue
            report_name = m.group(1)
            if report_name not in seen:
                seen.add(report_name)
                names.append(report_name)

    return names


def _probe_ok_temperature_report_names(probe_path: Path | None) -> list[str]:
    if not probe_path or not probe_path.exists():
        return []

    names: list[str] = []
    seen: set[str] = set()
    in_temp_block = False

    with open(probe_path, "r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("==="):
                in_temp_block = ("HWM Temperature" in line)
                continue
            if not in_temp_block:
                continue

            # Example: [OK]  HWM_TEMP_CPU  Id=... Value=3779 (=104.8 C)
            m = re.match(r"^\[OK\]\s+(HWM_TEMP_[A-Z0-9_]+)\b.*\bValue=", line)
            if not m:
                continue
            report_name = m.group(1)
            if report_name not in seen:
                seen.add(report_name)
                names.append(report_name)

    return names


def _load_temperature_defaults(db_path: Path) -> dict[str, str]:
    defaults: dict[str, str] = {
        "io_port": "0",
        "options": "0x80000000",
        "offset": "0",
        "disp_name": "",
    }

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute('SELECT name, value FROM "HWM.Temperature.Defaults"').fetchall()
        for r in rows:
            k = (r["name"] or "").strip()
            if k in defaults:
                defaults[k] = (r["value"] or "").strip()
    finally:
        con.close()

    return defaults


def _temp_item_name_from_report(report_name: str) -> str:
    rn = (report_name or "").upper().strip()
    direct_map = {
        "HWM_TEMP_CPU": "TCPU",
        "HWM_TEMP_CPU2": "TCPU2",
        "HWM_TEMP_SYSTEM": "TSYS",
        "HWM_TEMP_CHIPSET": "TCHIPSET",
        "HWM_TEMP_OEM0": "TOEM0",
        "HWM_TEMP_OEM1": "TOEM1",
        "HWM_TEMP_OEM2": "TOEM2",
        "HWM_TEMP_OEM3": "TOEM3",
        "HWM_TEMP_OEM4": "TOEM4",
        "HWM_TEMP_OEM5": "TOEM5",
        "HWM_TEMP_OEM6": "TOEM6",
        "HWM_TEMP_GRAPHIC": "GRAPHIC",
    }
    if rn in direct_map:
        return direct_map[rn]
    suffix = rn.replace("HWM_TEMP_", "") if rn.startswith("HWM_TEMP_") else rn
    return suffix


def _spec_temperature_alias_map(spec: dict | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(spec, dict):
        return out
    temps = spec.get("temperatures")
    if not isinstance(temps, list):
        return out
    for it in temps:
        if not isinstance(it, dict):
            continue
        key = str(it.get("ini_key") or "").strip().upper()
        label = str(it.get("label") or "").strip()
        if key and label:
            out[key] = label
    return out


def _load_project_temperature_name_hints(project_dir: Path, project_name: str) -> dict[str, str]:
    """Load explicit BIOS-derived temperature display names for this project."""
    path = project_dir / f"{project_name}-temperature-name-hints.json"
    if not path.exists():
        return {}
    try:
        data = _load_json(path)
    except Exception:
        return {}
    by_key = data.get("by_key") if isinstance(data, dict) else None
    if not isinstance(by_key, dict):
        return {}
    return {str(k).strip().upper(): str(v).strip() for k, v in by_key.items() if str(k).strip() and str(v).strip()}


def _apply_temperature_name_hints(result: dict, hints: dict[str, str]) -> dict:
    """Apply explicit image-derived names without changing probe/DB channel data."""
    if result.get("status") != "FOUND" or not hints:
        return result
    for row in result.get("rows") or []:
        if isinstance(row, dict):
            key = str(row.get("item_name") or "").strip().upper()
            if key in hints:
                row["disp_name"] = hints[key]
    return result


def _build_hwm_temperature_query_result(db_path: Path, product_name: str, chip_name: str,
                                        probe_path: Path | None, spec: dict | None) -> dict:
    result: dict = {
        "query_key": {
            "product_name": product_name,
            "chip_name": chip_name,
            "section": "HWM.Temperature",
        },
        "status": None,
        "prod_chip": None,
        "rows": [],
        "source": "PROBE_CHANNELS_DB_DEFAULTS",
    }

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        prod = con.execute(
            """
            SELECT id, product_name, chip_name, hardware_id, config_chip
            FROM ProductChip
            WHERE product_name = ? AND chip_name = ?
            LIMIT 1
            """,
            (product_name, chip_name),
        ).fetchone()
        if prod is None:
            result["status"] = "NO_SUCH_PRODUCT_CHIP"
            result["row_count"] = 0
            return result

        result["prod_chip"] = dict(prod)
        defaults = _load_temperature_defaults(db_path)
        result["defaults"] = defaults
        alias_by_item = _spec_temperature_alias_map(spec)

        report_names = _probe_ok_temperature_report_names(probe_path)
        if not report_names:
            result["status"] = "SECTION_EMPTY"
            result["row_count"] = 0
            result["error"] = "NO_OK_HWM_TEMPERATURE_IN_PROBE"
            return result

        ph = ",".join(["?"] * len(report_names))
        ch_rows = con.execute(
            f'''SELECT report_name, channel_name, channel_id
                FROM "HWM.Temperature.Channels"
                WHERE report_name IN ({ph})
                ORDER BY id''',
            tuple(report_names),
        ).fetchall()
        ch_map = {r["report_name"]: dict(r) for r in ch_rows}

        out_rows: list[dict] = []
        missing_reports: list[str] = []
        for rn in report_names:
            c = ch_map.get(rn)
            if not c:
                missing_reports.append(rn)
                continue
            item_name = _temp_item_name_from_report(rn)
            channel_id = c.get("channel_id") or ""
            out_rows.append({
                "item_name": item_name,
                "channel": channel_id,
                "channel_id": channel_id,
                "io_port": defaults["io_port"],
                "option": defaults["options"],
                "offset": defaults["offset"],
                "disp_name": alias_by_item.get(item_name.upper(), defaults.get("disp_name", "")),
                "report_name": rn,
            })

        result["rows"] = out_rows
        result["row_count"] = len(out_rows)
        result["status"] = "FOUND" if out_rows else "SECTION_EMPTY"
        if missing_reports:
            result["missing_report_name_mappings"] = missing_reports
        return result
    finally:
        con.close()


def _load_voltage_defaults(db_path: Path) -> dict[str, str]:
    defaults: dict[str, str] = {
        "io_port": "0",
        "options": "0x80000000",
        "resistor1": "0",
        "resistor2": "0",
        "offset": "0",
    }

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute('SELECT name, value FROM "HWM.Voltage.Defaults"').fetchall()
        for r in rows:
            k = (r["name"] or "").strip()
            if k in defaults:
                defaults[k] = (r["value"] or "").strip()
    finally:
        con.close()

    return defaults


def _build_ec_voltage_query_result(db_path: Path, product_name: str, chip_name: str,
                                   probe_path: Path | None) -> dict:
    result: dict = {
        "query_key": {
            "product_name": product_name,
            "chip_name": chip_name,
            "section": "HWM.Voltage",
        },
        "status": None,
        "prod_chip": None,
        "rows": [],
        "source": "EC_PROBE_CHANNELS",
    }

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        prod = con.execute(
            """
            SELECT id, product_name, chip_name, hardware_id, config_chip
            FROM ProductChip
            WHERE product_name = ? AND chip_name = ?
            LIMIT 1
            """,
            (product_name, chip_name),
        ).fetchone()
        if prod is None:
            result["status"] = "NO_SUCH_PRODUCT_CHIP"
            result["row_count"] = 0
            return result

        result["prod_chip"] = dict(prod)
        defaults = _load_voltage_defaults(db_path)
        result["defaults"] = defaults

        report_names = _probe_ok_voltage_report_names(probe_path)
        if not report_names:
            result["status"] = "SECTION_EMPTY"
            result["row_count"] = 0
            result["error"] = "NO_OK_HWM_VOLTAGE_IN_PROBE"
            return result

        ph = ",".join(["?"] * len(report_names))
        ch_rows = con.execute(
            f'''SELECT report_name, channel_name, channel_id
                FROM "HWM.Voltage.Channels"
                WHERE report_name IN ({ph})
                ORDER BY id''',
            tuple(report_names),
        ).fetchall()

        ch_map = {r["report_name"]: dict(r) for r in ch_rows}

        out_rows: list[dict] = []
        missing_reports: list[str] = []
        for rn in report_names:
            c = ch_map.get(rn)
            if not c:
                missing_reports.append(rn)
                continue
            channel_id = c.get("channel_id") or ""
            out_rows.append({
                "item_name": _canonical_voltage_item_name(
                    rn,
                    c.get("channel_name") or rn.replace("HWM_VOLTAGE_", ""),
                ),
                "channel": channel_id,
                "channel_id": channel_id,
                "io_port": defaults["io_port"],
                "option": defaults["options"],
                "resistor1": defaults["resistor1"],
                "resistor2": defaults["resistor2"],
                "offset": defaults["offset"],
                "disp_name": "",
                "report_name": rn,
            })

        result["rows"] = out_rows
        result["row_count"] = len(out_rows)
        result["status"] = "FOUND" if out_rows else "SECTION_EMPTY"
        if missing_reports:
            result["missing_report_name_mappings"] = missing_reports
        return result
    finally:
        con.close()


def _render_section_lines(section: str, query_result: dict) -> list[str]:
    lines = [f"[{section}]"]
    rows = query_result.get("rows") or []
    hwid = ""
    prod_chip = query_result.get("prod_chip") or {}
    if isinstance(prod_chip, dict):
        hwid = prod_chip.get("hardware_id") or ""

    for row in rows:
        if not isinstance(row, dict):
            continue
        key = row.get("item_name") or f"ITEM{row.get('id', '')}"
        channel = row.get("channel") or ""
        io_port = row.get("io_port") or ""
        option = row.get("option") or ""
        disp_name = row.get("disp_name") or ""

        if section == "VGA.Brightness":
            # [Brightness]=[HW],[Channel],[IOPort/Address],[Option],[Max],[Min],[Frequency],[Name]
            # Temporary default for all Brightness entries: Max=100, Min=0, Frequency=0
            value = f"{hwid},{channel},{io_port},{option},100,0,0,"
            if disp_name:
                value += f"{disp_name}"
        elif section == "HWM.Voltage":
            # [Voltage]=[HW],[Channel],[IOPort/Address],[Option],[R1],[R2],[Name],[Offset]
            r1 = row.get("resistor1") or "0"
            r2 = row.get("resistor2") or "0"
            offset = row.get("offset") or "0"
            name = disp_name
            value = f"{hwid},{channel},{io_port},{option},{r1},{r2},{name},{offset}"
        elif section == "HWM.Temperature":
            # [Temp]=[HW],[Channel],[IOPort],[Option],[Temp Offset],[Name]
            offset = row.get("offset") or "0"
            value = f"{hwid},{channel},{io_port},{option},{offset},"
            if disp_name:
                value += f"{disp_name}"
        elif section == "HWM.Fan":
            # [Fan]=[HW],[Channel],[IOPort],[Option],[Offset],[Name]
            offset = row.get("offset") or "0"
            value = f"{hwid},{channel},{io_port},{option},{offset},"
            if disp_name:
                value += f"{disp_name}"
        elif section == "HWM.Fan.Control":
            # [Fan.Control]=[HW],[Channel],[IOPort],[Option],[Name]
            value = f"{hwid},{channel},{io_port},{option},"
            if disp_name:
                value += f"{disp_name}"
        elif section == "GPIO":
            # [GPIO]=[HW],[IOBase],[IOPort/Device Address],[Option],[Group],[Bit],[Name]
            group = row.get("group") or ""
            bit = row.get("bit") or ""
            value = f"{hwid},{channel},{io_port},{option},{group},{bit},"
            if disp_name:
                value += f"{disp_name}"
        else:
            value = f"{hwid},{channel},{io_port},{option}"
            if disp_name:
                value += f",{disp_name}"

        lines.append(f"{key}={value}")

    lines.append("")
    return lines


def _norm_chip_name(chip_name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (chip_name or "").upper())


def _is_r014_r015_target(product_name: str, chip_name: str) -> str | None:
    p = (product_name or "").upper()
    c = _norm_chip_name(chip_name)
    if p == "AIMB" and c == "NCT6106D":
        return "R-014"
    if p == "AIMB" and c == "NCT6126D":
        return "R-015"
    return None


def _duplicate_channels(rows: list[dict]) -> list[str]:
    counts: dict[str, int] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        ch = (r.get("channel") or "").strip()
        if not ch:
            continue
        counts[ch] = counts.get(ch, 0) + 1
    return sorted([ch for ch, n in counts.items() if n > 1])


def _evaluate_non_ec_hwm_voltage(product_name: str, chip_name: str, rows: list[dict]) -> dict:
    """Return non-EC HWM.Voltage routing decision before diagram evidence is applied."""
    rule = _is_r014_r015_target(product_name, chip_name)
    dups = _duplicate_channels(rows)

    if rule:
        return {
            "route": "SUPERIO_DIAGRAM_RULE",
            "rule": rule,
            "pending": True,
            "status": f"PENDING_{rule.replace('-', '_')}_NEED_EVIDENCE",
            "reason": f"{rule} target requires circuit evidence before auto-correction",
            "duplicate_channels": dups,
        }

    if dups:
        return {
            "route": "SUPERIO_DUPLICATE_PENDING",
            "rule": None,
            "pending": True,
            "status": "PENDING_CHANNEL_DUPLICATE_NEED_EVIDENCE",
            "reason": "Duplicate channel_id detected in HWM.Voltage; no diagram evidence provided",
            "duplicate_channels": dups,
        }

    return {
        "route": "SUPERIO_BASELINE",
        "rule": None,
        "pending": False,
        "status": "FOUND",
        "reason": None,
        "duplicate_channels": [],
    }


def _parse_int_value(v: object) -> int | None:
    if v is None:
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


def _format_channel_like(template: str, value: int) -> str:
    t = (template or "").strip()
    if t.lower().startswith("0x"):
        width = max(1, len(t) - 2)
        return f"0x{value:0{width}X}"
    return str(value)


def _load_prod_chip_record(db_path: Path, product_name: str, chip_name: str) -> dict | None:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        prod = con.execute(
            """
            SELECT id, product_name, chip_name, hardware_id, config_chip
            FROM ProductChip
            WHERE product_name = ? AND chip_name = ?
            LIMIT 1
            """,
            (product_name, chip_name),
        ).fetchone()
        return dict(prod) if prod is not None else None
    finally:
        con.close()


def _fan_alias_from_key(key: str) -> str:
    k = (key or "").upper().strip()
    if k == "FCPU":
        return "CPU Fan"
    if k == "FCPU2":
        return "CPU2 Fan"
    if k == "FSYS":
        return "System Fan"
    m = re.match(r"^FOEM(\d+)$", k)
    if m:
        return f"OEM Fan {int(m.group(1))}"
    return k


def _load_fan_name_hints_from_bios_cache(cache_path: Path) -> dict:
    """Extract BIOS fan labels from vision_analyze cache for name backfill."""
    out = {"by_key": {}, "by_idx": {}}
    if not cache_path.exists():
        return out
    try:
        data = _load_json(cache_path)
    except Exception:
        return out

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return out

    merged_texts: list[str] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        fn = str(it.get("filename") or "")
        if not fn.lower().startswith("bios"):
            continue
        t = str(it.get("analysis_text") or "").strip()
        if t:
            merged_texts.append(t)

    text = "\n".join(merged_texts)
    u = text.upper()

    has_com = bool(re.search(r"\bCOM\s*MODULE\s*FAN\b|\bSMART\s*FAN\s*[-:]?\s*COM\s*MODULE\b", u))
    has_carrier = bool(re.search(r"\bCARRIER\s*BOARD\s*FAN\b|\bSMART\s*FAN\s*[-:]?\s*CARRIER\s*BOARD\b", u))

    if has_com:
        out["by_key"]["FCPU"] = "COM Module FAN"
        out["by_idx"][0] = "COM Module FAN"
    if has_carrier:
        out["by_key"]["FSYS"] = "Carrier Board FAN"
        out["by_idx"][1] = "Carrier Board FAN"

    if not out["by_key"]:
        if re.search(r"\bCPU\s*FAN\b", u):
            out["by_key"]["FCPU"] = "CPU Fan"
            out["by_idx"][0] = "CPU Fan"
        if re.search(r"\bSYSTEM\s*FAN\b|\bSYS\s*FAN\b", u):
            out["by_key"]["FSYS"] = "System Fan"
            out["by_idx"][1] = "System Fan"

    return out


def _load_project_fan_name_hints(project_dir: Path, project_name: str, bios_cache_path: Path) -> dict:
    """Priority: explicit project hints file > BIOS cache extraction."""
    out = _load_fan_name_hints_from_bios_cache(bios_cache_path)

    hints_path = project_dir / f"{project_name}-fan-name-hints.json"
    if not hints_path.exists():
        return out

    try:
        data = _load_json(hints_path)
    except Exception:
        return out

    if not isinstance(data, dict):
        return out

    by_key = data.get("by_key")
    if isinstance(by_key, dict):
        for k, v in by_key.items():
            kk = str(k or "").strip().upper()
            vv = str(v or "").strip()
            if kk and vv:
                out.setdefault("by_key", {})[kk] = vv

    by_idx = data.get("by_idx")
    if isinstance(by_idx, dict):
        for k, v in by_idx.items():
            try:
                ii = int(str(k).strip(), 0)
            except Exception:
                continue
            vv = str(v or "").strip()
            if vv:
                out.setdefault("by_idx", {})[ii] = vv

    return out


def _resolve_fan_disp_name(key: str, idx: int | None, fan_name_hints: dict | None) -> str:
    k = (key or "").upper().strip()
    if isinstance(fan_name_hints, dict):
        by_key = fan_name_hints.get("by_key")
        if isinstance(by_key, dict):
            v = by_key.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        if isinstance(idx, int):
            by_idx = fan_name_hints.get("by_idx")
            if isinstance(by_idx, dict):
                v = by_idx.get(idx)
                if isinstance(v, str) and v.strip():
                    return v.strip()
    return _fan_alias_from_key(k)


def _fan_conventional_idx(key: str) -> int | None:
    """
    Stable default mapping for common fan keys.
    Evidence-based pairing (fanin_idx_candidate/control_idx_candidate) still has higher priority.
    """
    k = (key or "").upper().strip()
    if k == "FCPU":
        return 0
    if k == "FSYS":
        return 1
    return None


def _next_free_idx(used: set[int], start: int = 0) -> int:
    i = max(0, int(start))
    while i in used:
        i += 1
    return i


def _extract_first_json_block(text: str) -> dict | None:
    s = (text or "").strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        pass

    m = re.search(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", s, re.I)
    if m:
        try:
            obj = json.loads(m.group(1))
            return obj if isinstance(obj, dict) else None
        except Exception:
            pass

    start = s.find("{")
    end = s.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(s[start:end + 1])
            return obj if isinstance(obj, dict) else None
        except Exception:
            return None
    return None


def _ensure_gpio_vision_images(project_dir: Path, project_name: str) -> list[Path]:
    pngs = sorted(project_dir.glob("circuit*.png")) + sorted(project_dir.glob("bios*.png"))
    if pngs:
        return pngs

    pdfs = sorted(project_dir.glob("circuit*.pdf"))
    if not pdfs:
        return []

    out_dir = project_dir / "_ai_gpio_pages"
    out_dir.mkdir(parents=True, exist_ok=True)

    rendered: list[Path] = []
    try:
        import fitz  # PyMuPDF
    except Exception:
        return []

    for pdf in pdfs:
        try:
            doc = fitz.open(str(pdf))
        except Exception:
            continue
        try:
            # 限制頁數避免自動流程過重；GPIO 通常在前幾頁或獨立頁。
            max_pages = min(len(doc), 4)
            for i in range(max_pages):
                page = doc.load_page(i)
                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                out = out_dir / f"{project_name}-{pdf.stem}-p{i+1}.png"
                pix.save(str(out))
                rendered.append(out)
        finally:
            doc.close()

    return rendered


def _auto_generate_gpio_trace(project_dir: Path, project_name: str) -> Path | None:
    out_path = project_dir / f"{project_name}-gpio-trace.json"
    images = _ensure_gpio_vision_images(project_dir, project_name)
    if not images:
        return None

    merged_items: list[dict] = []
    ambiguous = 0

    for img in images:
        prompt = (
            "你是電路圖 GPIO 追線分析器。"
            "請使用 vision_analyze 分析這張圖，僅依可見線路與文字，不可猜測。"
            "輸出必須是單一 JSON 物件，不要 markdown："
            "{\"items\":[{\"report_name\":\"GPIO00\",\"signal\":\"EC_P1_GPIO0\",\"function_label\":\"GPIOB0\",\"group\":1,\"bit\":0,\"package_pin\":\"F1\",\"status\":\"CONFIRMED|AMBIGUOUS\",\"evidence\":\"...\",\"name\":\"\"}],\"notes\":\"...\"}。"
            "規則：1) 必須是實際 wire trace；2) 無法確定就用 AMBIGUOUS；3) 不要輸出 FAN/BEEP。"
            f" 圖片路徑：{img}"
        )
        try:
            proc = subprocess.run(
                ["hermes", "-z", prompt, "-t", "vision"],
                text=True,
                capture_output=True,
                check=False,
            )
        except Exception:
            continue

        if proc.returncode != 0:
            continue

        obj = _extract_first_json_block(proc.stdout)
        if not isinstance(obj, dict):
            continue

        items = obj.get("items")
        if not isinstance(items, list):
            continue

        for it in items:
            if not isinstance(it, dict):
                continue
            status = str(it.get("status") or "AMBIGUOUS").strip().upper()
            if status != "CONFIRMED":
                ambiguous += 1
            item = {
                "report_name": str(it.get("report_name") or "").strip().upper(),
                "signal": str(it.get("signal") or "").strip(),
                "function_label": str(it.get("function_label") or "").strip(),
                "group": _parse_int_value(it.get("group")),
                "bit": _parse_int_value(it.get("bit")),
                "package_pin": str(it.get("package_pin") or "").strip(),
                "status": "CONFIRMED" if status == "CONFIRMED" else "AMBIGUOUS",
                "evidence": str(it.get("evidence") or f"vision:{img.name}").strip(),
                "name": str(it.get("name") or "").strip(),
            }
            merged_items.append(item)

    dedup: dict[tuple[str, str, str], dict] = {}
    for it in merged_items:
        key = (
            (it.get("report_name") or "").upper().strip(),
            (it.get("signal") or "").upper().strip(),
            (it.get("function_label") or "").upper().strip(),
        )
        prev = dedup.get(key)
        if prev is None:
            dedup[key] = it
            continue
        if prev.get("status") != "CONFIRMED" and it.get("status") == "CONFIRMED":
            dedup[key] = it

    items_out = list(dedup.values())
    confirmed_cnt = sum(1 for x in items_out if x.get("status") == "CONFIRMED" and x.get("group") is not None and x.get("bit") is not None)

    topology_status = "GPIO_TRACE_AMBIGUOUS"
    if confirmed_cnt > 0:
        topology_status = "GPIO_TRACE_CONFIRMED"

    payload = {
        "topology_status": topology_status,
        "items": items_out,
        "meta": {
            "source": "hermes_vision_analyze",
            "image_count": len(images),
            "ambiguous_count": ambiguous,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        },
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def _load_gpio_trace_result(project_dir: Path, project_name: str) -> dict | None:
    candidates = [
        project_dir / f"{project_name}-gpio-trace.json",
        project_dir / f"{project_name}-gpio-analysis.json",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            data = _load_json(p)
            if isinstance(data, dict):
                data["_source_path"] = str(p)
                return data
        except Exception:
            continue
    return None


def _normalize_gpio_trace_result(raw: dict | None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    status = (raw.get("topology_status") or raw.get("status") or "").strip()
    if not status:
        status = "GPIO_TRACE_UNKNOWN"

    items_raw = raw.get("items")
    if not isinstance(items_raw, list):
        items_raw = []

    items: list[dict] = []
    for i, it in enumerate(items_raw):
        if not isinstance(it, dict):
            continue
        group = _parse_int_value(it.get("group"))
        bit = _parse_int_value(it.get("bit"))
        item_status = str(it.get("status") or "CONFIRMED").strip().upper()
        if group is None or bit is None:
            if item_status == "CONFIRMED":
                item_status = "AMBIGUOUS"

        report_name = str(it.get("report_name") or "").strip().upper()
        if not report_name:
            report_name = f"GPIO{i:02d}"

        items.append({
            "report_name": report_name,
            "signal": str(it.get("signal") or "").strip(),
            "function_label": str(it.get("function_label") or "").strip(),
            "group": group,
            "bit": bit,
            "package_pin": str(it.get("package_pin") or "").strip(),
            "status": item_status,
            "evidence": str(it.get("evidence") or "").strip(),
            "name": str(it.get("name") or "").strip(),
        })

    return {
        "status": status,
        "items": items,
        "source_path": raw.get("_source_path"),
    }


def _load_gpio_defaults(db_path: Path) -> dict:
    out = {
        "base_addr": "0",
        "io_port": "0",
        "options": "0xA0000003",
        "disp_name": "",
    }
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT name, value FROM 'GPIO.Defaults'").fetchall()
        for r in rows:
            name = str(r["name"] or "").strip()
            if name in out:
                out[name] = str(r["value"] or "").strip()
        return out
    except Exception:
        return out
    finally:
        con.close()


def _resolve_gpio_expected_count(spec: dict | None) -> int | None:
    if not isinstance(spec, dict):
        return None
    gpio = spec.get("gpio")
    if not isinstance(gpio, dict):
        return None

    c = _parse_int_value(gpio.get("count"))
    if isinstance(c, int) and c > 0:
        return c

    pins = gpio.get("pins")
    if isinstance(pins, list) and len(pins) > 0:
        return len(pins)
    return None


def _load_gpio_group_pins(db_path: Path) -> list[dict]:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            "SELECT report_name, `group`, pin FROM 'GPIO.GroupPins' ORDER BY id"
        ).fetchall()
    except Exception:
        rows = []
    finally:
        con.close()

    out: list[dict] = []
    for r in rows:
        out.append({
            "report_name": str(r["report_name"] or "").strip().upper(),
            "group": str(r["group"] or "").strip(),
            "pin": str(r["pin"] or "").strip(),
        })
    return out


def _build_gpio_query_result(db_path: Path, product_name: str, chip_name: str,
                             spec: dict | None) -> tuple[dict, dict]:
    result: dict = {
        "query_key": {
            "product_name": product_name,
            "chip_name": chip_name,
            "section": "GPIO",
        },
        "status": None,
        "prod_chip": None,
        "rows": [],
        "source": "GPIO_GROUPPINS_BY_SPEC_COUNT",
    }
    decision = {
        "route": "GPIO_GROUPPINS",
        "status": "FOUND",
        "pending": False,
        "reason": None,
        "gpio_expected_count": None,
        "gpio_group_pins_total": 0,
        "gpio_trimmed_count": 0,
    }

    prod = _load_prod_chip_record(db_path, product_name, chip_name)
    if prod is None:
        result["status"] = "NO_SUCH_PRODUCT_CHIP"
        result["row_count"] = 0
        decision["pending"] = True
        decision["status"] = "NO_SUCH_PRODUCT_CHIP"
        decision["reason"] = "Missing ProductChip record"
        return result, decision
    result["prod_chip"] = prod

    defaults = _load_gpio_defaults(db_path)
    gp_rows = _load_gpio_group_pins(db_path)
    decision["gpio_group_pins_total"] = len(gp_rows)

    if not gp_rows:
        result["status"] = "SECTION_EMPTY"
        result["row_count"] = 0
        decision["pending"] = True
        decision["status"] = "GPIO_GROUPPINS_EMPTY"
        decision["reason"] = "GPIO.GroupPins has no rows"
        return result, decision

    expected = _resolve_gpio_expected_count(spec)
    decision["gpio_expected_count"] = expected

    use_rows = gp_rows
    if isinstance(expected, int) and expected > 0 and len(gp_rows) > expected:
        use_rows = gp_rows[:expected]
        decision["gpio_trimmed_count"] = len(gp_rows) - len(use_rows)

    out_rows: list[dict] = []
    for idx, r in enumerate(use_rows):
        key = r.get("report_name") or f"GPIO{idx:02d}"
        out_rows.append({
            "item_name": key,
            "channel": defaults["base_addr"],
            "io_port": defaults["io_port"],
            "option": defaults["options"],
            "group": r.get("group"),
            "bit": r.get("pin"),
            "disp_name": defaults.get("disp_name", ""),
        })

    if not out_rows:
        result["status"] = "SECTION_EMPTY"
        result["row_count"] = 0
        decision["pending"] = True
        decision["status"] = "GPIO_ROWS_EMPTY"
        decision["reason"] = "No usable GPIO rows after count filter"
        return result, decision

    result["rows"] = out_rows
    result["row_count"] = len(out_rows)
    result["status"] = "FOUND"
    decision["reason"] = "Built GPIO from ProductChip HWID + GPIO.Defaults + GPIO.GroupPins (trimmed by spec gpio.count)"
    return result, decision


def _build_hwm_fan_query_result(db_path: Path, product_name: str, chip_name: str,
                                probe_spec: dict | None, fan_pairing: dict | None,
                                fan_name_hints: dict | None = None) -> dict:
    result: dict = {
        "query_key": {
            "product_name": product_name,
            "chip_name": chip_name,
            "section": "HWM.Fan",
        },
        "status": None,
        "prod_chip": None,
        "rows": [],
        "source": "PROBE_FAN_KEYS_AI_PAIRING",
    }

    prod = _load_prod_chip_record(db_path, product_name, chip_name)
    if prod is None:
        result["status"] = "NO_SUCH_PRODUCT_CHIP"
        result["row_count"] = 0
        return result
    result["prod_chip"] = prod

    probe_keys: list[str] = []
    if isinstance(probe_spec, dict):
        hwm = probe_spec.get("hwm")
        if isinstance(hwm, dict):
            fans = hwm.get("fans")
            if isinstance(fans, list):
                for f in fans:
                    k = str(f or "").strip().upper()
                    if k and k not in probe_keys:
                        probe_keys.append(k)

    by_key = fan_pairing.get("by_key") if isinstance(fan_pairing, dict) else None
    if not probe_keys and isinstance(by_key, dict):
        probe_keys = sorted([str(k).strip().upper() for k in by_key.keys() if str(k).strip()])

    if not probe_keys:
        result["status"] = "SECTION_EMPTY"
        result["row_count"] = 0
        result["error"] = "NO_FAN_KEYS_FROM_PROBE_OR_PAIRING"
        return result

    out_rows: list[dict] = []
    used_idx: set[int] = set()
    by_key = fan_pairing.get("by_key") if isinstance(fan_pairing, dict) else None

    # Pass 1: reserve evidence-based fanin_idx from pairing (highest priority)
    idx_by_key: dict[str, int] = {}
    for key in probe_keys:
        p = by_key.get(key) if isinstance(by_key, dict) else None
        fanin_idx = p.get("fanin_idx") if isinstance(p, dict) else None
        if isinstance(fanin_idx, int) and fanin_idx >= 0:
            idx_by_key[key] = fanin_idx
            used_idx.add(fanin_idx)

    # Pass 2: apply stable convention when slot is still empty
    for key in probe_keys:
        if key in idx_by_key:
            continue
        cidx = _fan_conventional_idx(key)
        if isinstance(cidx, int) and cidx not in used_idx:
            idx_by_key[key] = cidx
            used_idx.add(cidx)

    # Pass 3: fill remaining keys with next free idx
    for key in probe_keys:
        if key in idx_by_key:
            continue
        idx = _next_free_idx(used_idx, 0)
        idx_by_key[key] = idx
        used_idx.add(idx)

    # Keep probe key order in rows; channel id comes from resolved idx map
    for key in probe_keys:
        idx = idx_by_key[key]
        out_rows.append({
            "item_name": key,
            "channel": f"0x{0x80000000 + idx:08X}",
            "io_port": "0x2E",
            "option": "0x80000000",
            "offset": "0",
            "disp_name": _resolve_fan_disp_name(key, idx, fan_name_hints),
        })

    result["rows"] = out_rows
    result["row_count"] = len(out_rows)
    result["status"] = "FOUND" if out_rows else "SECTION_EMPTY"
    return result


def _build_hwm_fan_control_query_result(fan_result: dict, fan_pairing: dict | None,
                                        fan_name_hints: dict | None = None) -> tuple[dict, dict]:
    result: dict = {
        "query_key": {
            **(fan_result.get("query_key") or {}),
            "section": "HWM.Fan.Control",
        },
        "status": None,
        "prod_chip": fan_result.get("prod_chip"),
        "rows": [],
        "source": "FAN_FROM_AI_PAIRING",
    }
    decision = {
        "route": "FAN_AI_PAIRING",
        "status": "FOUND",
        "pending": False,
        "reason": None,
        "pairing_status": None,
        "source_path": None,
        "overridden_count": 0,
    }

    fan_rows = fan_result.get("rows") or []
    if not fan_rows:
        result["status"] = "SECTION_EMPTY"
        result["row_count"] = 0
        decision["pending"] = True
        decision["status"] = "FAN_PAIRING_AMBIGUOUS"
        decision["reason"] = "HWM.Fan rows empty"
        return result, decision

    pairing_status = (fan_pairing.get("status") if isinstance(fan_pairing, dict) else "") or ""
    by_key = fan_pairing.get("by_key") if isinstance(fan_pairing, dict) else None
    decision["pairing_status"] = pairing_status if pairing_status else None
    decision["source_path"] = fan_pairing.get("source_path") if isinstance(fan_pairing, dict) else None

    if pairing_status in ("FAN_PAIRING_AMBIGUOUS", "FAN_PAIRING_NEEDS_FANCONTROL_EVIDENCE"):
        result["status"] = "SECTION_EMPTY"
        result["row_count"] = 0
        decision["pending"] = True
        decision["status"] = pairing_status
        decision["reason"] = "Fan pairing not confirmed"
        return result, decision

    fan_channel_by_key = _build_fan_channel_map(fan_rows)
    base_channel, template = _infer_fan_base_channel(fan_rows, [])
    if base_channel is None:
        result["status"] = "SECTION_EMPTY"
        result["row_count"] = 0
        decision["pending"] = True
        decision["status"] = "FAN_PAIRING_AMBIGUOUS"
        decision["reason"] = "Cannot infer base fan channel"
        return result, decision

    out_rows: list[dict] = []
    overridden = 0
    for pos, fr in enumerate(fan_rows):
        key = (fr.get("item_name") or "").strip()
        if not key:
            continue

        mapped = fan_channel_by_key.get(key)
        p = by_key.get(key) if isinstance(by_key, dict) else None
        control_idx = p.get("control_idx") if isinstance(p, dict) else None

        if pairing_status == "FAN_ONE_TO_MANY_CONFIRMED":
            if isinstance(control_idx, int):
                channel = _format_channel_like(template, base_channel + control_idx)
                overridden += 1
            elif mapped:
                channel = mapped
            else:
                channel = _format_channel_like(template, base_channel + pos)
        else:
            if mapped:
                channel = mapped
            elif isinstance(control_idx, int):
                channel = _format_channel_like(template, base_channel + control_idx)
                overridden += 1
            else:
                channel = _format_channel_like(template, base_channel + pos)

        out_rows.append({
            "item_name": key,
            "channel": channel,
            "io_port": "0x2E",
            "option": "0x20000000",
            "disp_name": (fr.get("disp_name") if isinstance(fr.get("disp_name"), str) and fr.get("disp_name").strip() else _resolve_fan_disp_name(key, control_idx if isinstance(control_idx, int) else pos, fan_name_hints)),
        })

    result["rows"] = out_rows
    result["row_count"] = len(out_rows)
    result["status"] = "FOUND" if out_rows else "SECTION_EMPTY"
    decision["overridden_count"] = overridden
    decision["reason"] = "Built HWM.Fan.Control from probe fan keys + AI pairing"
    return result, decision


def _load_fan_pairing_result(project_dir: Path, project_name: str) -> dict | None:
    candidates = [
        project_dir / f"{project_name}-fan-pairing.json",
        project_dir / f"{project_name}-fan-analysis.json",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            data = _load_json(p)
            if isinstance(data, dict):
                data["_source_path"] = str(p)
                return data
        except Exception:
            continue
    return None


def _normalize_fan_pairing_result(raw: dict | None) -> dict | None:
    if not isinstance(raw, dict):
        return None
    status = (raw.get("topology_status") or raw.get("status") or "").strip()
    if not status:
        return None

    by_key: dict[str, dict] = {}
    items = raw.get("items")
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            key = (it.get("key") or it.get("item_name") or it.get("alias") or "").strip()
            if not key:
                continue
            by_key[key] = {
                "fanin_idx": _parse_int_value(it.get("fanin_idx_candidate")),
                "control_idx": _parse_int_value(it.get("control_idx_candidate")),
                "fanin_label": it.get("fanin_label"),
                "fanin_signal": it.get("fanin_signal"),
                "fanout_signal": it.get("fanout_signal"),
            }
    elif isinstance(items, dict):
        for key, it in items.items():
            if not isinstance(it, dict):
                continue
            k = (key or "").strip()
            if not k:
                continue
            by_key[k] = {
                "fanin_idx": _parse_int_value(it.get("fanin_idx_candidate")),
                "control_idx": _parse_int_value(it.get("control_idx_candidate")),
                "fanin_label": it.get("fanin_label"),
                "fanin_signal": it.get("fanin_signal"),
                "fanout_signal": it.get("fanout_signal"),
            }

    return {
        "status": status,
        "by_key": by_key,
        "source_path": raw.get("_source_path"),
    }


def _build_fan_channel_map(rows: list[dict]) -> dict[str, str]:
    out: dict[str, str] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        key = (r.get("item_name") or "").strip()
        ch = (r.get("channel") or "").strip()
        if key and ch:
            out[key] = ch
    return out


def _infer_fan_base_channel(fan_rows: list[dict], ctrl_rows: list[dict]) -> tuple[int | None, str]:
    for rows in (fan_rows, ctrl_rows):
        vals: list[int] = []
        first_template = ""
        for r in rows:
            if not isinstance(r, dict):
                continue
            ch = (r.get("channel") or "").strip()
            if not ch:
                continue
            if not first_template:
                first_template = ch
            iv = _parse_int_value(ch)
            if iv is not None:
                vals.append(iv)
        if vals:
            return min(vals), first_template
    return None, "0x0"


def _apply_fan_control_topology(
    fan_rows: list[dict],
    control_result: dict,
    fan_pairing: dict | None,
) -> tuple[dict, dict]:
    decision = {
        "route": "FAN_DB_BASELINE",
        "status": "FOUND",
        "pending": False,
        "reason": None,
        "pairing_status": None,
        "source_path": None,
        "overridden_count": 0,
    }

    if not isinstance(control_result, dict):
        decision["pending"] = True
        decision["status"] = "FAN_PAIRING_AMBIGUOUS"
        decision["reason"] = "Invalid HWM.Fan.Control result payload"
        return control_result, decision

    rows = control_result.get("rows")
    if not isinstance(rows, list) or not rows:
        return control_result, decision

    if not fan_pairing:
        decision["reason"] = "No fan pairing result file; keep DB baseline"
        return control_result, decision

    pairing_status = (fan_pairing.get("status") or "").strip()
    by_key = fan_pairing.get("by_key") if isinstance(fan_pairing, dict) else None
    if not isinstance(by_key, dict):
        by_key = {}

    decision["pairing_status"] = pairing_status
    decision["source_path"] = fan_pairing.get("source_path")

    if pairing_status in {"FAN_PAIRING_AMBIGUOUS", "FAN_PAIRING_NEEDS_FANCONTROL_EVIDENCE"}:
        decision["route"] = "FAN_AI_PENDING"
        decision["pending"] = True
        decision["status"] = pairing_status
        decision["reason"] = "AI pairing status is pending/ambiguous"
        return control_result, decision

    if pairing_status not in {"FAN_ONE_TO_ONE_CONFIRMED", "FAN_ONE_TO_MANY_CONFIRMED"}:
        decision["reason"] = f"Unsupported pairing status '{pairing_status}', keep DB baseline"
        return control_result, decision

    base_channel, template = _infer_fan_base_channel(fan_rows, rows)
    if base_channel is None:
        decision["route"] = "FAN_AI_PENDING"
        decision["pending"] = True
        decision["status"] = "FAN_PAIRING_AMBIGUOUS"
        decision["reason"] = "Cannot infer base channel from HWM.Fan/HWM.Fan.Control"
        return control_result, decision

    fan_channel_by_key = _build_fan_channel_map(fan_rows)
    new_rows: list[dict] = []
    overridden = 0

    for r in rows:
        if not isinstance(r, dict):
            continue
        row = dict(r)
        key = (row.get("item_name") or "").strip()
        slot = by_key.get(key) if key else None

        if pairing_status == "FAN_ONE_TO_ONE_CONFIRMED":
            mapped = fan_channel_by_key.get(key)
            if mapped:
                if row.get("channel") != mapped:
                    row["channel"] = mapped
                    overridden += 1
            elif isinstance(slot, dict) and slot.get("control_idx") is not None:
                ch = _format_channel_like(template, base_channel + int(slot["control_idx"]))
                if row.get("channel") != ch:
                    row["channel"] = ch
                    overridden += 1
        else:
            if isinstance(slot, dict) and slot.get("control_idx") is not None:
                ch = _format_channel_like(template, base_channel + int(slot["control_idx"]))
                if row.get("channel") != ch:
                    row["channel"] = ch
                    overridden += 1

        new_rows.append(row)

    out = dict(control_result)
    out["rows"] = new_rows
    out["row_count"] = len(new_rows)
    out["fan_topology"] = {
        "status": pairing_status,
        "source": fan_pairing.get("source_path"),
        "base_channel": _format_channel_like(template, base_channel),
        "overridden_count": overridden,
    }

    decision["route"] = "FAN_AI_CONFIRMED"
    decision["overridden_count"] = overridden
    decision["reason"] = "Applied AI fan topology to HWM.Fan.Control channels"
    return out, decision


def _run_config_db_generate(project: str, in_json_path: Path, out_ini_path: Path,
                            db_path: Path, product_name: str, chip_name: str,
                            probe_spec: dict | None, probe_path: Path | None,
                            sections: list[str]) -> tuple[list[Path], Path, Path, Path, Path | None, Path | None]:
    spec = _load_spec_json(in_json_path)

    proj_dir = out_ini_path.parent
    name = out_ini_path.stem.replace("-pre", "")
    bios_cache_path = _build_bios_image_cache(proj_dir, name)
    fan_name_hints = _load_project_fan_name_hints(proj_dir, name, bios_cache_path)

    info_lines = _build_information_lines(project, probe_spec, spec)

    matrix: list[dict] = []
    generated_files: list[Path] = []
    pre_lines = info_lines.copy()

    is_ec = probe_spec.get("is_ec") if isinstance(probe_spec, dict) else None
    ec_voltage_base_path: Path | None = None
    voltage_alias_bridge_path: Path | None = None

    fan_pairing_raw = _load_fan_pairing_result(proj_dir, name)
    fan_pairing = _normalize_fan_pairing_result(fan_pairing_raw)

    # GPIO 改為 DB + spec.count 路線，不依賴外部 trace artifact
    latest_fan_rows: list[dict] = []
    latest_fan_result: dict | None = None

    for sec in sections:
        route = "EC" if is_ec is True else ("NON_EC" if is_ec is False else "UNKNOWN_EC")
        fan_meta: dict = {}

        if sec == "HWM.Voltage" and is_ec is True:
            result = _build_ec_voltage_query_result(
                db_path=db_path,
                product_name=product_name,
                chip_name=chip_name,
                probe_path=probe_path,
            )
            ec_voltage_base_path = _write_ec_voltage_base_file(proj_dir, name, result)
            voltage_alias_bridge_path = _write_voltage_alias_bridge_file(
                project_dir=proj_dir,
                project_name=name,
                ec_base_path=ec_voltage_base_path,
                bios_cache_path=bios_cache_path,
            )
        elif sec == "HWM.Temperature":
            result = _build_hwm_temperature_query_result(
                db_path=db_path,
                product_name=product_name,
                chip_name=chip_name,
                probe_path=probe_path,
                spec=spec,
            )
            result = _apply_temperature_name_hints(
                result,
                _load_project_temperature_name_hints(proj_dir, name),
            )
        elif sec == "HWM.Fan":
            result = _build_hwm_fan_query_result(
                db_path=db_path,
                product_name=product_name,
                chip_name=chip_name,
                probe_spec=probe_spec,
                fan_pairing=fan_pairing,
                fan_name_hints=fan_name_hints,
            )
            latest_fan_result = result
        elif sec == "HWM.Fan.Control":
            fan_seed = latest_fan_result or _build_hwm_fan_query_result(
                db_path=db_path,
                product_name=product_name,
                chip_name=chip_name,
                probe_spec=probe_spec,
                fan_pairing=fan_pairing,
                fan_name_hints=fan_name_hints,
            )
            latest_fan_rows = fan_seed.get("rows") or []
            result, fan_decision = _build_hwm_fan_control_query_result(
                fan_result=fan_seed,
                fan_pairing=fan_pairing,
                fan_name_hints=fan_name_hints,
            )
            route = f"{route}+{fan_decision.get('route')}"
            fan_meta = {
                "fan_pairing_status": fan_decision.get("pairing_status"),
                "fan_pairing_source": fan_decision.get("source_path"),
                "fan_channel_overridden_count": fan_decision.get("overridden_count", 0),
                "fan_pairing_reason": fan_decision.get("reason"),
            }
            if fan_decision.get("pending"):
                matrix.append({
                    "section": sec,
                    "status": fan_decision.get("status"),
                    "query_status": result.get("status"),
                    "row_count": result.get("row_count", 0),
                    "path": None,
                    "query_key": result.get("query_key"),
                    "route": route,
                    **fan_meta,
                })
                continue
        elif sec == "GPIO":
            result, gpio_decision = _build_gpio_query_result(
                db_path=db_path,
                product_name=product_name,
                chip_name=chip_name,
                spec=spec,
            )
            route = f"{route}+{gpio_decision.get('route')}"
            fan_meta = {
                "gpio_expected_count": gpio_decision.get("gpio_expected_count"),
                "gpio_group_pins_total": gpio_decision.get("gpio_group_pins_total"),
                "gpio_trimmed_count": gpio_decision.get("gpio_trimmed_count", 0),
                "gpio_trace_reason": gpio_decision.get("reason"),
            }
            if gpio_decision.get("pending"):
                matrix.append({
                    "section": sec,
                    "status": gpio_decision.get("status"),
                    "query_status": result.get("status"),
                    "row_count": result.get("row_count", 0),
                    "path": None,
                    "query_key": result.get("query_key"),
                    "route": route,
                    **fan_meta,
                })
                continue
        else:
            result = query_section(
                db_path=db_path,
                product_name=product_name,
                chip_name=chip_name,
                section=sec,
            )

        if sec == "HWM.Voltage" and is_ec is True and result.get("status") == "FOUND":
            result = _merge_voltage_alias_into_rows(result, voltage_alias_bridge_path)

        status = result.get("status")

        if sec == "HWM.Fan" and status == "FOUND":
            latest_fan_rows = result.get("rows") or []
            latest_fan_result = result

        if status == "FOUND":
            # HWM.Voltage has EC vs non-EC routing differences.
            if sec == "HWM.Voltage" and is_ec is False:
                eval_non_ec = _evaluate_non_ec_hwm_voltage(
                    product_name=product_name,
                    chip_name=chip_name,
                    rows=result.get("rows") or [],
                )
                route = eval_non_ec["route"]

                if eval_non_ec["pending"]:
                    matrix.append({
                        "section": sec,
                        "status": eval_non_ec["status"],
                        "query_status": status,
                        "row_count": result.get("row_count", 0),
                        "path": None,
                        "query_key": result.get("query_key"),
                        "route": route,
                        "rule": eval_non_ec.get("rule"),
                        "duplicate_channels": eval_non_ec.get("duplicate_channels", []),
                        "reason": eval_non_ec.get("reason"),
                    })
                    continue

            sec_lines = info_lines + _render_section_lines(sec, result)
            sec_path = proj_dir / f"{name}_{sec}.ini"
            sec_path.write_text("\n".join(sec_lines), encoding="utf-8")
            generated_files.append(sec_path)

            pre_lines.extend(_render_section_lines(sec, result))
            matrix.append({
                "section": sec,
                "status": "GENERATED",
                "query_status": status,
                "row_count": result.get("row_count", 0),
                "path": str(sec_path),
                "query_key": result.get("query_key"),
                "route": route,
                **fan_meta,
                **({
                    "ec_voltage_base": str(ec_voltage_base_path) if ec_voltage_base_path else None,
                    "voltage_alias_bridge": str(voltage_alias_bridge_path) if voltage_alias_bridge_path else None,
                } if sec == "HWM.Voltage" and is_ec is True else {}),
            })
        elif status == "SECTION_EMPTY":
            matrix.append({
                "section": sec,
                "status": "SKIPPED_EMPTY_SECTION",
                "query_status": status,
                "row_count": 0,
                "path": None,
                "query_key": result.get("query_key"),
                "route": route,
                **fan_meta,
                **({
                    "ec_voltage_base": str(ec_voltage_base_path) if ec_voltage_base_path else None,
                    "voltage_alias_bridge": str(voltage_alias_bridge_path) if voltage_alias_bridge_path else None,
                } if sec == "HWM.Voltage" and is_ec is True else {}),
            })
        else:
            matrix.append({
                "section": sec,
                "status": status,
                "query_status": status,
                "row_count": 0,
                "path": None,
                "query_key": result.get("query_key"),
                "error": result.get("error"),
                "route": route,
                **fan_meta,
                **({
                    "ec_voltage_base": str(ec_voltage_base_path) if ec_voltage_base_path else None,
                    "voltage_alias_bridge": str(voltage_alias_bridge_path) if voltage_alias_bridge_path else None,
                } if sec == "HWM.Voltage" and is_ec is True else {}),
            })

    out_ini_path.write_text("\n".join(pre_lines) + "\n", encoding="utf-8")

    matrix_path = proj_dir / f"{name}-section-matrix.json"
    matrix_path.write_text(
        json.dumps(
            {
                "project": name,
                "db_path": str(db_path),
                "query_key": {"product_name": product_name, "chip_name": chip_name},
                "ec_gate": {"is_ec": is_ec},
                "bios_image_cache": str(bios_cache_path),
                "hwm_voltage_ec_base": str(ec_voltage_base_path) if ec_voltage_base_path else None,
                "voltage_alias_bridge": str(voltage_alias_bridge_path) if voltage_alias_bridge_path else None,
                "fan_pairing_source": fan_pairing.get("source_path") if isinstance(fan_pairing, dict) else None,
                "fan_pairing_status": fan_pairing.get("status") if isinstance(fan_pairing, dict) else None,
                "sections": matrix,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    return generated_files, matrix_path, out_ini_path, bios_cache_path, ec_voltage_base_path, voltage_alias_bridge_path


def main():
    parser = argparse.ArgumentParser(
        description=(
            "SUSI pipeline wrapper\n"
            "  Stages: extract → understand → generate\n"
            "  PDF → form.json → spec.json → pre-INI\n"
            "  'understand' requires LLM_API_KEY env var (OpenAI-compatible)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--project", required=True, help="Project name or directory path")
    parser.add_argument("--root", default=".", help="Base directory for relative paths")
    parser.add_argument(
        "--stage",
        choices=["extract", "understand", "generate", "all"],
        default="all",
        help=(
            "extract   — PDF → form.json\n"
            "understand — form.json → spec.json (LLM, needs LLM_API_KEY)\n"
            "generate  — config.db query → pre-INI + section INIs\n"
            "all       — extract + understand + generate"
        ),
    )
    parser.add_argument("--in-pdf", dest="input_pdf", help="Override input PDF path")
    parser.add_argument("--out-json", help="Override form.json output path")
    parser.add_argument("--in-json", help="Override form.json input path")
    parser.add_argument("--spec-out", dest="spec_out", help="Override spec.json output path")
    parser.add_argument("--out-ini", help="Override pre-INI output path")
    parser.add_argument("--probe", help="Probe report .txt (from Machine B)")
    parser.add_argument("--split", action="store_true",
                        help="Emit section INI files for all 14 sections and write section-matrix.json")
    parser.add_argument("--sections",
                        help=("Comma-separated subset of sections to emit (e.g. "
                              "'HWM.CaseOpen,HWM.Current,HWM.Voltage,StorageArea,ThermalProtect,WDT,VGA.Backlight,VGA.Brightness')"))
    parser.add_argument("--db", default="/home/company2/AIagent_susi/config.db", help="Path to config.db")
    parser.add_argument("--product-name", help="Override ProductChip product_name key (default inferred from project, e.g. SOM-6833->SOM)")
    parser.add_argument("--chip-name", help="Override ProductChip chip_name key (default inferred from spec.json, e.g. EIO-211)")
    parser.add_argument(
        "--screenshot", dest="screenshots", action="append", metavar="PATH", default=[],
        help="Screenshot(s) for understand stage (repeatable, e.g. BIOS screen)",
    )
    parser.add_argument(
        "--skip-understand", dest="skip_understand", action="store_true",
        help="In 'all' mode: skip understand stage (use form.json directly for generate)",
    )
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    screenshots = [Path(s) for s in args.screenshots]

    if args.split and args.sections:
        parser.error("--split and --sections are mutually exclusive")

    selected_sections = _parse_sections_arg(args.sections)

    json_path = None

    # ── Stage: extract ──────────────────────────────────────────────────────
    if args.stage in ("extract", "all"):
        _, json_path = resolve_extract_paths(args.project, args.input_pdf, args.out_json, root)
        if json_path.exists():
            print(f"Skip extract: {json_path} already exists")
        else:
            pdf_path, json_path = resolve_extract_paths(args.project, args.input_pdf, args.out_json, root)
            json_path = extract_pdf_to_json(pdf_path, json_path)
            print(f"Extract done: {json_path}")

    # ── Stage: understand ────────────────────────────────────────────────────
    run_understand = (
        args.stage == "understand"
        or (args.stage == "all" and not args.skip_understand)
    )
    if run_understand:
        in_json_arg = args.in_json or (str(json_path) if json_path else None)
        spec_path = understand(
            project=args.project,
            in_json=in_json_arg,
            spec_out=args.spec_out,
            screenshots=screenshots,
            root=root,
        )
        print(f"Understand done: {spec_path}")

    # ── Stage: generate (config.db only) ───────────────────────────────────
    if args.stage in ("generate", "all"):
        in_json_arg = args.in_json or (str(json_path) if json_path else None)
        in_json_path, out_ini_path = resolve_generate_paths(args.project, in_json_arg, args.out_ini, root)

        probe_spec = None
        probe_path = Path(args.probe) if args.probe else None
        if args.probe:
            probe_spec = parse_probe_report(probe_path)
            print(f"Probe: board={probe_spec.get('board_name') or '?'} "
                  f"platform_rev={probe_spec.get('platform_version') or '?'} "
                  f"bios={probe_spec.get('bios_version') or '?'} is_ec={probe_spec.get('is_ec')}")

        sections_to_emit = selected_sections if selected_sections else SPLIT_SECTIONS

        product_name = args.product_name or _project_to_product_name(args.project)
        spec = _load_spec_json(in_json_path)
        chip_name = args.chip_name or _infer_chip_name(spec)
        if not chip_name:
            raise RuntimeError(
                "Cannot infer chip_name from spec.json. Please pass --chip-name (e.g. EIO-211)."
            )

        outs, matrix_path, full_ini, bios_cache_path, ec_base_path, alias_bridge_path = _run_config_db_generate(
            project=args.project,
            in_json_path=in_json_path,
            out_ini_path=out_ini_path,
            db_path=Path(args.db),
            product_name=product_name,
            chip_name=chip_name,
            probe_spec=probe_spec,
            probe_path=probe_path,
            sections=sections_to_emit,
        )

        print(f"Generate done: {full_ini}")
        for o in outs:
            print(f"Generate done: {o}")
        print(f"BIOS cache: {bios_cache_path}")
        if ec_base_path:
            print(f"EC voltage base: {ec_base_path}")
        if alias_bridge_path:
            print(f"Voltage alias bridge: {alias_bridge_path}")
        print(f"Section matrix: {matrix_path}")


if __name__ == "__main__":
    main()
