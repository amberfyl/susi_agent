import argparse
import ast
import hashlib
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import agent_llm
from build_machineB_section_configs import VOLTAGE_API_INDEX
from extract_pdf import extract_pdf_to_json, resolve_paths as resolve_extract_paths
from generate_ini import resolve_paths as resolve_generate_paths
from machineb_fallback import apply_project_route_overrides
from parse_probe import parse_probe_report
from query_config_db import (
    query_section,
    chip_uses_hwm_fan_defaults,
    load_hwm_fan_defaults,
)
from understand import understand


SPLIT_SECTIONS = [
    "SMBus", "I2C", "VGA.Backlight", "VGA.Brightness",
    "HWM.Voltage", "HWM.Current", "HWM.Temperature", "HWM.Fan",
    "HWM.Fan.Control", "HWM.CaseOpen", "WDT", "GPIO",
    "StorageArea", "ThermalProtect",
]


_CIRCUIT_EVIDENCE_CHIP_PREFIXES = (
    "NCT6694B",
    "NCT6106D",
    "NCT6116D",
    "NCT6126D",
    "NCT6776D",
)


def _chip_requires_circuit_evidence(chip_name: str | None) -> bool:
    c = _norm_chip_name(chip_name or "")
    return c.startswith(_CIRCUIT_EVIDENCE_CHIP_PREFIXES)


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


_BIOS_LIVE_READING_FIELDS = (
    "voltage_value_hints",
    "temperature_value_hints",
    "fan_value_hints",
    "current_value_hints",
    "caseopen_hints",
)
_BIOS_SECTION_EVIDENCE_FIELDS = {
    "HWM.Voltage": ("voltage_value_hints", "voltage_label_hints"),
    "HWM.Temperature": ("temperature_value_hints", "temperature_label_hints"),
    "HWM.Fan": ("fan_value_hints", "fan_label_hints"),
    "HWM.Fan.Control": ("fan_value_hints", "fan_label_hints"),
    "HWM.Current": ("current_value_hints", "current_label_hints"),
    "HWM.CaseOpen": ("caseopen_hints",),
}


def _is_bios_monitor_page(item: dict) -> bool:
    """A BIOS page is the Hardware Monitor page only if it shows live readings."""
    return any(isinstance(item.get(f), list) and item[f] for f in _BIOS_LIVE_READING_FIELDS)


