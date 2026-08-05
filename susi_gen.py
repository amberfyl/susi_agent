import argparse
import hashlib
import json
import re
import sqlite3
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

    # User-requested policy: do not use BOARD_PLATFORM_REV_VAL for PlatformVersion.
    # Keep a fixed default for now.
    platform = "A101-1"

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
    circus_pngs = sorted(project_dir.glob("circus*.png"))
    pngs = bios_pngs + [p for p in circus_pngs if p not in bios_pngs]

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
        "image_glob": ["bios*.png", "circus*.png"],
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


def _run_config_db_generate(project: str, in_json_path: Path, out_ini_path: Path,
                            db_path: Path, product_name: str, chip_name: str,
                            probe_spec: dict | None, probe_path: Path | None,
                            sections: list[str]) -> tuple[list[Path], Path, Path, Path, Path | None, Path | None]:
    spec = _load_spec_json(in_json_path)

    proj_dir = out_ini_path.parent
    name = out_ini_path.stem.replace("-pre", "")
    bios_cache_path = _build_bios_image_cache(proj_dir, name)

    info_lines = _build_information_lines(project, probe_spec, spec)

    matrix: list[dict] = []
    generated_files: list[Path] = []
    pre_lines = info_lines.copy()

    is_ec = probe_spec.get("is_ec") if isinstance(probe_spec, dict) else None
    ec_voltage_base_path: Path | None = None
    voltage_alias_bridge_path: Path | None = None

    for sec in sections:
        route = "EC" if is_ec is True else ("NON_EC" if is_ec is False else "UNKNOWN_EC")

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