def _bios_has_section_evidence(section: str, bios_cache_path: Path | None) -> bool:
    """HWM evidence comes only from structured readings on Hardware Monitor pages.

    Other BIOS pages (CPU configuration, main/version pages, ...) and free-text
    prose (including negative sentences or setting names) are never evidence.
    """
    if not bios_cache_path or not bios_cache_path.is_file():
        return False
    try:
        cache = json.loads(bios_cache_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    items = cache.get("items") if isinstance(cache, dict) else None
    if not isinstance(items, list):
        return False

    fields = _BIOS_SECTION_EVIDENCE_FIELDS.get(section, ())
    for item in items:
        if not isinstance(item, dict):
            continue
        image_name = str(item.get("filename") or item.get("path") or "").strip()
        if image_name and not Path(image_name).name.lower().startswith("bios"):
            continue
        if not _is_bios_monitor_page(item):
            continue
        if any(isinstance(item.get(f), list) and item[f] for f in fields):
            return True
    return False


def evaluate_section_applicability(
    section: str,
    spec: dict | None,
    bios_cache_path: Path | None,
) -> dict[str, object]:
    """Decide whether a section may be queried/generated before consulting DB tuples."""
    bios_gated_sections = {
        "HWM.Voltage",
        "HWM.Current",
        "HWM.Temperature",
        "HWM.Fan",
        "HWM.Fan.Control",
        "HWM.CaseOpen",
    }
    if section not in bios_gated_sections:
        return {"applicable": True, "reason_code": "SECTION_NOT_BIOS_GATED"}
    if _bios_has_section_evidence(section, bios_cache_path):
        return {"applicable": True, "reason_code": "BIOS_EVIDENCE_FOUND"}
    return {"applicable": False, "reason_code": "BIOS_EVIDENCE_NOT_FOUND"}


def _infer_chip_name(spec: dict | None) -> str | None:
    if not spec:
        return None

    # Candidate strings in priority order: explicit chips.* values, then
    # gpio pin chip hints.
    texts: list[str] = []
    chips = spec.get("chips") if isinstance(spec, dict) else None
    if isinstance(chips, dict):
        for k in ("ec", "hwm", "gpio", "smbus", "i2c"):
            v = chips.get(k)
            if isinstance(v, str) and v.strip():
                texts.append(v.upper())
    gpio = spec.get("gpio") if isinstance(spec, dict) else None
    if isinstance(gpio, dict):
        for pin in gpio.get("pins", []) if isinstance(gpio.get("pins"), list) else []:
            chip = pin.get("chip") if isinstance(pin, dict) else None
            if isinstance(chip, str):
                texts.append(chip.upper())

    # 1) EC key first. A compound string such as "NCT6694B(EIO-300)" keeps the
    #    EC key; section routing later sends GPIO/I2C/SMBus to NCT6694B.
    for text in texts:
        m = re.search(r"(EIO-\d+|IT-\d+|IT\d+)", text)
        if m:
            x = m.group(1)
            if x.startswith("IT") and not x.startswith("IT-") and x[2:].isdigit():
                x = f"IT-{x[2:]}"
            return x

    # 2) Nuvoton SIO chip (e.g. NCT6126D, NCT6694B) when no EC key exists.
    for text in texts:
        m = re.search(r"NCT[-_ ]?(\d{4}[A-Z])", text)
        if m:
            return f"NCT{m.group(1)}"

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
        r"\+12V|\+9V|\+5V|\+3\.3V",
        r"VBAT|VCORE2?|VTT|5VSB|3VSB|12V|9V|5V|3V3",
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


def _normalize_monitor_label(label: str) -> str:
    s = str(label or "").strip().upper()
    s = re.sub(r"\s+", " ", s)
    return s


def _parse_float_safe(x: str) -> float | None:
    try:
        return float(str(x).strip())
    except Exception:
        return None


def _merge_value_hints(existing: object, parsed: list[dict]) -> list[dict]:
    merged: list[dict] = []
    idx_by_key: dict[str, int] = {}

    def _add_one(raw: object) -> None:
        if not isinstance(raw, dict):
            return
        label = str(raw.get("label") or "").strip()
        val = raw.get("value")
        unit = str(raw.get("unit") or "").strip().upper()
        if not label:
            return
        if not isinstance(val, (int, float)):
            vv = _parse_float_safe(str(val))
            if vv is None:
                return
            val = vv
        key = _normalize_monitor_label(label)
        rec = {
            "label": label,
            "value": float(val),
            "unit": unit,
        }
        i = idx_by_key.get(key)
        if i is None:
            idx_by_key[key] = len(merged)
            merged.append(rec)
        else:
            merged[i] = rec

    if isinstance(existing, list):
        for it in existing:
            _add_one(it)
    for it in parsed:
        _add_one(it)
    return merged


def _extract_voltage_value_hints(text: str) -> list[dict]:
    u = (text or "").upper()
    patt = re.compile(
        r"(?P<label>\+?12VSB|\+?5VSB|\+?3\.3VSB|\+?12V|\+?9V|\+?5V|\+?3\.3V|VBAT|VCORE2?|VTT|3VSB|5VSB|12V|9V|5V|3V3)"
        r"\s*[:=]?\s*(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>MV|V)\b"
    )
    out: list[dict] = []
    for m in patt.finditer(u):
        v = _parse_float_safe(m.group("value"))
        if v is None:
            continue
        out.append({
            "label": m.group("label"),
            "value": v,
            "unit": m.group("unit").upper(),
        })
    return _merge_value_hints([], out)


def _extract_temperature_value_hints(text: str) -> list[dict]:
    u = (text or "").upper()
    patt = re.compile(
        r"(?P<label>CPU\s*TEMPERATURE|SYSTEM\s*TEMPERATURE|SYS\s*TEMPERATURE|CPU\s*TEMP|SYSTEM\s*TEMP|SYS\s*TEMP|TCPU|TSYS)"
        r"\s*[:=]?\s*(?P<value>-?\d+(?:\.\d+)?)\s*(?:°?\s*C)\b"
    )
    out: list[dict] = []
    for m in patt.finditer(u):
        v = _parse_float_safe(m.group("value"))
        if v is None:
            continue
        out.append({
            "label": m.group("label"),
            "value": v,
            "unit": "C",
        })
    return _merge_value_hints([], out)


def _extract_fan_value_hints(text: str) -> list[dict]:
    u = (text or "").upper()
    patt = re.compile(
        r"(?P<label>CPU\s*FAN\d*\s*SPEED|SYS(?:TEM)?\s*FAN\d*\s*SPEED|COM\s*MODULE\s*FAN|CARRIER\s*BOARD\s*FAN|FCPU\d*|FSYS\d*|FOEM\d+)"
        r"\s*[:=]?\s*(?P<value>-?\d+(?:\.\d+)?)\s*RPM\b"
    )
    out: list[dict] = []
    for m in patt.finditer(u):
        v = _parse_float_safe(m.group("value"))
        if v is None:
            continue
        out.append({
            "label": m.group("label"),
            "value": v,
            "unit": "RPM",
        })
    return _merge_value_hints([], out)


_CASEOPEN_LIVE_STATES = {
    "OPEN", "OPENED", "CLOSE", "CLOSED", "YES", "NO", "OK", "NORMAL",
    "DETECTED", "INTRUDED", "TRIGGERED", "ALARM", "CLEAR", "CLEARED",
}


def _extract_current_value_hints(text: str) -> list[dict]:
    """Live current readings, e.g. 'System Current 1.2 A' or 'CURRENT_VALUE: CPU Current=850mA'."""
    patt = re.compile(
        r"(?P<label>[A-Za-z][A-Za-z0-9 _.+-]*?\bCURRENT\b)\s*[:=]?\s*"
        r"(?P<value>-?\d+(?:\.\d+)?)\s*(?P<unit>mA|A)\b",
        re.IGNORECASE,
    )
    out: list[dict] = []
    for m in patt.finditer(text or ""):
        label = re.sub(r"^(?:CURRENT_VALUE\s*:\s*)", "", m.group("label").strip(), flags=re.IGNORECASE).strip()
        v = _parse_float_safe(m.group("value"))
        if v is None or not label:
            continue
        unit = "mA" if m.group("unit").lower() == "ma" else "A"
        out.append({"label": label, "value": v, "unit": unit})
    return out


def _extract_caseopen_hints(text: str) -> list[dict]:
    """Live case-open/intrusion status, e.g. 'CASEOPEN_VALUE: Case Open=No'.

    Settings such as 'Case Open Detection' or 'Chassis Intrusion [Disabled]'
    and negative sentences are not live readings and yield nothing.
    """
    patt = re.compile(
        r"(?P<label>case\s*open(?:\s+(?:status|warning))?|chassis\s+intrusion(?:\s+status)?)"
        r"\s*[:=]?\s*(?P<state>[A-Za-z]+)\b",
        re.IGNORECASE,
    )
    out: list[dict] = []
    for m in patt.finditer(text or ""):
        state = m.group("state")
        if state.upper() not in _CASEOPEN_LIVE_STATES:
            continue
        out.append({"label": re.sub(r"\s+", " ", m.group("label").strip()), "state": state})
    return out


def _collect_scoped_vision_images(project_dir: Path, chip_name: str | None = None) -> list[Path]:
    """Collect scoped bios/circuit rasters with chip-aware scope.

    - Always include bios*.png/jpg/jpeg.
    - Include circuit*.png/jpg/jpeg only for chip families that require
      circuit evidence (NCT6694B / NCT61xxD / NCT6776D*).
    """
    include_circuit = _chip_requires_circuit_evidence(chip_name)

    images: list[Path] = []
    for p in sorted(project_dir.iterdir()):
        if not p.is_file():
            continue
        name = p.name.lower()
        if name.startswith("bios"):
            pass
        elif include_circuit and name.startswith("circuit"):
            pass
        else:
            continue
        if p.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            continue
        images.append(p)
    return images


def _build_bios_image_cache(project_dir: Path, project_name: str, chip_name: str | None = None) -> Path:
    pngs = _collect_scoped_vision_images(project_dir, chip_name=chip_name)

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

        voltage_value_hints = prev.get("voltage_value_hints") if isinstance(prev, dict) else None
        if not isinstance(voltage_value_hints, list):
            voltage_value_hints = _extract_voltage_value_hints(str(analysis_text or ""))

        temperature_value_hints = prev.get("temperature_value_hints") if isinstance(prev, dict) else None
        if not isinstance(temperature_value_hints, list):
            temperature_value_hints = _extract_temperature_value_hints(str(analysis_text or ""))

        fan_value_hints = prev.get("fan_value_hints") if isinstance(prev, dict) else None
        if not isinstance(fan_value_hints, list):
            fan_value_hints = _extract_fan_value_hints(str(analysis_text or ""))

        current_value_hints = prev.get("current_value_hints") if isinstance(prev, dict) else None
        if not isinstance(current_value_hints, list):
            current_value_hints = _extract_current_value_hints(str(analysis_text or ""))

        caseopen_hints = prev.get("caseopen_hints") if isinstance(prev, dict) else None
        if not isinstance(caseopen_hints, list):
            caseopen_hints = _extract_caseopen_hints(str(analysis_text or ""))

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
            "analysis_engine": "agent_vision_cli",
            "analysis_status": analysis_status,
            "analysis_text": analysis_text or "",
            "voltage_label_hints": hints,
            "voltage_value_hints": voltage_value_hints,
            "temperature_value_hints": temperature_value_hints,
            "fan_value_hints": fan_value_hints,
            "current_value_hints": current_value_hints,
            "caseopen_hints": caseopen_hints,
            "label_hint_source": label_hint_source,
        })

    overall_status = "DONE" if items and all(i.get("analysis_status") == "DONE_VISION_ANALYZE" for i in items) else "PENDING"
    image_glob = ["bios*.png|jpg|jpeg"]
    if _chip_requires_circuit_evidence(chip_name):
        image_glob.append("circuit*.png|jpg|jpeg")

    payload = {
        "project": project_name,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "image_glob": image_glob,
        "image_count": len(items),
        "analysis_engine": "agent_vision_cli",
        "analysis_status": overall_status,
        "items": items,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


# Model-facing judgement rules live in prompts/*.md (single source; the human
# document AnalysisSKill/bios_circuit_image_analysis_rule.md points to them).
# Code keeps only the fixed lead-in, per-case info and the output format.
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
BIOS_READING_PROMPT = "bios_reading.md"
GPIO_TRACE_PROMPT = "gpio_trace.md"
BIOS_GATED_SECTIONS = (
    "HWM.Voltage",
    "HWM.Current",
    "HWM.Temperature",
    "HWM.Fan",
    "HWM.Fan.Control",
    "HWM.CaseOpen",
)


def _load_prompt_rules(filename: str, prompts_dir: Path | None = None) -> dict:
    """Load a prompts/*.md rule file.

    Returns {"text", "issue"}. When the file cannot be used, text is None and
    issue explains why; callers skip the AI step and report it.
    """
    path = (prompts_dir or PROMPTS_DIR) / filename
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return {"text": None, "issue": f"PROMPT_FILE_MISSING: prompts/{filename}"}
    if not text:
        return {"text": None, "issue": f"PROMPT_FILE_EMPTY: prompts/{filename}"}
    return {"text": text, "issue": None}


def _build_bios_reading_prompt(rules_text: str, img_path: Path) -> str:
    return (
        "Analyze this BIOS/circuit image with high reasoning. "
        "Follow the rules below and report only what is visible.\n\n"
        f"{rules_text}\n\n"
        "## Output format\n"
        "Plain text. List the voltage labels, real-time temperature sensor names and fan names you see. "
        "When a live value is visible, also write it in this form: "
        "VOLTAGE_VALUE: <label>=<number><V|mV>; TEMP_VALUE: <label>=<number>C; FAN_VALUE: <label>=<number>RPM; "
        "CURRENT_VALUE: <label>=<number><A|mA>; CASEOPEN_VALUE: <label>=<state>\n"
        "If this is not the Hardware Monitor page, answer only: No live hardware-monitor sensor rows are visible.\n\n"
        "## Case info\n"
        f"Image path: {img_path}"
    )


def _build_gpio_trace_prompt(rules_text: str, chip_u: str, target_signal_hint: str, img: Path) -> str:
    return (
        "You are a schematic GPIO wire-trace analyzer. Use vision_analyze on this image. "
        "Follow the rules below; never guess.\n\n"
        f"{rules_text}\n\n"
        "## Output format\n"
        "Output a single JSON object, no markdown:\n"
        "{\"items\":[{\"report_name\":\"GPIO00\",\"signal\":\"EC_P1_GPIO0\",\"function_label\":\"GPIOB0\","
        "\"group\":11,\"bit\":0,\"package_pin\":\"F1\",\"status\":\"CONFIRMED|AMBIGUOUS\","
        "\"evidence\":\"...\",\"name\":\"\"}],\"notes\":\"...\"}\n\n"
        "## Case info\n"
        f"Chip: {chip_u or 'UNKNOWN'}\n"
        f"Target signal pattern: {target_signal_hint}\n"
        f"Image path: {img}"
    )


# All AI calls go through agent_llm: the current agent CLI (default Hermes
# one-shot) answers with its own provider/model/credentials. Template env:
# SUSI_AGENT_CMD (or legacy SUSI_VISION_CMD), "{prompt}" required, "{image}" optional.
DEFAULT_VISION_CMD = agent_llm.DEFAULT_AGENT_CMD


def _vision_command(prompt: str, image_path: Path) -> list[str]:
    """Build the agent CLI argv for one image (see agent_llm.agent_command)."""
    return agent_llm.agent_command(prompt, image_path)


def _run_vision_analyze(image_path: Path, prompt: str) -> str | None:
    """Run the agent CLI on one image and return its plain-text answer."""
    return agent_llm.run_agent(prompt, image_path)


def _vision_populate_bios_cache(cache_path: Path, prompt_issues: dict | None = None,
                                prompts_dir: Path | None = None) -> Path:
    """Producer half: actively run vision only for pending/missing cache items.

    Items already analyzed (for example by the Hermes agent before generation)
    are used as-is. prompts/bios_reading.md is read only when an AI call is
    needed; if it is unusable, no AI call is made and the issue is recorded in
    prompt_issues[BIOS_READING_PROMPT].
    """
    if not cache_path.exists():
        return cache_path

    rules: dict | None = None

    try:
        data = _load_json(cache_path)
    except Exception:
        return cache_path

    if not isinstance(data, dict):
        return cache_path

    items = data.get("items")
    if not isinstance(items, list):
        return cache_path

    changed = False
    for it in items:
        if not isinstance(it, dict):
            continue

        img_path = Path(str(it.get("path") or "").strip())
        if not img_path.exists():
            continue

        status = str(it.get("analysis_status") or "").strip().upper()
        analysis_text = str(it.get("analysis_text") or "").strip()
        has_value_fields = all(k in it for k in [
            "voltage_value_hints",
            "temperature_value_hints",
            "fan_value_hints",
        ])
        # A completed page without live readings (e.g. CPU configuration) is a
        # valid result and must not be re-analyzed.
        needs_ai = (status != "DONE_VISION_ANALYZE") or (not analysis_text) or (not has_value_fields)
        if not needs_ai:
            continue

        if rules is None:
            rules = _load_prompt_rules(BIOS_READING_PROMPT, prompts_dir)
            if rules["issue"]:
                if prompt_issues is not None:
                    prompt_issues[BIOS_READING_PROMPT] = rules["issue"]
                print(f"WARNING: {rules['issue']}; BIOS image analysis skipped", file=sys.stderr)
        if rules["issue"]:
            continue

        out = _run_vision_analyze(img_path, _build_bios_reading_prompt(rules["text"], img_path))
        if not out:
            continue

        existing_hints = it.get("voltage_label_hints")
        merged_hints: list[str] = []
        seen: set[str] = set()
        if isinstance(existing_hints, list):
            for x in existing_hints:
                s = str(x or "").strip().upper()
                if s and s not in seen:
                    seen.add(s)
                    merged_hints.append(s)
        for x in _extract_voltage_label_hints(out):
            s = str(x or "").strip().upper()
            if s and s not in seen:
                seen.add(s)
                merged_hints.append(s)

        voltage_value_hints = _merge_value_hints(
            it.get("voltage_value_hints"),
            _extract_voltage_value_hints(out),
        )
        temperature_value_hints = _merge_value_hints(
            it.get("temperature_value_hints"),
            _extract_temperature_value_hints(out),
        )
        fan_value_hints = _merge_value_hints(
            it.get("fan_value_hints"),
            _extract_fan_value_hints(out),
        )

        it["analysis_text"] = out
        it["analysis_status"] = "DONE_VISION_ANALYZE"
        it["label_hint_source"] = "vision_analyze"
        it["voltage_label_hints"] = merged_hints
        it["voltage_value_hints"] = voltage_value_hints
        it["temperature_value_hints"] = temperature_value_hints
        it["fan_value_hints"] = fan_value_hints
        it["current_value_hints"] = _extract_current_value_hints(out)
        it["caseopen_hints"] = _extract_caseopen_hints(out)
        changed = True

    data["analysis_status"] = "DONE" if items and all(
        isinstance(i, dict) and str(i.get("analysis_status") or "").strip().upper() == "DONE_VISION_ANALYZE"
        for i in items
    ) else "PENDING"
    data["generated_at_utc"] = datetime.now(timezone.utc).isoformat()

    if changed:
        cache_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    return cache_path


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

    # DC input rail (SUSI HWM_VOLTAGE_DC): BIOS pages label it "+Vin" / "DC IN".
    # Return the BIOS text as written so Name matches the BIOS screen.
    dc_input_labels = {"+VIN", "VIN", "DCIN", "+DCIN", "VDCIN"}
    if re.search(r"(^|_)DC$", rn.strip()):
        for label in sorted(x for x in labels if isinstance(x, str)):
            if _canon(label) in dc_input_labels:
                return label.strip(), "BIOS_LABEL_MATCH_DC_INPUT"
        return None, "NO_CONFIDENT_ALIAS_DC"

    has_5vsb = any(x in canon_labels for x in {"+5VSB", "5VSB", "5VSTANDBY", "+5VSTANDBY"})
    has_5v_plain = any(x in canon_labels for x in {"+5V", "5V"})
    has_12v = any(x in canon_labels for x in {"+12V", "12V"})
    has_9v = any(x in canon_labels for x in {"+9V", "9V"})
    has_33v = any(x in canon_labels for x in {"+3.3V", "3V3"})
    has_vbat = "VBAT" in canon_labels

    # 回填 Name 使用 BIOS 直接可讀標籤（例如 +12V/+5V/VBAT）
    if "VBAT" in rn and has_vbat:
        return "VBAT", "BIOS_LABEL_MATCH_VBAT"
    if "12V" in rn and has_12v:
        return "+12V", "BIOS_LABEL_MATCH_12V"
    if "9V" in rn and has_9v:
        return "+9V", "BIOS_LABEL_MATCH_9V"
    if "3V" in rn and has_33v:
        return "+3.3V", "BIOS_LABEL_MATCH_3V3"

    # 5V rail: must disambiguate 5V vs 5VSB by report_name first.
    if "5VSB" in rn or "STBY" in rn:
        if has_5vsb:
            return "+5VSB", "BIOS_LABEL_MATCH_5VSB"
        return None, "NO_CONFIDENT_ALIAS_5VSB"

    if "5V" in rn:
        if has_5v_plain:
            return "+5V", "BIOS_LABEL_MATCH_5V_PLAIN"
        if has_5vsb:
            return "+5VSB", "BIOS_LABEL_MATCH_5VSB_FALLBACK"

    return None, "NO_CONFIDENT_ALIAS"


def _ai_resolve_unmatched_voltage_aliases(
    unresolved: list[dict],
    bios_items: list[dict],
    candidate_labels: list[str],
    used_aliases: set[str],
) -> list[dict]:
    """Use vision-AI reasoning to map unresolved DB voltage rows to BIOS labels.

    Fallback for obvious name mismatch (e.g., report_name DC vs BIOS +9V),
    to reduce dependence on Python-side string enumeration.
    """
    if not unresolved or not bios_items or not candidate_labels:
        return []

    bios_candidates: list[dict] = []
    for it in bios_items:
        if not isinstance(it, dict):
            continue
        fn = str(it.get("filename") or "")
        if not fn.lower().startswith("bios"):
            continue
        p = Path(str(it.get("path") or "").strip())
        if not p.exists():
            continue
        vv = it.get("voltage_value_hints")
        score = len(vv) if isinstance(vv, list) else 0
        bios_candidates.append({"item": it, "path": p, "score": score})
    if not bios_candidates:
        return []

    bios_candidates.sort(key=lambda x: int(x.get("score") or 0), reverse=True)
    target_item = bios_candidates[0]["item"]
    target_path: Path = bios_candidates[0]["path"]

    values_lines: list[str] = []
    vv = target_item.get("voltage_value_hints")
    if isinstance(vv, list):
        for rec in vv:
            if not isinstance(rec, dict):
                continue
            lb = str(rec.get("label") or "").strip()
            val = rec.get("value")
            unit = str(rec.get("unit") or "").strip()
            if lb:
                values_lines.append(f"- {lb}={val}{unit}")

    added: list[dict] = []
    used_norm = {str(x).upper().strip() for x in used_aliases if str(x).strip()}
    candidate_norm = {str(x).upper().strip() for x in candidate_labels if str(x).strip()}

    for row in unresolved:
        if not isinstance(row, dict):
            continue
        report_name = str(row.get("report_name") or "").strip()
        channel_id = str(row.get("channel_id") or "").strip()
        if not report_name:
            continue

        prompt = (
            "請分析這張 BIOS 圖，替一個未匹配 DB 電壓項目選最可能 BIOS 標籤。"
            "可用語意/數值推理（名字可不一致），但不可虛構標籤。\n"
            f"目標DB項目: report_name={report_name}, channel_id={channel_id}\n"
            f"候選BIOS標籤(只能選其一): {', '.join(candidate_labels)}\n"
            f"已使用標籤(盡量避免重複): {', '.join(sorted(used_norm)) if used_norm else '(none)'}\n"
            "BIOS可見電壓值:\n"
            f"{chr(10).join(values_lines) if values_lines else '- (no parsed values)'}\n"
            "只輸出一行：PICK|<alias-or-UNKNOWN>|<high|medium|low>|<reason>"
        )
        out = _run_vision_analyze(target_path, prompt)
        if not out:
            continue
        first = str(out).strip().splitlines()[0].strip()
        m = re.match(r"^PICK\|([^|]+)\|(high|medium|low)\|(.+)$", first, re.IGNORECASE)
        if not m:
            continue

        alias = m.group(1).strip()
        confidence = m.group(2).strip().lower()
        reason = m.group(3).strip()
        if not alias or alias.upper() == "UNKNOWN":
            continue
        alias_norm = alias.upper().strip()
        if alias_norm in used_norm:
            continue
        if alias_norm not in candidate_norm:
            continue
        if confidence != "high":
            continue

        used_norm.add(alias_norm)
        added.append({
            "report_name": report_name,
            "channel_id": channel_id,
            "alias": alias,
            "confidence": "high",
            "reason": f"AI_INFERRED_MISMATCH:{reason}",
            "evidence_image": str(target_item.get("filename") or target_path.name),
        })

    return added


def _build_voltage_alias_bridge(ec_base: dict, bios_cache: dict) -> dict:
    """Item-3 skill bridge: ec-base + bios-cache -> alias_map + unresolved (strict schema)."""
    bios_items = bios_cache.get("items") if isinstance(bios_cache, dict) else []
    if not isinstance(bios_items, list):
        bios_items = []

    image_label_map: dict[str, list[str]] = {}
    merged_labels: set[str] = set()
    measured_labels: set[str] = set()
    for item in bios_items:
        if not isinstance(item, dict):
            continue
        image = item.get("filename") or item.get("path") or ""
        analysis_text = item.get("analysis_text") or ""
        labels = []
        labels.extend(_extract_voltage_label_hints(analysis_text))

        # Strong signal: parsed/measured voltage-value pairs from analysis text.
        for rec in _extract_voltage_value_hints(str(analysis_text)):
            if isinstance(rec, dict):
                lb = str(rec.get("label") or "").strip().upper()
                if lb:
                    labels.append(lb)
                    measured_labels.add(lb)

        # Backward compatibility: keep explicit hints if present in cache.
        vv = item.get("voltage_value_hints")
        if isinstance(vv, list):
            for rec in vv:
                if isinstance(rec, dict):
                    lb = str(rec.get("label") or "").strip().upper()
                    if lb:
                        labels.append(lb)
                        measured_labels.add(lb)
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
            "+9V": {"+9V", "9V", "+V9"},
            "+3.3V": {"+3.3V", "3V3", "+V3.3"},
            "VBAT": {"VBAT", "+VBAT"},
        }.get(alias, set())
        for image, labels in image_label_map.items():
            if any(t in labels for t in targets):
                return image
        return None

    alias_map: list[dict] = []
    unresolved: list[dict] = []

    query_key = ec_base.get("query_key") if isinstance(ec_base, dict) else None
    is_aimb_nct6126d = bool(
        isinstance(query_key, dict)
        and str(query_key.get("product_name") or "").strip().upper() == "AIMB"
        and str(query_key.get("chip_name") or "").strip().upper() == "NCT6126D"
    )
    v5sb_to_v33_evidence_image: str | None = None
    if is_aimb_nct6126d:
        for bios_item in bios_items:
            if not isinstance(bios_item, dict):
                continue
            image_name = str(bios_item.get("filename") or bios_item.get("path") or "").strip()
            if image_name and not Path(image_name).name.lower().startswith("circuit"):
                continue
            route_hints = bios_item.get("voltage_route_hints")
            if not isinstance(route_hints, list):
                continue
            for hint in route_hints:
                if not isinstance(hint, dict):
                    continue
                source_item = str(hint.get("source_item") or "").strip().upper()
                actual_rail = str(hint.get("actual_rail") or "").strip().upper().replace(" ", "")
                evidence_level = str(hint.get("evidence_level") or "").strip().upper()
                if (source_item in {"V5SB", "VIN0", "HWM_VOLTAGE_5VSB"}
                        and actual_rail in {"+3.3V", "3.3V", "3V3", "+3V3", "V33"}
                        and evidence_level in {"NET_LEVEL_CONFIRMED", "SCHEMATIC_CONFIRMED"}):
                    v5sb_to_v33_evidence_image = Path(image_name).name if image_name else None
                    break
            if v5sb_to_v33_evidence_image is not None:
                break

    for item in ec_base.get("items") or []:
        if not isinstance(item, dict):
            continue
        report_name = str(item.get("report_name") or "")
        channel_id = str(item.get("channel_id") or "")
        current_item = _canonical_voltage_item_name(
            report_name,
            str(item.get("item_name") or item.get("channel_name") or ""),
        )
        if v5sb_to_v33_evidence_image is not None and current_item == "V5SB":
            alias_map.append({
                "report_name": report_name,
                "channel_id": channel_id,
                "alias": "+3.3V",
                "confidence": "high",
                "item_name_override": "V33",
                "reason": "AIMB_NCT6126D_V5SB_RENAMED_TO_V33_BY_SCHEMATIC",
                "evidence_image": v5sb_to_v33_evidence_image,
            })
            continue
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

    def _canon_label(x: str) -> str:
        y = (x or "").upper().strip().replace(" ", "")
        y = y.replace("+V5SB", "+5VSB").replace("+V12", "+12V").replace("+V9", "+9V").replace("+V5", "+5V")
        y = y.replace("+V3.3", "+3.3V").replace("+VBAT", "VBAT")
        return y

    # AI fallback for mismatched naming: let model reason from image + values.
    if unresolved:
        cand = sorted({_canon_label(x) for x in measured_labels if str(x).strip()} or
                      {_canon_label(x) for x in merged_labels if str(x).strip()})
        used = {_canon_label(str(x.get("alias") or "")) for x in alias_map if isinstance(x, dict)}
        ai_added = _ai_resolve_unmatched_voltage_aliases(unresolved, bios_items, cand, used)
        if ai_added:
            resolved_keys = {
                (str(x.get("report_name") or ""), str(x.get("channel_id") or ""))
                for x in ai_added if isinstance(x, dict)
            }
            unresolved = [
                x for x in unresolved
                if (str(x.get("report_name") or ""), str(x.get("channel_id") or "")) not in resolved_keys
            ]
            alias_map.extend(ai_added)

    if len(unresolved) == 1:
        known_aliases = {"+12V", "+9V", "+5V", "+5VSB", "+3.3V", "VBAT"}
        used_aliases = {_canon_label(str(x.get("alias") or "")) for x in alias_map if isinstance(x, dict)}

        measured_known = {_canon_label(x) for x in measured_labels}
        measured_known = {x for x in measured_known if x in known_aliases}
        remain_measured = measured_known - used_aliases

        merged_known = {_canon_label(x) for x in merged_labels}
        merged_known = {x for x in merged_known if x in known_aliases}
        remain_merged = merged_known - used_aliases

        inferred_aliases = remain_measured if len(remain_measured) == 1 else remain_merged
        if len(inferred_aliases) == 1:
            inferred = next(iter(inferred_aliases))
            tail = unresolved.pop()
            alias_map.append({
                "report_name": tail.get("report_name", ""),
                "channel_id": tail.get("channel_id", ""),
                "alias": inferred,
                "confidence": "high",
                "reason": "BIOS_SINGLETON_REMAINDER_MATCH",
                "evidence_image": first_evidence_image(inferred),
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

    Default behavior keeps item_name from report/channel semantics and only fills
    disp_name(Name). One exception: when BIOS clearly shows VBAT, report_name is
    HWM_VOLTAGE_VBATLI, and no true VBAT key exists in this section, promote that
    row key from VBATLI -> VBAT.
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
    if not isinstance(alias_map, list):
        alias_map = []
    unresolved = bridge.get("unresolved") if isinstance(bridge, dict) else []
    if not isinstance(unresolved, list):
        unresolved = []

    by_key: dict[tuple[str, str], str] = {}
    by_report: dict[str, str] = {}
    item_override_by_key: dict[tuple[str, str], str] = {}
    item_override_by_report: dict[str, str] = {}
    query_key = query_result.get("query_key") if isinstance(query_result, dict) else None
    is_aimb_nct6126d = bool(
        isinstance(query_key, dict)
        and str(query_key.get("product_name") or "").strip().upper() == "AIMB"
        and str(query_key.get("chip_name") or "").strip().upper() == "NCT6126D"
    )
    override_reason = "AIMB_NCT6126D_V5SB_RENAMED_TO_V33_BY_SCHEMATIC"
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
        item_name_override = str(item.get("item_name_override") or "").strip().upper()
        reason = str(item.get("reason") or "").strip()
        if report_name and channel_id:
            by_key[(report_name, channel_id)] = alias
        if report_name:
            by_report[report_name] = alias
        if (is_aimb_nct6126d
                and item_name_override == "V33"
                and reason == override_reason):
            if report_name and channel_id:
                item_override_by_key[(report_name, channel_id)] = "V33"
            if report_name:
                item_override_by_report[report_name] = "V33"

    unresolved_keys: set[tuple[str, str]] = set()
    for u in unresolved:
        if not isinstance(u, dict):
            continue
        unresolved_keys.add((
            str(u.get("report_name") or "").strip(),
            str(u.get("channel_id") or "").strip(),
        ))

    has_true_vbat = any(
        _canonical_voltage_item_name(
            str(r.get("report_name") or ""),
            str(r.get("item_name") or r.get("channel_name") or "")
        ) == "VBAT"
        for r in rows if isinstance(r, dict)
    )

    merged = 0
    item_override_count = 0
    rows_to_remove: set[int] = set()
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
            current_item = _canonical_voltage_item_name(
                report_name,
                str(row.get("item_name") or row.get("channel_name") or "")
            )
            if (alias == "VBAT"
                    and current_item == "VBATLI"
                    and not has_true_vbat):
                row["item_name"] = "VBAT"
                has_true_vbat = True
            item_override = item_override_by_key.get((report_name, channel_id))
            if item_override is None:
                item_override = item_override_by_report.get(report_name)
            if item_override == "V33" and current_item == "V5SB":
                existing_v33 = next(
                    (
                        candidate for candidate in rows
                        if candidate is not row
                        and isinstance(candidate, dict)
                        and str(candidate.get("channel_id") or candidate.get("channel") or "").strip() == channel_id
                        and _canonical_voltage_item_name(
                            str(candidate.get("report_name") or ""),
                            str(candidate.get("item_name") or candidate.get("channel_name") or ""),
                        ) == "V33"
                    ),
                    None,
                )
                if existing_v33 is not None:
                    existing_v33["disp_name"] = alias
                    rows_to_remove.add(id(row))
                else:
                    row["item_name"] = "V33"
                item_override_count += 1
            merged += 1

    if rows_to_remove:
        rows[:] = [row for row in rows if id(row) not in rows_to_remove]

    # Rows still without a BIOS alias keep their key and an empty Name
    # (orchestrator 10.5 rules 4 and 10): the key comes from DB/probe and decides
    # the SUSI ID, so a missing display name must never rename it.
    unresolved_rows: list[dict] = []
    if unresolved_keys:
        for r in rows:
            if not isinstance(r, dict):
                continue
            rk = (
                str(r.get("report_name") or "").strip(),
                str(r.get("channel_id") or r.get("channel") or "").strip(),
            )
            if rk in unresolved_keys:
                unresolved_rows.append({
                    "report_name": rk[0],
                    "channel_id": rk[1],
                    "item_name": _canonical_voltage_item_name(
                        rk[0], str(r.get("item_name") or r.get("channel_name") or "")
                    ),
                })

    query_result["alias_merged_count"] = merged
    query_result["alias_unresolved_count"] = len(unresolved_keys)
    query_result["alias_unresolved"] = unresolved_rows
    query_result["alias_item_override_count"] = item_override_count
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
        "HWM_VOLTAGE_OEM0": "VOEM0",
        "HWM_VOLTAGE_OEM1": "VOEM1",
        "HWM_VOLTAGE_OEM2": "VOEM2",
        "HWM_VOLTAGE_OEM3": "VOEM3",
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


VOLTAGE_OEM_SLOTS = ("VOEM0", "VOEM1", "VOEM2", "VOEM3")


def _assign_oem_slots_for_unmapped_voltage_rows(query_result: dict) -> dict:
    """EC route: park rows that DB + probe cannot place on a SUSI voltage ID.

    A row keeps its key whenever that key has a SUSI voltage ID (the same table
    the Machine-B builder uses, aligned with Susi4.h). Only rows whose key has no
    ID are moved to the next free VOEM0..VOEM3 slot; the BIOS name, if any,
    stays as Name, otherwise "OEM Voltage". BIOS names never decide the key.
    """
    rows = query_result.get("rows")
    if not isinstance(rows, list):
        return query_result

    def key_of(row: dict) -> str:
        return _canonical_voltage_item_name(
            str(row.get("report_name") or ""),
            str(row.get("item_name") or row.get("channel_name") or ""),
        ).strip().upper()

    used = {key_of(r) for r in rows if isinstance(r, dict)}
    free_slots = [slot for slot in VOLTAGE_OEM_SLOTS if slot not in used]
    assigned: list[dict] = []
    unplaced: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = key_of(row)
        if key in VOLTAGE_API_INDEX:
            continue
        record = {
            "report_name": str(row.get("report_name") or ""),
            "channel_id": str(row.get("channel_id") or row.get("channel") or ""),
            "from_key": key,
        }
        if not free_slots:
            unplaced.append(record)
            continue
        slot = free_slots.pop(0)
        row["item_name"] = slot
        if not str(row.get("disp_name") or "").strip():
            row["disp_name"] = "OEM Voltage"
        assigned.append({**record, "slot": slot})

    query_result["voltage_oem_slots"] = assigned
    query_result["voltage_unplaced"] = unplaced
    return query_result


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


def _map_bios_voltage_label_to_item_alias(label: str) -> tuple[str, str] | None:
    """Map BIOS-visible voltage label text to INI item key + Name(alias)."""
    s = str(label or "").strip().upper()
    if not s:
        return None

    s = s.replace(" ", "")
    s = s.replace("＋", "+")

    # standby rails first (avoid 5VSB being matched by plain 5V rule)
    if any(t in s for t in ["5VSB", "5VSTANDBY", "+5VSB", "+5VSTANDBY"]):
        return "V5SB", "+5VSB"
    if any(t in s for t in ["3.3VSB", "3VSB", "+3.3VSB", "+3VSB"]):
        return "V3SB", "+3VSB"

    if "VCORE2" in s:
        return "VCORE2", "VCORE2"
    if "VCORE" in s:
        return "VCORE", "VCORE"
    if "VBAT" in s:
        return "VBAT", "VBAT"
    if "VTT" in s:
        return "VTT", "VTT"
    if "12V" in s:
        return "V120", "+12V"
    if "9V" in s:
        return "DC", "+9V"
    if "3.3V" in s or "3V3" in s:
        return "V33", "+3.3V"
    # keep plain 5V after 5VSB rule
    if s in {"+5V", "5V", "+V5"} or ("5V" in s and "SB" not in s and "S5" not in s):
        return "V50", "+5V"

    return None


def _extract_bios_voltage_name_hints(cache_path: Path) -> dict:
    """Extract BIOS-visible voltage item set + alias names for SuperIO v2 refine."""
    out: dict = {
        "visible_items": [],
        "alias_by_item": {},
        "evidence_images": {},
    }
    if not cache_path.exists():
        return out
    try:
        data = _load_json(cache_path)
    except Exception:
        return out

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return out

    visible: set[str] = set()
    alias_by_item: dict[str, str] = {}
    evidence_images: dict[str, list[str]] = {}

    for it in items:
        if not isinstance(it, dict):
            continue
        fn = str(it.get("filename") or "")
        if not fn.lower().startswith("bios"):
            continue

        labels_with_priority: list[tuple[str, int]] = []

        # higher confidence: explicit value hints in BIOS text
        vv = it.get("voltage_value_hints")
        if isinstance(vv, list):
            for rec in vv:
                if isinstance(rec, dict):
                    lb = str(rec.get("label") or "").strip()
                    if lb:
                        labels_with_priority.append((lb, 0))

        # medium confidence: parsed label hints
        for lb in _extract_voltage_label_hints(str(it.get("analysis_text") or "")):
            labels_with_priority.append((lb, 1))
        vh = it.get("voltage_label_hints")
        if isinstance(vh, list):
            for lb in vh:
                s = str(lb or "").strip()
                if s:
                    labels_with_priority.append((s, 1))

        for lb, _prio in labels_with_priority:
            mapped = _map_bios_voltage_label_to_item_alias(lb)
            if not mapped:
                continue
            item_key, alias = mapped
            visible.add(item_key)

            # first-wins is stable because priority-0 entries are inserted first.
            if item_key not in alias_by_item:
                alias_by_item[item_key] = alias

            imgs = evidence_images.setdefault(item_key, [])
            if fn and fn not in imgs:
                imgs.append(fn)

    out["visible_items"] = sorted(visible)
    out["alias_by_item"] = alias_by_item
    out["evidence_images"] = evidence_images
    return out


def _probe_ok_voltage_measurements(probe_path: Path | None) -> dict[str, dict]:
    """Parse probe HWM Voltage block and return item-key indexed OK measurements."""
    if not probe_path or not probe_path.exists():
        return {}

    out: dict[str, dict] = {}
    in_voltage_block = False

    with open(probe_path, "r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("==="):
                in_voltage_block = ("HWM Voltage" in line)
                continue
            if not in_voltage_block:
                continue

            m = re.match(r"^\[OK\]\s+HWM_VOLTAGE_([A-Z0-9_]+)\b.*\bValue=([0-9]+)\s*mV\b", line)
            if not m:
                continue
            suffix = m.group(1)
            mv = _parse_int_value(m.group(2))
            if mv is None:
                continue

            rn = f"HWM_VOLTAGE_{suffix}"
            item_key = _canonical_voltage_item_name(rn, suffix)
            if not item_key or item_key in out:
                continue
            out[item_key] = {
                "report_name": suffix,
                "mv": mv,
            }

    return out


def _apply_superio_voltage_v2_refine(result: dict,
                                     bios_cache_path: Path | None,
                                     probe_path: Path | None) -> dict:
    """Non-EC SuperIO v2 refine.

    Keep only BIOS-visible voltage items that are also probe-OK, and backfill
    Name(alias) with BIOS display labels.
    """
    if result.get("status") != "FOUND":
        return result
    rows = result.get("rows")
    if not isinstance(rows, list) or not rows:
        return result
    if not bios_cache_path or not probe_path:
        return result

    bios_hints = _extract_bios_voltage_name_hints(bios_cache_path)
    probe_ok = _probe_ok_voltage_measurements(probe_path)

    visible_items = set(str(x) for x in bios_hints.get("visible_items") or [])
    alias_by_item = bios_hints.get("alias_by_item") or {}
    if not visible_items or not probe_ok:
        return result

    kept: list[dict] = []
    dropped: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        report_name = str(row.get("report_name") or row.get("item_name") or "")
        fallback = str(row.get("item_name") or row.get("channel_name") or "")
        item_key = _canonical_voltage_item_name(report_name, fallback)

        out_row = dict(row)
        if item_key:
            out_row["item_name"] = item_key

        in_bios = item_key in visible_items if item_key else False
        in_probe = item_key in probe_ok if item_key else False
        if in_bios and in_probe:
            alias = alias_by_item.get(item_key)
            if alias:
                out_row["disp_name"] = alias
            kept.append(out_row)
        else:
            dropped.append({
                "item_name": item_key,
                "report_name": str(row.get("report_name") or ""),
                "channel_id": str(row.get("channel_id") or row.get("channel") or ""),
                "reason": "NOT_IN_BIOS" if not in_bios else "NOT_OK_IN_PROBE",
            })

    out = dict(result)
    out["rows"] = kept
    out["row_count"] = len(kept)
    out["status"] = "FOUND" if kept else "SECTION_EMPTY"
    out["superio_v2"] = {
        "enabled": True,
        "route": "SUPERIO_V2_BIOS_PROBE_ALIAS",
        "bios_visible_count": len(visible_items),
        "probe_ok_count": len(probe_ok),
        "kept_count": len(kept),
        "dropped_count": len(dropped),
        "dropped": dropped,
        "evidence_images": bios_hints.get("evidence_images") or {},
    }
    return out


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


def _is_superio_temperature_seed_chip(chip_name: str) -> bool:
    """Return True for non-EC SIO families that should emit temperature seed rows."""
    c = _norm_chip_name(chip_name)
    return c.startswith(("NCT6694B", "NCT6106D", "NCT6116D", "NCT6126D", "NCT6776D"))


def _apply_superio_temperature_seed_rows(rows: list[dict]) -> list[dict]:
    """Normalize non-EC SIO HWM.Temperature rows for v1 seed INI output."""
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        r = dict(row)
        report_name = str(r.get("report_name") or r.get("item_name") or "")
        fallback = str(r.get("item_name") or r.get("channel_name") or "")
        item = _temp_item_name_from_report(report_name) or fallback
        if item:
            r["item_name"] = item
        r["channel"] = r.get("channel") or r.get("channel_id") or ""
        r["option"] = r.get("option") or r.get("options") or ""
        out.append(r)
    return out


def _map_bios_temp_label_to_item_alias(label: str) -> tuple[str, str] | None:
    s = (label or "").upper().strip()
    if not s:
        return None
    s = re.sub(r"\s+", " ", s)

    if "CPU" in s and "TEMPERATURE" in s:
        return "TCPU", "CPU Temperature"
    if ("SYSTEM" in s or re.search(r"\bSYS\b", s)) and "TEMPERATURE" in s:
        return "TSYS", "System Temperature"
    if "CHIPSET" in s and "TEMPERATURE" in s:
        return "TCHIPSET", "Chipset Temperature"
    if ("GRAPHIC" in s or "GPU" in s) and "TEMPERATURE" in s:
        return "GRAPHIC", "Graphic Temperature"

    m_oem = re.search(r"\bOEM\s*([0-6])\b", s)
    if m_oem and "TEMPERATURE" in s:
        idx = m_oem.group(1)
        return f"TOEM{idx}", f"OEM{idx} Temperature"
    return None


def _extract_bios_temperature_name_hints(cache_path: Path) -> dict:
    """Extract BIOS-visible temperature item set + alias names for SuperIO v2 refine."""
    out: dict = {
        "visible_items": [],
        "alias_by_item": {},
        "value_c_by_item": {},
        "evidence_images": {},
    }
    if not cache_path.exists():
        return out
    try:
        data = _load_json(cache_path)
    except Exception:
        return out

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return out

    visible: set[str] = set()
    alias_by_item: dict[str, str] = {}
    value_c_by_item: dict[str, float] = {}
    evidence_images: dict[str, list[str]] = {}

    for it in items:
        if not isinstance(it, dict):
            continue
        fn = str(it.get("filename") or "")
        if not fn.lower().startswith("bios"):
            continue

        labels_with_priority: list[tuple[str, int, float | None]] = []

        tv = it.get("temperature_value_hints")
        if isinstance(tv, list):
            for rec in tv:
                if isinstance(rec, dict):
                    lb = str(rec.get("label") or "").strip()
                    vv = rec.get("value")
                    vnum = float(vv) if isinstance(vv, (int, float)) else None
                    if lb:
                        labels_with_priority.append((lb, 0, vnum))

        txt = str(it.get("analysis_text") or "")
        for m in re.finditer(r"TEMP_VALUE:\s*([^=\n]+?)\s*=\s*([0-9]+(?:\.[0-9]+)?)\s*C\b", txt, re.IGNORECASE):
            lb = str(m.group(1) or "").strip()
            try:
                vnum = float(m.group(2))
            except Exception:
                vnum = None
            labels_with_priority.append((lb, 1, vnum))

        for lb, _prio, vc in labels_with_priority:
            mapped = _map_bios_temp_label_to_item_alias(lb)
            if not mapped:
                continue
            item_key, alias = mapped
            visible.add(item_key)

            if item_key not in alias_by_item:
                alias_by_item[item_key] = alias
            if vc is not None and item_key not in value_c_by_item:
                value_c_by_item[item_key] = vc

            imgs = evidence_images.setdefault(item_key, [])
            if fn and fn not in imgs:
                imgs.append(fn)

    out["visible_items"] = sorted(visible)
    out["alias_by_item"] = alias_by_item
    out["value_c_by_item"] = value_c_by_item
    out["evidence_images"] = evidence_images
    return out


def _probe_ok_temperature_measurements(probe_path: Path | None) -> dict[str, dict]:
    """Parse probe HWM Temperature block and return item-key indexed OK measurements."""
    if not probe_path or not probe_path.exists():
        return {}

    out: dict[str, dict] = {}
    in_temp_block = False

    with open(probe_path, "r", encoding="utf-8-sig") as f:
        for raw in f:
            line = raw.strip()
            if line.startswith("==="):
                in_temp_block = ("HWM Temperature" in line)
                continue
            if not in_temp_block:
                continue

            m = re.match(r"^\[OK\]\s+HWM_TEMP_([A-Z0-9_]+)\b.*\bValue=([0-9]+)(?:\s*\(=\s*([0-9]+(?:\.[0-9]+)?)\s*C\))?", line)
            if not m:
                continue

            suffix = m.group(1)
            rn = f"HWM_TEMP_{suffix}"
            item_key = _temp_item_name_from_report(rn)
            if not item_key or item_key in out:
                continue

            raw_v = _parse_int_value(m.group(2))
            c_v = None
            if m.group(3):
                try:
                    c_v = float(m.group(3))
                except Exception:
                    c_v = None

            out[item_key] = {
                "report_name": rn,
                "raw": raw_v,
                "celsius": c_v,
            }

    return out


def _apply_superio_temperature_v2_refine(result: dict,
                                         bios_cache_path: Path | None,
                                         probe_path: Path | None) -> dict:
    """Non-EC SuperIO HWM.Temperature v2 refine.

    Keep only BIOS-visible temperature items that are also probe-OK, and backfill
    Name(alias) with BIOS display labels.
    """
    if result.get("status") != "FOUND":
        return result
    rows = result.get("rows")
    if not isinstance(rows, list) or not rows:
        return result
    if not bios_cache_path or not probe_path:
        return result

    bios_hints = _extract_bios_temperature_name_hints(bios_cache_path)
    probe_ok = _probe_ok_temperature_measurements(probe_path)

    visible_items = set(str(x) for x in bios_hints.get("visible_items") or [])
    alias_by_item = bios_hints.get("alias_by_item") or {}
    if not visible_items or not probe_ok:
        return result

    kept: list[dict] = []
    dropped: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        report_name = str(row.get("report_name") or row.get("item_name") or "")
        item_key = _temp_item_name_from_report(report_name)
        if not item_key:
            item_key = str(row.get("item_name") or "").strip().upper()

        out_row = dict(row)
        if item_key:
            out_row["item_name"] = item_key

        in_bios = item_key in visible_items if item_key else False
        in_probe = item_key in probe_ok if item_key else False
        if in_bios and in_probe:
            alias = alias_by_item.get(item_key)
            if alias:
                out_row["disp_name"] = alias
            kept.append(out_row)
        else:
            dropped.append({
                "item_name": item_key,
                "report_name": str(row.get("report_name") or ""),
                "channel_id": str(row.get("channel_id") or row.get("channel") or ""),
                "reason": "NOT_IN_BIOS" if not in_bios else "NOT_OK_IN_PROBE",
            })

    out = dict(result)
    out["rows"] = kept
    out["row_count"] = len(kept)
    out["status"] = "FOUND" if kept else "SECTION_EMPTY"
    out["superio_temp_v2"] = {
        "enabled": True,
        "route": "SUPERIO_TEMP_V2_BIOS_PROBE_ALIAS",
        "bios_visible_count": len(visible_items),
        "probe_ok_count": len(probe_ok),
        "kept_count": len(kept),
        "dropped_count": len(dropped),
        "dropped": dropped,
        "evidence_images": bios_hints.get("evidence_images") or {},
        "bios_value_c_by_item": bios_hints.get("value_c_by_item") or {},
    }
    return out


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


def _coerce_display_hint(value: object) -> str:
    """Return only the display label from a hint payload.

    Hint files may carry structured evidence, but INI Name fields must contain
    a plain label string rather than a Python dict representation.
    """
    if isinstance(value, dict):
        value = value.get("label") or value.get("name") or value.get("alias") or ""
    elif isinstance(value, str) and value.lstrip().startswith("{"):
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            value = parsed.get("label") or parsed.get("name") or parsed.get("alias") or ""
    return str(value or "").strip()


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
    out: dict[str, str] = {}
    for k, v in by_key.items():
        kk = str(k or "").strip().upper()
        vv = _coerce_display_hint(v)
        if kk and vv:
            out[kk] = vv
    return out


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


def _extract_temperature_name_hints_from_bios_cache(cache_path: Path) -> dict:
    """Extract high-confidence temperature display labels from BIOS cache text."""
    payload: dict = {
        "source_glob": "bios*.png|jpg|jpeg",
        "status": "NO_BIOS_IMAGES",
        "images_analyzed": [],
        "by_key": {},
        "evidence": {},
    }
    if not cache_path.exists():
        return payload
    try:
        data = _load_json(cache_path)
    except Exception:
        return payload

    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return payload

    bios_items: list[dict] = []
    for it in items:
        if not isinstance(it, dict):
            continue
        fn = str(it.get("filename") or "")
        if fn.lower().startswith("bios"):
            bios_items.append(it)

    if not bios_items:
        return payload

    payload["images_analyzed"] = [str(it.get("filename") or "") for it in bios_items]
    by_key: dict[str, str] = {}
    evidence: dict[str, dict] = {}

    for it in bios_items:
        fn = str(it.get("filename") or "")
        txt = str(it.get("analysis_text") or "")
        if not txt.strip():
            continue
        u = txt.upper()

        if "TCPU" not in by_key and re.search(r"\bCPU\s*TEMPERATURE\b", u):
            by_key["TCPU"] = "CPU Temperature"
            evidence["TCPU"] = {"image": fn, "label": "CPU Temperature"}

        if "TSYS" not in by_key and re.search(r"\b(SYSTEM|SYS)\s*TEMPERATURE\b", u):
            by_key["TSYS"] = "System Temperature"
            evidence["TSYS"] = {"image": fn, "label": "System Temperature"}

    payload["by_key"] = by_key
    payload["evidence"] = evidence
    payload["status"] = "DONE" if by_key else "NO_CONFIDENT_LABELS"
    return payload


def _write_project_temperature_name_hints(project_dir: Path, project_name: str, bios_cache_path: Path) -> Path:
    payload = _extract_temperature_name_hints_from_bios_cache(bios_cache_path)
    payload["project"] = project_name
    path = project_dir / f"{project_name}-temperature-name-hints.json"

    # Preserve explicit/manual hints if they already exist.
    if path.exists():
        try:
            prev = _load_json(path)
            prev_by_key = prev.get("by_key") if isinstance(prev, dict) else None
            if isinstance(prev_by_key, dict):
                for k, v in prev_by_key.items():
                    kk = str(k or "").strip().upper()
                    vv = _coerce_display_hint(v)
                    if kk and vv:
                        payload.setdefault("by_key", {})[kk] = vv
                if payload.get("by_key"):
                    payload["status"] = "DONE"

            prev_evidence = prev.get("evidence") if isinstance(prev, dict) else None
            if isinstance(prev_evidence, dict):
                payload.setdefault("evidence", {}).update(prev_evidence)
        except Exception:
            pass

    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _build_hwm_temperature_query_result(db_path: Path, product_name: str, chip_name: str,
                                        probe_path: Path | None, spec: dict | None) -> dict:
    result = query_section(db_path, product_name, chip_name, "HWM.Temperature")
    result["source"] = "PROBE_CHANNELS_BY_HARDWARE_ID"
    report_names = _probe_ok_temperature_report_names(probe_path)
    if not report_names:
        result["status"] = "SECTION_EMPTY"
        result["rows"] = []
        result["row_count"] = 0
        result["error"] = "NO_OK_HWM_TEMPERATURE_IN_PROBE"
        return result

    wanted = set(report_names)
    alias_by_item = _spec_temperature_alias_map(spec)
    db_rows = {str(r.get("report_name") or ""): r for r in result.get("rows") or []
               if isinstance(r, dict)}
    out_rows: list[dict] = []
    missing_reports: list[str] = []
    for rn in report_names:
        row = db_rows.get(rn)
        if row is None:
            missing_reports.append(rn)
            continue
        item_name = _temp_item_name_from_report(rn)
        out = dict(row)
        out["item_name"] = item_name
        out["channel"] = out.get("channel_id", "")
        out["option"] = out.get("options", "")
        # channel_name/report_name are SUSI/internal reference fields, not INI display Name.
        # Only use explicit alias evidence; otherwise keep Name blank.
        out["disp_name"] = alias_by_item.get(item_name.upper(), "")
        out_rows.append(out)

    result["rows"] = out_rows
    result["row_count"] = len(out_rows)
    result["status"] = "FOUND" if out_rows else "SECTION_EMPTY"
    if missing_reports:
        result["missing_report_name_mappings"] = missing_reports
    return result


def _build_ec_voltage_query_result(db_path: Path, product_name: str, chip_name: str,
                                   probe_path: Path | None) -> dict:
    result = query_section(db_path, product_name, chip_name, "HWM.Voltage")
    result["source"] = "EC_PROBE_CHANNELS_BY_HARDWARE_ID"
    report_names = _probe_ok_voltage_report_names(probe_path)
    if not report_names:
        result["status"] = "SECTION_EMPTY"
        result["rows"] = []
        result["row_count"] = 0
        result["error"] = "NO_OK_HWM_VOLTAGE_IN_PROBE"
        return result

    db_rows = {str(r.get("report_name") or ""): r for r in result.get("rows") or []
               if isinstance(r, dict)}
    out_rows: list[dict] = []
    missing_reports: list[str] = []
    for rn in report_names:
        row = db_rows.get(rn)
        if row is None:
            missing_reports.append(rn)
            continue
        out = dict(row)
        out["item_name"] = _canonical_voltage_item_name(rn, out.get("channel_name", ""))
        out["channel"] = out.get("channel_id", "")
        out["option"] = out.get("options", "")
        # channel_name/report_name are SUSI/internal reference fields, not INI display Name.
        # Leave Name blank at this stage; alias backfill is handled by later merge steps.
        out["disp_name"] = ""
        out_rows.append(out)

    result["rows"] = out_rows
    result["row_count"] = len(out_rows)
    result["status"] = "FOUND" if out_rows else "SECTION_EMPTY"
    if missing_reports:
        result["missing_report_name_mappings"] = missing_reports
    return result


def _render_section_lines(section: str, query_result: dict) -> list[str]:
    lines = [f"[{section}]"]
    rows = query_result.get("rows") or []
    hwid = ""
    prod_chip = query_result.get("prod_chip") or {}
    if isinstance(prod_chip, dict):
        hwid = prod_chip.get("hardware_id") or ""

    for idx, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        key = row.get("item_name") or f"ITEM{row.get('id', '')}"
        if section == "HWM.CaseOpen":
            key = f"CO{idx}"
        elif section == "WDT":
            key = f"WDT{idx + 1}"
        elif section == "StorageArea":
            key = f"Area{idx}"
        elif section == "ThermalProtect":
            key = f"TPCH{idx}"
        elif section == "HWM.Current":
            # Normalize DB item names (e.g. CURRENT_OEM0) to INI convention OEM0/OEM1...
            key = f"OEM{idx}"
        elif section == "VGA.Backlight":
            # INI key casing is strict: Backlight1, Backlight2, ...
            key = f"Backlight{idx + 1}"
        elif section == "GPIO" and isinstance(key, str):
            m_gpio_key = re.match(r"^GPIO(\d+)$", key.strip(), re.IGNORECASE)
            if m_gpio_key:
                key = f"GPIO{int(m_gpio_key.group(1)):02d}"
        channel = row.get("channel") or ""
        io_port = row.get("io_port") or ""
        option = row.get("option") or ""
        disp_name = row.get("disp_name") or ""

        if section == "SMBus":
            # [Channel]=[HW],[Channel],[IOPort],[Option],[Name]
            row_hwid = row["hw"] if "hw" in row else hwid
            if any([row_hwid, channel, io_port, option, disp_name]):
                value = f"{row_hwid},{channel},{io_port},{option},"
                if disp_name:
                    value += f"{disp_name}"
            else:
                value = ""
        elif section == "I2C":
            # [Channel]=[HW],[Channel],[IOPort],[Option],[Name]
            # Name field must stay blank by project rule.
            value = f"{hwid},{channel},{io_port},{option},"
        elif section == "VGA.Backlight":
            # [Backlight]=[HW],[Channel],[IOPort],[Option],[Name]
            # Name field must stay blank by project rule.
            value = f"{hwid},{channel},{io_port},{option},"
        elif section == "VGA.Brightness":
            # [Brightness]=[HW],[Channel],[IOPort/Address],[Option],[Max],[Min],[Frequency],[Name]
            # Name field must stay blank by project rule.
            # Prefer DB values (range_max/range_min/frequency); keep legacy fallback only if absent.
            range_max = row.get("range_max")
            range_min = row.get("range_min")
            frequency = row.get("frequency")
            max_v = str(range_max).strip() if range_max is not None else ""
            min_v = str(range_min).strip() if range_min is not None else ""
            freq_v = str(frequency).strip() if frequency is not None else ""
            if not max_v:
                max_v = "100"
            if not min_v:
                min_v = "0"
            if not freq_v:
                freq_v = "0"
            value = f"{hwid},{channel},{io_port},{option},{max_v},{min_v},{freq_v},"
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
            group_v = row.get("group")
            bit_v = row.get("bit")
            group = "" if group_v is None else str(group_v)
            bit = "" if bit_v is None else str(bit_v)
            value = f"{hwid},{channel},{io_port},{option},{group},{bit},"
            if disp_name:
                value += f"{disp_name}"
        elif section in {"HWM.CaseOpen", "WDT", "HWM.Current", "StorageArea", "ThermalProtect"}:
            # Keep the Name field blank for sections whose rows are identified by
            # stable keys rather than user-facing display aliases.
            value = f"{hwid},{channel},{io_port},{option},"
        else:
            value = f"{hwid},{channel},{io_port},{option}"
            if disp_name:
                value += f",{disp_name}"

        lines.append(f"{key}={value}")

    lines.append("")
    return lines


def _norm_chip_name(chip_name: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (chip_name or "").upper())


def _is_superio_voltage_seed_chip(chip_name: str) -> bool:
    """Return True for non-EC SIO families that should emit voltage seed rows.

    New rule: for NCT6694B/NCT61xxD families, HWM.Voltage v1 should keep all
    queried channel rows as seed data, then rely on target-machine report +
    BIOS value/name evidence for item remap in v2.
    """
    c = _norm_chip_name(chip_name)
    return c.startswith(("NCT6694B", "NCT6106D", "NCT6116D", "NCT6126D", "NCT6776D"))


def _apply_superio_voltage_seed_rows(rows: list[dict]) -> list[dict]:
    """Normalize non-EC SIO HWM.Voltage rows for first-pass seed INI output.

    - Keep every queried channel row (do not prune by duplicate/channel heuristics).
    - Prefer conventional INI keys when report_name suffix is recognizable.
    - Keep unresolved rows as-is for follow-up target validation.
    """
    out: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        r = dict(row)
        report_name = str(r.get("report_name") or r.get("item_name") or "")
        fallback = str(r.get("item_name") or r.get("channel_name") or "")
        item = _canonical_voltage_item_name(report_name, fallback)
        if item:
            r["item_name"] = item
        r["channel"] = r.get("channel") or r.get("channel_id") or ""
        r["option"] = r.get("option") or r.get("options") or ""
        out.append(r)
    return out


def _resolve_chip_name_for_section(base_chip_name: str, section: str) -> str:
    """Section-aware chip routing for compound EC/SIO modules.

    Rule lock (susiagent skill): for EIO-300/NCT6694B compound cases,
    route GPIO/I2C/SMBus to NCT6694B; route other sections to EIO-300.
    """
    sec = str(section or "").strip()
    chip_u = str(base_chip_name or "").upper().strip()
    if chip_u in {"EIO-300", "NCT6694B"}:
        if sec in {"GPIO", "I2C", "SMBus"}:
            return "NCT6694B"
        return "EIO-300"
    return base_chip_name


def _resolve_gpio_trace_chip_name(base_chip_name: str) -> str:
    """Resolve the physical chip identity that owns GPIO circuit evidence."""
    return _resolve_chip_name_for_section(base_chip_name, "GPIO")


def _filter_vga_rows_by_probe(result: dict, section: str, probe_spec: dict | None) -> tuple[dict, dict]:
    """Trim DB maximum rows to the supported VGA channel count from full probe."""
    kind_by_section = {
        "VGA.Brightness": "brightness",
        "VGA.Backlight": "backlight",
    }
    kind = kind_by_section.get(section)
    meta = {
        "filter_applied": False,
        "probe_supported_count": None,
        "db_row_count": len(result.get("rows") or []),
        "trimmed_count": 0,
    }
    if kind is None or not isinstance(probe_spec, dict):
        return result, meta

    vga = probe_spec.get("vga")
    channel_spec = vga.get(kind) if isinstance(vga, dict) else None
    if not isinstance(channel_spec, dict) or channel_spec.get("present") is not True:
        return result, meta

    try:
        supported_count = max(0, int(channel_spec.get("count", 0)))
    except (TypeError, ValueError):
        return result, meta

    rows = list(result.get("rows") or [])
    kept = rows[:supported_count]
    filtered = dict(result)
    filtered["rows"] = kept
    filtered["row_count"] = len(kept)
    if result.get("status") == "FOUND" and not kept:
        filtered["status"] = "SECTION_EMPTY"

    meta.update({
        "filter_applied": True,
        "probe_supported_count": supported_count,
        "db_row_count": len(rows),
        "trimmed_count": max(0, len(rows) - len(kept)),
        "probe_channel_ids": list(channel_spec.get("channel_ids") or []),
    })
    return filtered, meta


def _fan_template_by_chip(db_path: Path, chip_name: str) -> dict[str, str]:
    """Resolve HWM.Fan template values.

    For EIO-201/EIO-211/IT-8528/IT-5782, use DB HWM.Fan.Defaults (io_port/options/pulses).
    For NCT6106D/NCT6116D/NCT6126D, keep SuperIO io_port=0x2E.
    Others fall back to EC-like defaults.
    """
    c = _norm_chip_name(chip_name)

    if chip_uses_hwm_fan_defaults(chip_name):
        defaults = {"io_port": "0", "options": "0x80000000", "pulses": "0"}
        try:
            con = sqlite3.connect(str(db_path))
            try:
                db_defaults = load_hwm_fan_defaults(con)
                defaults["io_port"] = str(db_defaults.get("io_port", defaults["io_port"]))
                defaults["options"] = str(db_defaults.get("options", defaults["options"]))
                defaults["pulses"] = str(db_defaults.get("pulses", defaults["pulses"]))
            finally:
                con.close()
        except Exception:
            pass
        return defaults

    if c in {"NCT6106D", "NCT6116D", "NCT6126D"}:
        return {"io_port": "0x2E", "options": "0x80000000", "pulses": "0"}

    return {"io_port": "0", "options": "0x80000000", "pulses": "0"}


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
    if _is_superio_voltage_seed_chip(chip_name):
        return {
            "route": "SUPERIO_SEED_CHANNEL_SCAN",
            "rule": None,
            "pending": False,
            "status": "FOUND",
            "reason": "SIO seed-mode: keep all queried channels for target validation v1",
            "duplicate_channels": _duplicate_channels(rows),
        }

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


def _extract_probe_value(probe_path: Path | None, item_name: str) -> str:
    if not probe_path or not probe_path.exists():
        return ""
    pat = re.compile(
        rf"^\[(?P<st>OK|ERR)\]\s+{re.escape(item_name)}\s+.*?Value=(?P<val>.+?)\s*$",
        re.IGNORECASE,
    )
    try:
        with open(probe_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                m = pat.match(line.rstrip("\n"))
                if not m:
                    continue
                if (m.group("st") or "").upper() != "OK":
                    return ""
                raw = (m.group("val") or "").strip()
                if raw.startswith('"') and raw.endswith('"'):
                    return raw[1:-1]
                return raw
    except Exception:
        return ""
    return ""


def _classify_cpu_platform_from_probe(probe_path: Path | None) -> dict:
    manu = _extract_probe_value(probe_path, "CPU_MANUFACTURER")
    cpu_name = _extract_probe_value(probe_path, "CPU_NAME")
    token = f"{manu} {cpu_name}".upper()

    is_amd = ("AUTHENTICAMD" in token) or (" AMD" in f" {token}")
    is_intel = ("GENUINEINTEL" in token) or ("INTEL" in token)

    return {
        "cpu_manufacturer": manu,
        "cpu_name": cpu_name,
        "is_amd": is_amd,
        "is_intel": is_intel,
    }


def _parse_spd_idx_candidates(spd_report_path: Path | None) -> list[int]:
    if not spd_report_path or not spd_report_path.exists():
        return []

    summary_re = re.compile(r"SUMMARY:\s*idx value\(s\) with a detected DIMM:\s*(.*)$", re.IGNORECASE)
    confirmed_re = re.compile(r"=>\s*idx\s*=\s*(\d+)\s*:.*valid SPD signature", re.IGNORECASE)

    out: list[int] = []
    seen: set[int] = set()

    try:
        with open(spd_report_path, "r", encoding="utf-8-sig") as f:
            for line in f:
                s = line.strip()
                m = summary_re.search(s)
                if m:
                    payload = (m.group(1) or "").strip()
                    if payload and payload.lower() not in {"none", "n/a", "na", "-"}:
                        for tok in re.findall(r"\d+", payload):
                            iv = int(tok)
                            if iv not in seen:
                                seen.add(iv)
                                out.append(iv)
                    continue

                m2 = confirmed_re.search(s)
                if m2:
                    iv = int(m2.group(1))
                    if iv not in seen:
                        seen.add(iv)
                        out.append(iv)
    except Exception:
        return []

    return out


def _find_spd_idx_probe_report(project_dir: Path, project_name: str) -> Path | None:
    candidates = [
        project_dir / f"{project_name}_susi_spd_idx_probe_report.txt",
        project_dir / "susi_spd_idx_probe_report.txt",
    ]
    for p in candidates:
        if p.exists():
            return p

    try:
        for p in sorted(project_dir.iterdir()):
            if not p.is_file():
                continue
            n = p.name.lower()
            if "spd" in n and "idx" in n and n.endswith(".txt"):
                return p
    except Exception:
        return None
    return None


def _build_i2c_query_result(db_path: Path, product_name: str, chip_name: str,
                            probe_spec: dict | None, spec: dict | None) -> dict:
    features = spec.get("features") if isinstance(spec, dict) else None
    i2c_enabled = isinstance(features, dict) and features.get("i2c") is True
    if not i2c_enabled:
        return {
            "query_key": {
                "product_name": product_name,
                "chip_name": chip_name,
                "section": "I2C",
            },
            "status": "SECTION_EMPTY",
            "prod_chip": None,
            "rows": [],
            "row_count": 0,
            "source": "SPEC_FEATURE_GATE",
            "i2c_feature_enabled": False,
            "reason": "spec.features.i2c is not true; skip I2C generation",
        }

    result = query_section(
        db_path=db_path,
        product_name=product_name,
        chip_name=chip_name,
        section="I2C",
    )
    result["source"] = "CONFIG_DB_I2C"
    result["i2c_feature_enabled"] = True
    if result.get("status") != "FOUND":
        return result

    probe_buses = probe_spec.get("i2c_buses") if isinstance(probe_spec, dict) else None
    if not isinstance(probe_buses, list):
        probe_buses = []

    probe_bus_names = {str(bus.get("name") or "").strip().upper() for bus in probe_buses}
    probe_bus_ids = {
        bus.get("probe_id")
        for bus in probe_buses
        if isinstance(bus.get("probe_id"), int) and not isinstance(bus.get("probe_id"), bool)
    }

    rows: list[dict] = []
    skipped_oem_ids: list[int] = []
    for db_row in result.get("rows") or []:
        if not isinstance(db_row, dict):
            continue
        # Channel values come only from config_new.db; the probe never
        # composes a channel, it may only remove unsupported DB rows.
        encoded_channel = _parse_int_value(str(db_row.get("channel") or "").strip())
        if encoded_channel is None:
            continue
        bus_id = encoded_channel - 0x80000000 if encoded_channel >= 0x80000000 else encoded_channel
        slot = bus_id + 1
        if slot < 1 or slot > 5:
            skipped_oem_ids.append(bus_id)
            continue

        # config_new.db is the maximum topology. When the full probe has
        # enumerated supported buses, intersect the DB rows with that
        # probe result so unsupported OEM channels are not emitted.
        if probe_buses:
            db_report_name = str(db_row.get("report_name") or "").strip().upper()
            is_supported = (
                db_report_name in probe_bus_names
                if db_report_name
                else bus_id in probe_bus_ids
            )
            if not is_supported:
                skipped_oem_ids.append(bus_id)
                continue

        row = dict(db_row)
        # INI keys are slots; the tuple keeps the encoded SUSI bus ID.
        # Reusing Channel1 for every DB row creates invalid duplicate keys.
        row["item_name"] = f"Channel{slot}"
        row["i2c_channel_source"] = "DB"
        rows.append(row)

    result["rows"] = rows
    result["row_count"] = len(rows)
    result["i2c_skipped_oem_ids"] = sorted(set(skipped_oem_ids))
    if rows:
        result["status"] = "FOUND"
        result["reason"] = "I2C rows from config_new.db filtered by full probe supported buses"
    else:
        result["status"] = "SECTION_EMPTY"
        result["reason"] = "no config_new.db I2C row remains after full probe filtering"
    return result


def _build_smbus_query_result(
    db_path: Path,
    product_name: str,
    chip_name: str,
    probe_path: Path | None,
    project_dir: Path,
    project_name: str,
    spec: dict | None = None,
) -> dict:
    result: dict = {
        "query_key": {
            "product_name": product_name,
            "chip_name": chip_name,
            "section": "SMBus",
        },
        "status": None,
        "prod_chip": _load_prod_chip_record(db_path, product_name, chip_name),
        "rows": [],
        "row_count": 0,
    }

    def _intel_oem_idxs_from_full_probe(p: Path) -> list[int]:
        """Parse SMBUS_OEMn EXISTS from full probe report for Intel Channel2+ mapping."""
        if not p.exists():
            return []
        idxs: set[int] = set()
        pat = re.compile(r"SMBUS_OEM(\d+).*?:\s*EXISTS\b", re.IGNORECASE)
        for raw in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            m = pat.search(raw)
            if not m:
                continue
            try:
                idxs.add(int(m.group(1)))
            except Exception:
                continue
        return sorted(idxs)

    def _channel_rows(
        idx: int,
        *,
        intel_oem_idxs: list[int] | None = None,
        amd_secondary_idx4: bool = False,
        fixed_channel2: dict | None = None,
    ) -> list[dict]:
        rows: list[dict] = []
        prod_chip = result.get("prod_chip") or {}
        ec_hwid = ""
        if isinstance(prod_chip, dict):
            ec_hwid = str(prod_chip.get("hardware_id") or "").strip()

        for i in range(1, 6):
            if i == 1:
                rows.append({
                    "item_name": f"Channel{i}",
                    "hw": "0x00000001",
                    "channel": str(idx),
                    "io_port": "0",
                    "option": "0xA0000000",
                    "disp_name": "",
                })
                continue

            rows.append({
                "item_name": f"Channel{i}",
                "hw": "",
                "channel": "",
                "io_port": "",
                "option": "",
                "disp_name": "",
            })

        # Intel extension: Channel2+ from full probe SMBUS_OEMn EXISTS
        if intel_oem_idxs:
            for oem_idx in intel_oem_idxs:
                ch_pos = oem_idx + 2  # OEM0 -> Channel2, OEM1 -> Channel3, ...
                if ch_pos < 2 or ch_pos > 5:
                    continue
                channel_val = f"0x{(0x80000000 + oem_idx):08X}"
                rows[ch_pos - 1] = {
                    "item_name": f"Channel{ch_pos}",
                    "hw": ec_hwid,
                    "channel": channel_val,
                    "io_port": "0",
                    "option": "0xA0000000",
                    "disp_name": "",
                }

        # AMD rule: if Channel1 idx in 0..3, Channel2 is fixed idx=4; if idx=4, no Channel2.
        if amd_secondary_idx4:
            rows[1] = {
                "item_name": "Channel2",
                "hw": ec_hwid,
                "channel": f"0x{(0x80000000 + 4):08X}",
                "io_port": "0",
                "option": "0xA0000000",
                "disp_name": "",
            }

        # NCT6694B EC/SOC SMBus policy: Channel2 may be forced by SMBus table fixed row.
        if isinstance(fixed_channel2, dict):
            rows[1] = {
                "item_name": "Channel2",
                "hw": str(fixed_channel2.get("hw") or ec_hwid),
                "channel": str(fixed_channel2.get("channel") or ""),
                "io_port": str(fixed_channel2.get("io_port") or ""),
                "option": str(fixed_channel2.get("option") or ""),
                "disp_name": "",
            }
        return rows

    cpu = _classify_cpu_platform_from_probe(probe_path)
    result["cpu"] = cpu

    chip_norm = _norm_chip_name(chip_name)
    is_sio_route = chip_norm.startswith(("NCT6106D", "NCT6116D", "NCT6126D", "NCT6776D"))
    # SMBus policy lock:
    # - SIO route: SMBus remains empty by default.
    # - EC route + Intel: Channel1 must exist even when spec.features.smbus=false.
    if is_sio_route:
        result["status"] = "SECTION_EMPTY"
        result["reason"] = "SIO route: SMBus is empty by policy"
        return result

    features = spec.get("features") if isinstance(spec, dict) else None
    feature_details = spec.get("feature_details") if isinstance(spec, dict) else None
    smbus_enabled = True
    smbus_chip_desc = ""
    if isinstance(features, dict) and isinstance(features.get("smbus"), bool):
        smbus_enabled = bool(features.get("smbus"))
    if isinstance(feature_details, dict):
        smbus_node = feature_details.get("smbus")
        if isinstance(smbus_node, dict):
            chip_raw = smbus_node.get("chip")
            if chip_raw is not None:
                smbus_chip_desc = str(chip_raw).strip()

    result["smbus_feature_enabled"] = smbus_enabled
    result["smbus_feature_chip_desc"] = smbus_chip_desc

    if (not smbus_enabled) and (not cpu.get("is_intel")):
        result["status"] = "SECTION_EMPTY"
        result["reason"] = "spec.features.smbus=false; skip SMBus generation"
        return result

    def _smbus_desc_is_ec_or_soc(desc: str) -> bool:
        d = str(desc or "").upper()
        if not d:
            return False
        # Treat explicit EC/SOC wording or common EC chip family tokens as EC-related.
        ec_tokens = ("EC", "SOC", "EIO", "ITE", "NCT", "MEC", "RDC")
        return any(tok in d for tok in ec_tokens)

    def _load_smbus_fixed_channel2_row() -> dict | None:
        prod_chip = result.get("prod_chip") or {}
        hwid = str(prod_chip.get("hardware_id") or "").strip() if isinstance(prod_chip, dict) else ""
        if not hwid:
            return None
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        try:
            row = con.execute(
                """
                SELECT hardware_id, channel_id, io_port, options
                FROM SMBus
                WHERE hardware_id = ?
                ORDER BY id ASC
                LIMIT 1
                """,
                (hwid,),
            ).fetchone()
            if row is None:
                return None
            channel = str(row["channel_id"] or "").strip()
            io_port = str(row["io_port"] or "").strip()
            option = str(row["options"] or "").strip()
            if not channel or not io_port or not option:
                return None
            return {
                "hw": str(row["hardware_id"] or "").strip(),
                "channel": channel,
                "io_port": io_port,
                "option": option,
            }
        finally:
            con.close()

    ec_related = _smbus_desc_is_ec_or_soc(smbus_chip_desc)
    chip_norm = _norm_chip_name(chip_name)
    is_nct6694b = chip_norm.startswith("NCT6694B")

    # EC/SOC SMBus hint policy:
    # - NCT6694B: Channel2 SMBus-table row is mandatory (pending if absent)
    # - non-NCT6694B EC/SOC chips: opportunistically use SMBus-table row for Channel2 when present
    use_db_fixed_channel2 = smbus_enabled and ec_related
    require_db_fixed_channel2 = is_nct6694b and use_db_fixed_channel2
    fixed_channel2 = _load_smbus_fixed_channel2_row() if use_db_fixed_channel2 else None
    if use_db_fixed_channel2:
        result["smbus_fixed_channel2"] = fixed_channel2
    if require_db_fixed_channel2 and not isinstance(fixed_channel2, dict):
        result["status"] = "PENDING_SMBUS_FIXED_CHANNEL2_DB_ROW"
        result["reason"] = "NCT6694B + EC/SOC SMBus requires fixed Channel2 row from SMBus table"
        return result

    if not probe_path or not probe_path.exists():
        result["status"] = "PENDING_PROBE_FOR_SMBUS_PLATFORM_GATE"
        result["reason"] = "SMBus policy requires full probe report for CPU platform gate"
        return result

    if cpu.get("is_amd"):
        spd_path = _find_spd_idx_probe_report(project_dir, project_name)
        result["spd_idx_probe_report"] = str(spd_path) if spd_path else None
        if not spd_path:
            result["status"] = "PENDING_AMD_SPD_PROBE_PATH_OR_RESULT"
            result["reason"] = "AMD requires SPD idx probe report"
            return result

        idxs = _parse_spd_idx_candidates(spd_path)
        result["spd_idx_candidates"] = idxs
        if not idxs:
            result["status"] = "PENDING_AMD_SPD_PROBE_PATH_OR_RESULT"
            result["reason"] = "SPD idx report has no detected DIMM idx"
            return result

        primary_idx = int(idxs[0])
        amd_secondary_idx4 = ec_related and 0 <= primary_idx <= 3
        result["rows"] = _channel_rows(primary_idx, amd_secondary_idx4=amd_secondary_idx4, fixed_channel2=fixed_channel2)
        result["row_count"] = len(result["rows"])
        result["status"] = "FOUND"
        if amd_secondary_idx4 and isinstance(fixed_channel2, dict):
            result["reason"] = (
                "AMD SMBus Channel1 from SPD idx; Channel2 filled by SMBus table fixed row (EC/SOC spec hint)"
            )
        elif amd_secondary_idx4:
            result["reason"] = (
                "AMD SMBus Channel1 from SPD idx; Channel2 fixed to idx=4 because spec SMBus is EC-related"
            )
        elif isinstance(fixed_channel2, dict):
            result["reason"] = "AMD SMBus Channel1 derived from SPD idx; Channel2 filled by SMBus table fixed row"
        else:
            result["reason"] = "AMD SMBus Channel1 derived from SPD idx probe"
        return result

    if cpu.get("is_intel"):
        intel_oem_idxs = _intel_oem_idxs_from_full_probe(probe_path) if ec_related else []
        result["intel_oem_idxs"] = intel_oem_idxs
        result["rows"] = _channel_rows(0, intel_oem_idxs=intel_oem_idxs, fixed_channel2=fixed_channel2)
        result["row_count"] = len(result["rows"])
        result["status"] = "FOUND"
        if isinstance(fixed_channel2, dict):
            result["reason"] = (
                "Intel SMBus uses fixed Channel1 idx=0 + Channel2 filled by SMBus table fixed row (EC/SOC spec hint)"
            )
        elif intel_oem_idxs:
            result["reason"] = (
                "Intel SMBus uses fixed Channel1 idx=0 + Channel2+ from full probe SMBUS_OEMn EXISTS"
            )
        elif ec_related:
            result["reason"] = "Intel SMBus uses fixed Channel1 idx=0"
        else:
            result["reason"] = "Intel SMBus uses fixed Channel1 idx=0 (no EC-related SMBus hint in spec)"
        return result

    result["status"] = "PENDING_SMBUS_PLATFORM_UNRESOLVED"
    result["reason"] = "Cannot classify CPU platform as Intel or AMD from full probe"
    return result


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
        # Keep the label exactly as the BIOS shows it (e.g. "CPU FAN Speed"):
        # the fan word plus up to two following words, stopping at values/delimiters.
        label_tail = r"\d*(?:[ \t]+(?!RPM\b)[A-Za-z]+){0,2}"
        cpu = re.search(r"\bCPU\s*FAN" + label_tail, text, re.IGNORECASE)
        if cpu:
            out["by_key"]["FCPU"] = cpu.group(0).strip()
            out["by_idx"][0] = cpu.group(0).strip()
        sys_fan = re.search(r"\b(?:SYSTEM|SYS)\s*FAN" + label_tail, text, re.IGNORECASE)
        if sys_fan:
            out["by_key"]["FSYS"] = sys_fan.group(0).strip()
            out["by_idx"][1] = sys_fan.group(0).strip()

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
            vv = _coerce_display_hint(v)
            if kk and vv:
                out.setdefault("by_key", {})[kk] = vv

    by_idx = data.get("by_idx")
    if isinstance(by_idx, dict):
        for k, v in by_idx.items():
            try:
                ii = int(str(k).strip(), 0)
            except Exception:
                continue
            vv = _coerce_display_hint(v)
            if vv:
                out.setdefault("by_idx", {})[ii] = vv

    return out


def _write_project_fan_name_hints(project_dir: Path, project_name: str, bios_cache_path: Path) -> Path:
    hints = _load_fan_name_hints_from_bios_cache(bios_cache_path)
    by_key = hints.get("by_key") if isinstance(hints, dict) else {}
    by_idx = hints.get("by_idx") if isinstance(hints, dict) else {}

    payload: dict = {
        "project": project_name,
        "source_glob": "bios*.png|jpg|jpeg",
        "status": "NO_BIOS_IMAGES",
        "images_analyzed": [],
        "by_key": by_key if isinstance(by_key, dict) else {},
        "by_idx": by_idx if isinstance(by_idx, dict) else {},
    }

    if bios_cache_path.exists():
        try:
            cache = _load_json(bios_cache_path)
            items_raw = cache.get("items") if isinstance(cache, dict) else []
            items = items_raw if isinstance(items_raw, list) else []
            bios_imgs = [str(it.get("filename") or "") for it in items if isinstance(it, dict) and str(it.get("filename") or "").lower().startswith("bios")]
            payload["images_analyzed"] = bios_imgs
            if bios_imgs:
                payload["status"] = "DONE" if payload["by_key"] else "NO_CONFIDENT_LABELS"
        except Exception:
            pass

    path = project_dir / f"{project_name}-fan-name-hints.json"

    # Preserve explicit/manual hints if they already exist.
    if path.exists():
        try:
            prev = _load_json(path)
            prev_by_key = prev.get("by_key") if isinstance(prev, dict) else None
            if isinstance(prev_by_key, dict):
                for k, v in prev_by_key.items():
                    kk = str(k or "").strip().upper()
                    vv = _coerce_display_hint(v)
                    if kk and vv:
                        payload.setdefault("by_key", {})[kk] = vv

            prev_by_idx = prev.get("by_idx") if isinstance(prev, dict) else None
            if isinstance(prev_by_idx, dict):
                for k, v in prev_by_idx.items():
                    try:
                        ii = int(str(k).strip(), 0)
                    except Exception:
                        continue
                    vv = _coerce_display_hint(v)
                    if vv:
                        payload.setdefault("by_idx", {})[ii] = vv

            if payload.get("by_key"):
                payload["status"] = "DONE"
        except Exception:
            pass

    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


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


def _fan_key_sort_value(key: str) -> tuple[int, int, str]:
    k = (key or "").upper().strip()
    if k == "FCPU":
        return (0, 0, k)
    if k == "FCPU2":
        return (0, 1, k)
    if k == "FSYS":
        return (1, 0, k)
    m = re.match(r"^FOEM(\d+)$", k)
    if m:
        return (2, int(m.group(1)), k)
    return (3, 0, k)


def _fan_keys_from_hints(fan_name_hints: dict | None) -> list[str]:
    if not isinstance(fan_name_hints, dict):
        return []

    keys: list[str] = []
    by_key = fan_name_hints.get("by_key")
    if isinstance(by_key, dict):
        for k, v in by_key.items():
            kk = str(k or "").strip().upper()
            vv = _coerce_display_hint(v)
            if not kk or not vv:
                continue
            if re.match(r"^(FCPU2?|FSYS|FOEM\d+)$", kk) and kk not in keys:
                keys.append(kk)

    # If BIOS hints only provide indexed names, derive minimal keys by index.
    by_idx = fan_name_hints.get("by_idx")
    if isinstance(by_idx, dict):
        for raw_idx, raw_name in by_idx.items():
            try:
                idx = int(str(raw_idx).strip(), 0)
            except Exception:
                continue
            nm = str(raw_name or "").strip()
            if not nm:
                continue
            if idx == 0:
                kk = "FCPU"
            elif idx == 1:
                kk = "FSYS"
            else:
                kk = f"FOEM{idx-2}"
            if kk not in keys:
                keys.append(kk)

    return sorted(keys, key=_fan_key_sort_value)


def _is_nct61xxd_fan_chip(chip_name: str) -> bool:
    c = _norm_chip_name(chip_name)
    return c.startswith(("NCT6106D", "NCT6116D", "NCT6126D", "NCT6776D"))


def _fan_db_row_key(row: dict) -> str:
    rn = str(row.get("report_name") or "").upper().strip()
    m = re.search(r"HWM_FAN_([A-Z0-9]+)$", rn)
    if not m:
        return ""
    suffix = m.group(1)
    fixed = {"CPU": "FCPU", "SYSTEM": "FSYS", "CPU2": "FCPU2"}
    if suffix in fixed:
        return fixed[suffix]
    m_oem = re.match(r"^OEM(\d+)$", suffix)
    if m_oem:
        return f"FOEM{int(m_oem.group(1))}"
    return ""


def _build_hwm_fan_rows_from_db_keys(db_path: Path, product_name: str, chip_name: str,
                                     fan_keys: list[str], fan_name_hints: dict | None = None) -> list[dict]:
    """Build HWM.Fan rows by reverse lookup from DB rows for requested fan keys.

    Used by non-EC NCT61xxD-like chips when probe fan channels are unavailable
    (common all-ERR probe case) and fan keys come from BIOS hints.
    """
    if not fan_keys:
        return []

    q = query_section(
        db_path=db_path,
        product_name=product_name,
        chip_name=chip_name,
        section="HWM.Fan",
    )
    rows = q.get("rows") or []
    if q.get("status") != "FOUND" or not rows:
        return []

    by_key: dict[str, dict] = {}
    for r in rows:
        if not isinstance(r, dict):
            continue
        k = _fan_db_row_key(r)
        if k and k not in by_key:
            by_key[k] = r

    out: list[dict] = []
    for key in fan_keys:
        k = str(key or "").strip().upper()
        r = by_key.get(k)
        if not isinstance(r, dict):
            continue
        ch = str(r.get("channel_id") or r.get("channel") or "").strip()
        io = str(r.get("io_port") or "").strip()
        opt = str(r.get("options") or r.get("option") or "").strip()
        pul = str(r.get("pulses") or r.get("offset") or "0").strip() or "0"
        if not ch:
            continue
        out.append({
            "item_name": k,
            "channel": ch,
            "io_port": io,
            "option": opt,
            "offset": pul,
            "disp_name": _resolve_fan_disp_name(k, None, fan_name_hints),
        })
    return out


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
    # GPIO trace source must include both circuit rasters and circuit PDF pages when available.
    rasters: list[Path] = []
    for p in sorted(project_dir.iterdir()):
        if not p.is_file():
            continue
        name = p.name.lower()
        if not name.startswith("circuit"):
            continue
        if p.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            continue
        rasters.append(p)

    pdfs = sorted(project_dir.glob("circuit*.pdf"))

    rendered: list[Path] = []
    if pdfs:
        out_dir = project_dir / "_ai_gpio_pages"
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            import fitz  # PyMuPDF
        except Exception:
            fitz = None

        if fitz is not None:
            for pdf in pdfs:
                try:
                    doc = fitz.open(str(pdf))
                except Exception:
                    continue
                try:
                    # 限制頁數避免自動流程過重；GPIO 常在前幾頁或獨立頁。
                    max_pages = min(len(doc), 4)
                    for page_index in range(max_pages):
                        page = doc.load_page(page_index)
                        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
                        out = out_dir / f"{project_name}-{pdf.stem}-p{page_index + 1}.png"
                        pix.save(str(out))
                        rendered.append(out)
                finally:
                    doc.close()

    # Keep deterministic order and de-duplicate absolute paths.
    merged: list[Path] = []
    seen: set[str] = set()
    for p in rasters + rendered:
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        merged.append(p)
    return merged


def _parse_gpio_function_label(function_label: str) -> tuple[int, int] | None:
    """Decode a chip GPIO function label into (group, bit).

    GPxy -> (x, y); GPIOxy -> (x, y) where the group digit may be hex
    (GPIO34 -> 3,4; GPIOB0 -> 11,0; GPIOA5 -> 10,5).
    """
    label = str(function_label or "").upper()
    m_gp = re.search(r"\bGP\s*([0-9])\s*([0-9])\b", label)
    if m_gp:
        return int(m_gp.group(1)), int(m_gp.group(2))
    m_gpio = re.search(r"\bGPIO\s*([0-9A-F])\s*([0-9])\b", label)
    if m_gpio:
        return int(m_gpio.group(1), 16), int(m_gpio.group(2))
    return None


def _gpio_target_signal_pattern(chip_name: str | None) -> tuple[str, re.Pattern[str] | None]:
    """Chip-aware external signal scope for GPIO circuit tracing (R-016)."""
    chip_u = str(chip_name or "").upper()
    if chip_u.startswith(("NCT6126D", "NCT6116D", "NCT6106D", "NCT6776D")):
        return "SIO_GPIO*", re.compile(r"^SIO_GPIO\d+$", re.IGNORECASE)
    if chip_u.startswith(("NCT6694B", "EIO-300")):
        # All EC ports (P1, P2, P3, ...); never P1-only.
        return "EC_P*_GPIO*", re.compile(r"^EC_P\d+_GPIO\d+$", re.IGNORECASE)
    if chip_u.startswith("EIO-211"):
        return "EC_GP*", re.compile(r"^EC_GP\d+$", re.IGNORECASE)
    return "(依圖面證據決定，需先收斂唯一 target_signal_set)", None


def _auto_generate_gpio_trace(project_dir: Path, project_name: str, chip_name: str | None = None,
                              prompts_dir: Path | None = None) -> Path | None:
    if not _chip_requires_circuit_evidence(chip_name):
        return None

    rules = _load_prompt_rules(GPIO_TRACE_PROMPT, prompts_dir)
    if rules["issue"]:
        return None

    out_path = project_dir / f"{project_name}-gpio-trace.json"
    images = _ensure_gpio_vision_images(project_dir, project_name)
    if not images:
        return None

    chip_u = str(chip_name or "").upper()
    target_signal_hint, target_signal_re = _gpio_target_signal_pattern(chip_name)

    merged_items: list[dict] = []
    ambiguous = 0

    for img in images:
        prompt = _build_gpio_trace_prompt(rules["text"], chip_u, target_signal_hint, img)
        out = _run_vision_analyze(img, prompt)
        if not out:
            continue

        obj = _extract_first_json_block(out)
        if not isinstance(obj, dict):
            continue

        items = obj.get("items")
        if not isinstance(items, list):
            continue

        for it in items:
            if not isinstance(it, dict):
                continue
            signal = str(it.get("signal") or "").strip()
            if target_signal_re is not None and signal and not target_signal_re.match(signal):
                # Out-of-scope signal for this chip naming hint.
                continue

            status = str(it.get("status") or "AMBIGUOUS").strip().upper()
            if status != "CONFIRMED":
                ambiguous += 1

            function_label = str(it.get("function_label") or "").strip()
            group = _parse_int_value(it.get("group"))
            bit = _parse_int_value(it.get("bit"))

            # Deterministic override: group/bit come from the chip function
            # label (GPxy / GPIOxy, hex group allowed), not from the AI value.
            parsed = _parse_gpio_function_label(function_label)
            if parsed:
                group, bit = parsed
                is_gp_label = re.search(r"\bGP\s*[0-9]\s*[0-9]\b", function_label.upper())
                if is_gp_label and not str(it.get("report_name") or "").strip():
                    it["report_name"] = f"GPIO{group}{bit}"

            item = {
                "report_name": str(it.get("report_name") or "").strip().upper(),
                "signal": signal,
                "function_label": function_label,
                "group": group,
                "bit": bit,
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
            "source": "agent_vision_cli",
            "images_analyzed": [str(p.name) for p in images],
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


def _should_regen_gpio_trace(project_dir: Path, chip_name: str, gpio_trace_raw: dict | None) -> bool:
    if not _chip_requires_circuit_evidence(chip_name):
        return False

    if not isinstance(gpio_trace_raw, dict):
        return True

    src = gpio_trace_raw.get("_source_path")
    if not src:
        return True
    trace_path = Path(str(src))
    if not trace_path.exists():
        return True

    # Build current scoped source set every run (circuit rasters + circuit pdf).
    # This is the "auto rescan" gate: even if mtime is unchanged/preserved,
    # newly-added screenshot files must trigger trace rebuild.
    current_sources: list[Path] = []
    try:
        for p in project_dir.iterdir():
            if not p.is_file():
                continue
            n = p.name.lower()
            if not n.startswith("circuit"):
                continue
            if p.suffix.lower() not in {".png", ".jpg", ".jpeg", ".pdf"}:
                continue
            current_sources.append(p)
    except Exception:
        return True

    # If any circuit* source is newer than trace artifact, rebuild.
    try:
        trace_mtime = trace_path.stat().st_mtime
        for p in current_sources:
            if p.stat().st_mtime > trace_mtime:
                return True
    except Exception:
        return True

    # Auto-rescan set gate: compare source set against previous analyzed image list.
    # Some copy/sync flows preserve mtime, so mtime-only checks can miss new files.
    analyzed_names: set[str] = set()
    meta = gpio_trace_raw.get("meta")
    if isinstance(meta, dict):
        imgs = meta.get("images_analyzed")
        if isinstance(imgs, list):
            for raw in imgs:
                s = str(raw or "").strip()
                if not s:
                    continue
                analyzed_names.add(Path(s).name.lower())
    if not analyzed_names:
        # Missing source manifest means we cannot prove freshness.
        return True

    for p in current_sources:
        if p.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
            continue
        if p.name.lower() not in analyzed_names:
            return True

    # Chip-aware scope sanity:
    # - SIO chips require SIO_GPIOn evidence.
    # - NCT6694B route requires EC_P*_GPIOn evidence across all visible EC ports.
    chip_u = (chip_name or "").upper()
    items = gpio_trace_raw.get("items")
    if chip_u.startswith(("NCT6126D", "NCT6116D", "NCT6106D", "NCT6776D")):
        if not isinstance(items, list) or not items:
            return True
        has_sio = any(
            isinstance(it, dict)
            and re.match(r"^SIO_GPIO\d+$", str(it.get("signal") or ""), re.IGNORECASE)
            for it in items
        )
        if not has_sio:
            return True

    if chip_u.startswith("NCT6694B"):
        if not isinstance(items, list) or not items:
            return True
        has_ec_port_gpio = any(
            isinstance(it, dict)
            and _is_nct6694b_gpio_signal(it.get("signal"))
            for it in items
        )
        if not has_ec_port_gpio:
            return True

    return False


def _is_nct6694b_gpio_signal(signal: object) -> bool:
    """Return true for the NCT6694B external GPIO target pattern EC_P*_GPIO*."""
    return re.fullmatch(r"EC_P\d+_GPIO\d+", str(signal or "").strip(), re.IGNORECASE) is not None


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


def _gpio_signal_order(signal: str) -> tuple:
    """Sort key for traced GPIO signals: EC_P<port>_GPIO<n> by (port, n), SIO_GPIO<n> by n."""
    sig = str(signal or "").strip().upper()
    m_ec = re.match(r"^EC_P(\d+)_GPIO(\d+)$", sig)
    if m_ec:
        return (0, int(m_ec.group(1)), int(m_ec.group(2)), sig)
    m_sio = re.match(r"^SIO_GPIO(\d+)$", sig)
    if m_sio:
        return (0, 0, int(m_sio.group(1)), sig)
    m_num = re.search(r"(\d+)$", sig)
    return (1, 0, int(m_num.group(1)) if m_num else 10**9, sig)


def _trace_item_quality(it: dict) -> int:
    score = 0
    fl = str(it.get("function_label") or "").strip().upper()
    if fl and fl not in {"GPIO", "N/A", "UNKNOWN"}:
        score += 3
    if re.search(r"\bGP\s*[0-9]\s*[0-9]\b", fl):
        score += 2
    if isinstance(_parse_int_value(it.get("group")), int) and isinstance(_parse_int_value(it.get("bit")), int):
        score += 1
    return score


def _load_gpio_defaults(db_path: Path) -> dict:
    """Fallback values for circuit-trace GPIO rows.

    GPIO database rows are now hardware-specific and are loaded through
    query_section().  This helper is only retained for the trace branch,
    where the circuit evidence supplies group/bit and these stable protocol
    defaults are needed for rendering.
    """
    out = {
        "base_addr": "0",
        "io_port": "0",
        "options": "0xA0000003",
        "disp_name": "",
    }
    return out


def _is_non_ec_sio_gpio_chip(chip_norm: str) -> bool:
    if chip_norm.startswith("NCT6694B"):
        return True
    if chip_norm.startswith("NCT61") and chip_norm.endswith("D"):
        return True
    return False


def _resolve_gpio_expected_count(spec: dict | None, probe_spec: dict | None = None) -> int | None:
    # 1) Probe-first: when probe exposes concrete GPIO keys/count, trust it.
    if isinstance(probe_spec, dict):
        gp = probe_spec.get("gpio")
        if isinstance(gp, dict):
            keys = gp.get("keys")
            if isinstance(keys, list):
                norm_keys = [str(k).strip().upper() for k in keys if str(k).strip()]
                norm_keys = [k for k in norm_keys if re.match(r"^GPIO\d+$", k)]
                if norm_keys:
                    return len(norm_keys)
            c_probe = _parse_int_value(gp.get("count"))
            if isinstance(c_probe, int) and c_probe > 0:
                return c_probe

    # 2) Spec fallback
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


def _build_gpio_query_result(db_path: Path, product_name: str, chip_name: str,
                             spec: dict | None, gpio_trace: dict | None = None,
                             is_ec: bool | None = None,
                             probe_spec: dict | None = None) -> tuple[dict, dict]:
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
    chip_norm = _norm_chip_name(chip_name)
    trace_required = chip_norm.startswith("NCT6694B")

    decision = {
        "route": "GPIO_TRACE_REQUIRED_NCT6694B" if trace_required else ("GPIO_GROUPPINS_EC" if is_ec is True else "GPIO_GROUPPINS"),
        "status": "FOUND",
        "pending": False,
        "reason": None,
        "gpio_expected_count": None,
        "gpio_group_pins_total": 0,
        "gpio_trimmed_count": 0,
        "gpio_trace_source": None,
        "gpio_trace_status": None,
        "gpio_trace_confirmed_count": 0,
        "gpio_trace_item_count": 0,
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
    if is_ec is False and _is_non_ec_sio_gpio_chip(chip_norm):
        defaults["base_addr"] = "0"
        defaults["io_port"] = "0x2E"
        defaults["options"] = "0xA0000003"

    # 1) Prefer circuit-trace contract when available for NON-EC/SIO route,
    #    and require trace-first for NCT6694B-routed GPIO in compound EIO-300 cases.
    trace_items = gpio_trace.get("items") if isinstance(gpio_trace, dict) else None
    if isinstance(gpio_trace, dict):
        decision["gpio_trace_source"] = gpio_trace.get("source_path")
        decision["gpio_trace_status"] = gpio_trace.get("status")
    if (is_ec is not True or trace_required) and isinstance(trace_items, list):
        decision["gpio_trace_item_count"] = len(trace_items)
        confirmed = [
            it for it in trace_items
            if isinstance(it, dict)
            and _parse_int_value(it.get("group")) is not None
            and _parse_int_value(it.get("bit")) is not None
            and str(it.get("status") or "").upper() not in {"OUT_OF_SCOPE", "INVALID"}
        ]

        # NCT61xxD/SIO strict scope: only accept SIO_GPIOn traced signals.
        # Do not ingest generic/non-target GPIO labels for this route.
        if is_ec is False and chip_norm.startswith(("NCT6106D", "NCT6116D", "NCT6126D", "NCT6776D")):
            confirmed = [
                it for it in confirmed
                if re.match(r"^SIO_GPIO\d+$", str(it.get("signal") or "").strip(), re.IGNORECASE)
            ]

            # SIO_GPIOn strict-scope quality guard (no fixed count):
            # if we have function-label evidence (GPxy), prefer those rows and drop
            # self-labeled placeholders like function_label=SIO_GPIOxx that can cause
            # duplicated/phantom channels from mixed extraction passes.
            has_gp_function = any(
                re.search(r"\bGP\s*[0-9]\s*[0-9]\b", str(it.get("function_label") or "").strip(), re.IGNORECASE)
                for it in confirmed
            )
            if has_gp_function:
                confirmed = [
                    it for it in confirmed
                    if re.search(r"\bGP\s*[0-9]\s*[0-9]\b", str(it.get("function_label") or "").strip(), re.IGNORECASE)
                ]

        # NCT6694B strict scope: accept all traced EC_P*_GPIOn signals.
        if chip_norm.startswith("NCT6694B"):
            confirmed = [
                it for it in confirmed
                if _is_nct6694b_gpio_signal(it.get("signal"))
            ]

        decision["gpio_trace_confirmed_count"] = len(confirmed)

        if confirmed:
            # INI keys are always GPIO00, GPIO01, ... assigned by the code in
            # traced signal order (EC_P1_GPIO0..7, EC_P2_GPIO0..7, ...; SIO_GPIOn by n).
            # The AI's report_name / chip function label never decides the key;
            # group/bit come from the traced function label.
            best_by_signal: dict[str, dict] = {}
            for idx, it in enumerate(confirmed):
                signal_name = str(it.get("signal") or "").strip().upper()
                if not signal_name:
                    signal_name = f"__UNNAMED_{idx}"
                prev = best_by_signal.get(signal_name)
                if prev is None or _trace_item_quality(it) > _trace_item_quality(prev):
                    best_by_signal[signal_name] = it

            out_rows: list[dict] = []
            ordered = sorted(best_by_signal.items(), key=lambda kv: _gpio_signal_order(kv[0]))
            for idx, (_signal, it) in enumerate(ordered):
                group = _parse_int_value(it.get("group"))
                bit = _parse_int_value(it.get("bit"))
                disp_name = str(it.get("name") or "").strip()
                out_rows.append({
                    "item_name": f"GPIO{idx:02d}",
                    "channel": defaults["base_addr"],
                    "io_port": defaults["io_port"],
                    "option": defaults["options"],
                    "group": group,
                    "bit": bit,
                    "disp_name": disp_name,
                })

            result["rows"] = out_rows
            result["row_count"] = len(out_rows)
            result["status"] = "FOUND"
            result["source"] = "GPIO_TRACE_CONTRACT"
            decision["route"] = "GPIO_TRACE"
            decision["reason"] = "Built GPIO section from gpio-trace contract"
            return result, decision

        trace_status = str(gpio_trace.get("status") or "").upper() if isinstance(gpio_trace, dict) else ""
        if trace_status.startswith("GPIO_TRACE_"):
            result["status"] = "SECTION_EMPTY"
            result["row_count"] = 0
            decision["pending"] = True
            decision["route"] = "GPIO_TRACE"
            decision["status"] = trace_status
            decision["reason"] = "GPIO trace exists but has no confirmed group/bit evidence"
            return result, decision

    if trace_required:
        result["status"] = "SECTION_EMPTY"
        result["row_count"] = 0
        decision["pending"] = True
        decision["route"] = "GPIO_TRACE_REQUIRED_NCT6694B"
        decision["status"] = "GPIO_TRACE_REQUIRED_NCT6694B"
        decision["reason"] = "NCT6694B GPIO must be built from circuit trace evidence; DB fallback is disabled"
        return result, decision

    gpio_query = query_section(db_path, product_name, chip_name, "GPIO")
    if gpio_query.get("status") != "FOUND":
        result["status"] = gpio_query.get("status") or "SECTION_EMPTY"
        result["row_count"] = 0
        decision["pending"] = True
        decision["status"] = result["status"]
        decision["reason"] = "No hardware-specific GPIO rows in config_new.db"
        return result, decision
    gp_rows = gpio_query.get("rows") or []
    decision["gpio_group_pins_total"] = len(gp_rows)

    if not gp_rows:
        result["status"] = "SECTION_EMPTY"
        result["row_count"] = 0
        decision["pending"] = True
        decision["status"] = "GPIO_ROWS_EMPTY"
        decision["reason"] = "Hardware-specific GPIO query returned no rows"
        return result, decision

    expected = _resolve_gpio_expected_count(spec, probe_spec=probe_spec)
    decision["gpio_expected_count"] = expected

    use_rows = gp_rows

    probe_gpio_keys: list[str] = []
    if isinstance(probe_spec, dict):
        gp = probe_spec.get("gpio")
        if isinstance(gp, dict):
            keys = gp.get("keys")
            if isinstance(keys, list):
                for k in keys:
                    kk = str(k or "").strip().upper()
                    if re.match(r"^GPIO\d+$", kk) and kk not in probe_gpio_keys:
                        probe_gpio_keys.append(kk)

    if probe_gpio_keys:
        desired = set(probe_gpio_keys)
        filtered = []
        for r in gp_rows:
            rn = str(r.get("report_name") or r.get("item_name") or "").strip().upper()
            m = re.match(r"^GPIO(\d+)$", rn)
            key = f"GPIO{int(m.group(1)):02d}" if m else rn
            if key in desired:
                filtered.append(r)
        if filtered:
            use_rows = filtered
            decision["gpio_trimmed_count"] = len(gp_rows) - len(use_rows)

    if isinstance(expected, int) and expected > 0 and len(use_rows) > expected:
        use_rows = use_rows[:expected]
        decision["gpio_trimmed_count"] = len(gp_rows) - len(use_rows)

    out_rows: list[dict] = []
    for idx, r in enumerate(use_rows):
        key = r.get("report_name") or r.get("item_name") or f"GPIO{idx:02d}"
        row_base_addr = r.get("base_addr") or defaults["base_addr"]
        row_io_port = r.get("io_port") or defaults["io_port"]
        row_options = r.get("options") or r.get("option") or defaults["options"]
        out_rows.append({
            "item_name": key,
            "channel": row_base_addr,
            "io_port": row_io_port,
            "option": row_options,
            "group": r.get("group"),
            "bit": r.get("pin") or r.get("bit"),
            # channel_name is app-reference metadata; do not emit it as INI Name.
            "disp_name": r.get("disp_name") or defaults.get("disp_name", ""),
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
    decision["reason"] = "Built GPIO from hardware-specific GPIO rows (trimmed by probe gpio evidence / spec gpio.count)"
    return result, decision


def _build_hwm_fan_query_result(db_path: Path, product_name: str, chip_name: str,
                                probe_spec: dict | None, fan_pairing: dict | None,
                                fan_name_hints: dict | None = None,
                                is_ec: bool | None = None) -> dict:
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
    if not probe_keys and isinstance(by_key, dict) and is_ec is not True:
        probe_keys = sorted([str(k).strip().upper() for k in by_key.keys() if str(k).strip()], key=_fan_key_sort_value)

    # NON-EC/SIO fallback: when probe fan items are unsupported/missing,
    # derive fan keys from BIOS fan-name hints.
    from_bios_hints = False
    if not probe_keys and is_ec is False:
        probe_keys = _fan_keys_from_hints(fan_name_hints)
        from_bios_hints = bool(probe_keys)

    # EC route (EIO-300/NCT6694B compound included): config_new.db rows are the
    # base, the full probe keeps only [OK] fans, BIOS hints fill the Name.
    # Schematic fan pairing is never used here.
    if is_ec is True:
        rows = _build_hwm_fan_rows_from_db_keys(
            db_path=db_path,
            product_name=product_name,
            chip_name=chip_name,
            fan_keys=probe_keys,
            fan_name_hints=fan_name_hints,
        )
        result["rows"] = rows
        result["row_count"] = len(rows)
        result["source"] = "EC_DB_ROWS_FILTERED_BY_PROBE"
        result["status"] = "FOUND" if rows else "SECTION_EMPTY"
        if not rows:
            result["error"] = "NO_DB_HWM_FAN_ROWS_FOR_PROBE_OK_FANS"
        return result

    if not probe_keys:
        result["status"] = "SECTION_EMPTY"
        result["row_count"] = 0
        result["error"] = "NO_FAN_KEYS_FROM_PROBE_OR_PAIRING_OR_BIOS_HINTS"
        return result

    # NCT61xxD/SIO all-ERR probe route:
    # fan keys come from BIOS hints, then reverse-lookup DB HWM.Fan rows
    # to fill channel/io_port/options/pulses by mapped item key.
    if is_ec is False and _is_nct61xxd_fan_chip(chip_name) and from_bios_hints:
        db_rows = _build_hwm_fan_rows_from_db_keys(
            db_path=db_path,
            product_name=product_name,
            chip_name=chip_name,
            fan_keys=probe_keys,
            fan_name_hints=fan_name_hints,
        )
        if db_rows:
            result["rows"] = db_rows
            result["row_count"] = len(db_rows)
            result["status"] = "FOUND"
            result["source"] = "BIOS_FAN_KEYS_DB_REVERSE_LOOKUP"
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

    fan_tpl = _fan_template_by_chip(db_path, chip_name)

    # Keep probe key order in rows; channel id comes from resolved idx map
    for key in probe_keys:
        idx = idx_by_key[key]
        out_rows.append({
            "item_name": key,
            "channel": f"0x{0x80000000 + idx:08X}",
            "io_port": fan_tpl["io_port"],
            "option": fan_tpl["options"],
            "offset": fan_tpl["pulses"],
            "disp_name": _resolve_fan_disp_name(key, idx, fan_name_hints),
        })

    result["rows"] = out_rows
    result["row_count"] = len(out_rows)
    result["status"] = "FOUND" if out_rows else "SECTION_EMPTY"
    return result


def _build_hwm_fan_control_query_result(db_path: Path, fan_result: dict, fan_pairing: dict | None,
                                        fan_name_hints: dict | None = None,
                                        is_ec: bool | None = None) -> tuple[dict, dict]:
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

    if is_ec is True:
        # EC route: Fan.Control mirrors the DB-based HWM.Fan rows one-to-one;
        # schematic fan pairing applies to SIO (NCT61xxD) boards only.
        fan_pairing = None
        decision["route"] = "FAN_EC_DB_MIRROR"
    pairing_status = (fan_pairing.get("status") if isinstance(fan_pairing, dict) else "") or ""
    by_key = fan_pairing.get("by_key") if isinstance(fan_pairing, dict) else None
    decision["pairing_status"] = pairing_status if pairing_status else None
    decision["source_path"] = fan_pairing.get("source_path") if isinstance(fan_pairing, dict) else None

    # NON-EC/SIO fallback: if pairing artifact is missing, keep a deterministic
    # one-to-one control mapping from resolved HWM.Fan channels instead of
    # forcing an empty section.
    if is_ec is False:
        has_pairing_items = isinstance(by_key, dict) and bool(by_key)
        if not has_pairing_items:
            decision["pairing_status"] = "FAN_PAIRING_MISSING_FALLBACK_ONE_TO_ONE"
            decision["reason"] = "NON_EC fan pairing missing; fallback to one-to-one from HWM.Fan channels"

    if pairing_status in ("FAN_PAIRING_AMBIGUOUS", "FAN_PAIRING_NEEDS_FANCONTROL_EVIDENCE"):
        result["status"] = "SECTION_EMPTY"
        result["row_count"] = 0
        decision["pending"] = True
        decision["status"] = pairing_status
        decision["reason"] = "Fan pairing not confirmed"
        return result, decision

    fan_channel_by_key = _build_fan_channel_map(fan_rows)
    base_channel, template = _infer_fan_base_channel(fan_rows, [])
    chip_name = str(((fan_result.get("query_key") or {}).get("chip_name") or "")).strip()
    fan_tpl = _fan_template_by_chip(db_path, chip_name)
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
            "io_port": (str(fr.get("io_port")) if is_ec is True and str(fr.get("io_port") or "").strip()
                        else fan_tpl["io_port"]),
            "option": "0x20000000",
            "disp_name": (fr.get("disp_name") if isinstance(fr.get("disp_name"), str) and fr.get("disp_name").strip() else _resolve_fan_disp_name(key, control_idx if isinstance(control_idx, int) else pos, fan_name_hints)),
        })

    result["rows"] = out_rows
    result["row_count"] = len(out_rows)
    result["status"] = "FOUND" if out_rows else "SECTION_EMPTY"
    decision["overridden_count"] = overridden
    decision["reason"] = (
        "EC route: HWM.Fan.Control mirrors DB-based HWM.Fan rows one-to-one"
        if is_ec is True
        else "Built HWM.Fan.Control from resolved fan keys (probe/pairing/BIOS hints) + AI pairing"
    )
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
    # prompts/*.md files that could not be used: {prompt file: issue}.
    prompt_issues: dict[str, str] = {}
    bios_cache_path = _build_bios_image_cache(proj_dir, name, chip_name=chip_name)
    bios_cache_path = _vision_populate_bios_cache(bios_cache_path, prompt_issues=prompt_issues)
    _write_project_temperature_name_hints(proj_dir, name, bios_cache_path)
    _write_project_fan_name_hints(proj_dir, name, bios_cache_path)
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

    gpio_trace = None
    gpio_trace_chip_name = _resolve_gpio_trace_chip_name(chip_name)
    if _chip_requires_circuit_evidence(gpio_trace_chip_name):
        # An existing trace (for example written by the Hermes agent before
        # generation) is used as-is; the prompt file is needed only when the
        # code itself has to run the AI trace.
        gpio_trace_raw = _load_gpio_trace_result(proj_dir, name)
        if _should_regen_gpio_trace(proj_dir, gpio_trace_chip_name, gpio_trace_raw):
            gpio_rules = _load_prompt_rules(GPIO_TRACE_PROMPT)
            if gpio_rules["issue"]:
                prompt_issues[GPIO_TRACE_PROMPT] = gpio_rules["issue"]
                print(f"WARNING: {gpio_rules['issue']}; GPIO circuit trace skipped", file=sys.stderr)
                gpio_trace_raw = None
            else:
                _auto_generate_gpio_trace(proj_dir, name, chip_name=gpio_trace_chip_name)
                gpio_trace_raw = _load_gpio_trace_result(proj_dir, name)
        if GPIO_TRACE_PROMPT not in prompt_issues:
            gpio_trace = _normalize_gpio_trace_result(gpio_trace_raw)

    # Sections that depend on an unusable prompt file are skipped and reported.
    section_prompt_issues: dict[str, str] = {}
    if BIOS_READING_PROMPT in prompt_issues:
        for gated in BIOS_GATED_SECTIONS:
            section_prompt_issues[gated] = prompt_issues[BIOS_READING_PROMPT]
    if GPIO_TRACE_PROMPT in prompt_issues:
        section_prompt_issues["GPIO"] = prompt_issues[GPIO_TRACE_PROMPT]

    latest_fan_rows: list[dict] = []
    latest_fan_result: dict | None = None

    for sec in sections:
        route = "EC" if is_ec is True else ("NON_EC" if is_ec is False else "UNKNOWN_EC")
        fan_meta: dict = {}
        section_chip_name = _resolve_chip_name_for_section(chip_name, sec)

        prompt_issue = section_prompt_issues.get(sec)
        if prompt_issue:
            stale_section_path = proj_dir / f"{name}_{sec}.ini"
            if stale_section_path.exists():
                stale_section_path.unlink()
            matrix.append({
                "section": sec,
                "status": "SKIPPED_PROMPT_UNAVAILABLE",
                "query_status": "NOT_QUERIED",
                "row_count": 0,
                "path": None,
                "query_key": None,
                "route": f"{route}+PROMPT_UNAVAILABLE",
                "reason_code": prompt_issue.split(":", 1)[0],
                "reason": f"AI analysis skipped because {prompt_issue}",
            })
            continue

        applicability = evaluate_section_applicability(sec, spec, bios_cache_path)
        if applicability.get("applicable") is False:
            stale_section_path = proj_dir / f"{name}_{sec}.ini"
            if stale_section_path.exists():
                stale_section_path.unlink()
            matrix.append({
                "section": sec,
                "status": "SKIPPED_NOT_APPLICABLE",
                "query_status": "NOT_QUERIED",
                "row_count": 0,
                "path": None,
                "query_key": None,
                "route": f"{route}+BIOS_HWM_APPLICABILITY",
                "reason_code": applicability.get("reason_code"),
                "reason": "HWM section absent from completed BIOS Hardware Monitor evidence",
            })
            continue

        if sec == "SMBus":
            result = _build_smbus_query_result(
                db_path=db_path,
                product_name=product_name,
                chip_name=section_chip_name,
                probe_path=probe_path,
                project_dir=proj_dir,
                project_name=name,
                spec=spec,
            )
            route = f"{route}+SMBUS_POLICY"
            fan_meta = {
                "smbus_reason": result.get("reason"),
                "smbus_spd_idx_probe_report": result.get("spd_idx_probe_report"),
                "smbus_spd_idx_candidates": result.get("spd_idx_candidates"),
                "smbus_cpu": result.get("cpu"),
            }
        elif sec == "I2C":
            result = _build_i2c_query_result(
                db_path=db_path,
                product_name=product_name,
                chip_name=section_chip_name,
                probe_spec=probe_spec,
                spec=spec,
            )
            route = f"{route}+I2C_POLICY"
        elif sec == "HWM.Voltage" and is_ec is True:
            result = _build_ec_voltage_query_result(
                db_path=db_path,
                product_name=product_name,
                chip_name=section_chip_name,
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
            if is_ec is not True and _is_superio_temperature_seed_chip(section_chip_name):
                # non-EC SuperIO v1 seed: keep all queried channel rows first
                result = query_section(
                    db_path=db_path,
                    product_name=product_name,
                    chip_name=section_chip_name,
                    section=sec,
                )
                result["source"] = "SUPERIO_SEED_CHANNEL_SCAN"
            else:
                result = _build_hwm_temperature_query_result(
                    db_path=db_path,
                    product_name=product_name,
                    chip_name=section_chip_name,
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
                chip_name=section_chip_name,
                probe_spec=probe_spec,
                fan_pairing=fan_pairing,
                fan_name_hints=fan_name_hints,
                is_ec=is_ec,
            )
            latest_fan_result = result
        elif sec == "HWM.Fan.Control":
            fan_seed = latest_fan_result or _build_hwm_fan_query_result(
                db_path=db_path,
                product_name=product_name,
                chip_name=section_chip_name,
                probe_spec=probe_spec,
                fan_pairing=fan_pairing,
                fan_name_hints=fan_name_hints,
                is_ec=is_ec,
            )
            latest_fan_rows = fan_seed.get("rows") or []
            result, fan_decision = _build_hwm_fan_control_query_result(
                db_path=db_path,
                fan_result=fan_seed,
                fan_pairing=fan_pairing,
                fan_name_hints=fan_name_hints,
                is_ec=is_ec,
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
                chip_name=section_chip_name,
                spec=spec,
                gpio_trace=gpio_trace,
                is_ec=is_ec,
                probe_spec=probe_spec,
            )
            route = f"{route}+{gpio_decision.get('route')}"
            fan_meta = {
                "gpio_expected_count": gpio_decision.get("gpio_expected_count"),
                "gpio_group_pins_total": gpio_decision.get("gpio_group_pins_total"),
                "gpio_trimmed_count": gpio_decision.get("gpio_trimmed_count", 0),
                "gpio_trace_source": gpio_decision.get("gpio_trace_source"),
                "gpio_trace_status": gpio_decision.get("gpio_trace_status"),
                "gpio_trace_confirmed_count": gpio_decision.get("gpio_trace_confirmed_count", 0),
                "gpio_trace_item_count": gpio_decision.get("gpio_trace_item_count", 0),
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
                chip_name=section_chip_name,
                section=sec,
            )

        if sec in {"VGA.Brightness", "VGA.Backlight"}:
            result, vga_filter = _filter_vga_rows_by_probe(result, sec, probe_spec)
            if vga_filter.get("filter_applied"):
                route = f"{route}+PROBE_CHANNEL_FILTER"
            fan_meta.update({
                "vga_probe_filter_applied": vga_filter.get("filter_applied", False),
                "vga_probe_supported_count": vga_filter.get("probe_supported_count"),
                "vga_db_row_count": vga_filter.get("db_row_count"),
                "vga_trimmed_count": vga_filter.get("trimmed_count", 0),
                "vga_probe_channel_ids": vga_filter.get("probe_channel_ids", []),
            })

        if sec == "HWM.Voltage" and is_ec is True and result.get("status") == "FOUND":
            result = _merge_voltage_alias_into_rows(result, voltage_alias_bridge_path)
            if result.get("alias_unresolved"):
                route = f"{route}+AMBIGUOUS_HWM_VOLTAGE_ALIAS"
                fan_meta["voltage_alias_unresolved"] = result["alias_unresolved"]
            result = _assign_oem_slots_for_unmapped_voltage_rows(result)
            if result.get("voltage_oem_slots"):
                route = f"{route}+VOLTAGE_OEM_SLOT"
                fan_meta["voltage_oem_slots"] = result["voltage_oem_slots"]
            if result.get("voltage_unplaced"):
                route = f"{route}+AMBIGUOUS_HWM_VOLTAGE_ALIAS"
                fan_meta["voltage_unplaced"] = result["voltage_unplaced"]

        status = result.get("status")

        if status == "FOUND" and sec in {"HWM.Current", "HWM.CaseOpen", "WDT"}:
            rows = result.get("rows") or []
            if isinstance(rows, list) and len(rows) > 1:
                result = dict(result)
                result["rows"] = rows[:1]
                result["row_count"] = 1

        if sec == "HWM.Fan" and status == "FOUND":
            latest_fan_rows = result.get("rows") or []
            latest_fan_result = result

        if status == "FOUND":
            # HWM.Voltage has EC vs non-EC routing differences.
            if sec == "HWM.Voltage" and is_ec is not True:
                if _is_superio_voltage_seed_chip(section_chip_name):
                    result = dict(result)
                    result["rows"] = _apply_superio_voltage_seed_rows(result.get("rows") or [])
                    result["row_count"] = len(result.get("rows") or [])

                    # v2 refine (when probe + BIOS cache are both available):
                    # keep BIOS-visible + probe-OK items, and backfill Name(alias).
                    result = _apply_superio_voltage_v2_refine(
                        result=result,
                        bios_cache_path=bios_cache_path,
                        probe_path=probe_path,
                    )
                    v2_meta = result.get("superio_v2") if isinstance(result, dict) else None
                    if isinstance(v2_meta, dict) and v2_meta.get("enabled"):
                        route = f"{route}+{v2_meta.get('route')}"
                        fan_meta.update({
                            "voltage_v2_enabled": True,
                            "voltage_v2_kept_count": v2_meta.get("kept_count"),
                            "voltage_v2_dropped_count": v2_meta.get("dropped_count"),
                        })

                eval_non_ec = _evaluate_non_ec_hwm_voltage(
                    product_name=product_name,
                    chip_name=section_chip_name,
                    rows=result.get("rows") or [],
                )
                # keep v2 suffix if route already carries it
                base_route = eval_non_ec["route"]
                if "+SUPERIO_V2_BIOS_PROBE_ALIAS" in route:
                    route = f"{base_route}+SUPERIO_V2_BIOS_PROBE_ALIAS"
                else:
                    route = base_route

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
                        **fan_meta,
                    })
                    continue

            # HWM.Temperature non-EC SuperIO v1/v2 flow:
            # v1 seed all rows -> v2 keep BIOS-visible + probe-OK + alias backfill.
            if sec == "HWM.Temperature" and is_ec is not True and _is_superio_temperature_seed_chip(section_chip_name):
                result = dict(result)
                result["rows"] = _apply_superio_temperature_seed_rows(result.get("rows") or [])
                result["row_count"] = len(result.get("rows") or [])

                result = _apply_superio_temperature_v2_refine(
                    result=result,
                    bios_cache_path=bios_cache_path,
                    probe_path=probe_path,
                )
                t_v2_meta = result.get("superio_temp_v2") if isinstance(result, dict) else None
                if isinstance(t_v2_meta, dict) and t_v2_meta.get("enabled"):
                    route = f"SUPERIO_TEMP_SEED_CHANNEL_SCAN+{t_v2_meta.get('route')}"
                    fan_meta.update({
                        "temperature_v2_enabled": True,
                        "temperature_v2_kept_count": t_v2_meta.get("kept_count"),
                        "temperature_v2_dropped_count": t_v2_meta.get("dropped_count"),
                    })
                else:
                    route = "SUPERIO_TEMP_SEED_CHANNEL_SCAN"

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

    # A validated project route override is applied only after DB/probe/BIOS
    # generation has completed. Option fallback remains disabled. Applying the
    # same override to full and split INIs keeps the section-config builder's
    # downstream JSON projection consistent with the runtime INI.
    override_path = proj_dir / f"{name}-config-overrides.json"
    generated_section_paths = {
        str(entry["section"]): Path(str(entry["path"]))
        for entry in matrix
        if str(entry.get("status", "")).upper() == "GENERATED"
        and entry.get("path")
    }
    project_override = apply_project_route_overrides(
        project=name,
        override_path=override_path,
        full_ini_path=out_ini_path,
        section_paths=generated_section_paths,
    )
    applied_override_sections = project_override.get("sections")
    if not isinstance(applied_override_sections, list):
        applied_override_sections = []
    override_details = project_override.get("details")
    if not isinstance(override_details, dict):
        override_details = {}
    for section in applied_override_sections:
        entry = next((item for item in matrix if item.get("section") == section), None)
        section_details = override_details.get(section)
        if not isinstance(section_details, dict):
            section_details = {}
        if isinstance(entry, dict):
            entry["project_route_override"] = {
                "status": "APPLIED",
                "path": str(override_path),
                "sha256": project_override.get("sha256"),
                **section_details,
            }
            base_route = str(entry.get("route") or "")
            if "PROJECT_ROUTE_OVERRIDE" not in base_route:
                entry["route"] = (
                    f"{base_route}+PROJECT_ROUTE_OVERRIDE"
                    if base_route
                    else "PROJECT_ROUTE_OVERRIDE"
                )

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
                "prompt_issues": prompt_issues,
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
            "  'extract'/'understand' call the current agent CLI (agent_llm.py; default Hermes)"
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
            "understand — form.json → spec.json (agent LLM)\n"
            "generate  — config_new.db query → pre-INI + section INIs\n"
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
    parser.add_argument("--db", default="/home/company2/AIagent_susi/config_new.db", help="Path to config_new.db")
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
    request_form_missing = False

    # ── Stage: extract ──────────────────────────────────────────────────────
    if args.stage in ("extract", "all"):
        pdf_path, json_path = resolve_extract_paths(args.project, args.input_pdf, args.out_json, root)
        if not pdf_path.exists():
            request_form_missing = True
            print(
                f"Request form not found: {pdf_path} — will not auto-select other PDFs as spec.json source"
            )

        if json_path.exists():
            print(f"Skip extract: {json_path} already exists")
        else:
            if request_form_missing:
                print(
                    "Skip extract: request form is missing. "
                    "Continue with CLI-provided rules (e.g. --sections/--chip-name) and BIOS evidence."
                )
            else:
                json_path = extract_pdf_to_json(pdf_path, json_path)
                print(f"Extract done: {json_path}")

    # ── Stage: understand ────────────────────────────────────────────────────
    run_understand = (
        args.stage == "understand"
        or (args.stage == "all" and not args.skip_understand)
    )
    if run_understand and request_form_missing and not args.in_json:
        print("Skip understand: request form is missing and no --in-json was provided")
        run_understand = False
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

    # ── Stage: generate (config_new.db only) ───────────────────────────────
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
