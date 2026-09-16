# -*- coding: utf-8 -*-
"""
SUSI project.json -> project-pre.ini generator

Pipeline: {project}.pdf -> extract_pdf.py -> {project}.json -> (this) -> {project}-pre.ini
Principle: template-first, key-complete, no fabrication.
  - Every key in the fixed key list must appear in the output.
  - Value = empty when it cannot be derived from form data + rules + chip_db.
  - Never copy from a golden reference.
"""

import argparse
import json
import re
import difflib
from pathlib import Path

# from chip_db import ChipDB
# Disabled by policy: generation must not use chip_db/chip_db.db; use config_new.db query flow instead.


# ---------- option bit constants (from PageTemplate.cs / Page*.cs) ----------
OPT_BACKUP_S3S4 = 0x20000000
OPT_INIT_HW     = 0x80000000
OPT_BOTH        = OPT_INIT_HW | OPT_BACKUP_S3S4   # 0xA0000000
GPIO_IN         = 0x01
GPIO_OUT        = 0x02
WDT_PIN_EVENT   = 0x10
TEMP_MODE_BIT   = 0x00000001


def hx(v):
    return "0x%08X" % v


# Per-section split output: emit one file per functional section.
# File name pattern: {project}_{section}.ini (dot in section name is preserved).
# Each file contains ONLY [Information] + that section.
SPLIT_SECTIONS = [
    "SMBus", "I2C", "VGA.Backlight", "VGA.Brightness",
    "HWM.Voltage", "HWM.Current", "HWM.Temperature", "HWM.Fan",
    "HWM.Fan.Control", "HWM.CaseOpen", "WDT", "GPIO",
    "StorageArea", "ThermalProtect",
]


def _split_keeps_section(target_section: str, section_name: str) -> bool:
    """Does this per-section file keep this section?"""
    return section_name == "Information" or section_name == target_section


def _filter_lines_for_section(lines: list[str], target_section: str) -> list[str]:
    """Per-section slim output:
    - keep only [Information] + target section;
    - drop comment lines;
    - drop keys with empty values.
    """
    out: list[str] = []
    keeping = True
    current_section = None
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            current_section = s[1:-1]
            keeping = _split_keeps_section(target_section, current_section)
            if keeping:
                out.append(line)
            continue
        if not keeping:
            continue
        if s.startswith(";"):
            continue
        # Keep [Information] keys even when value is empty.
        if current_section != "Information" and "=" in s and s.split("=", 1)[1].strip() == "":
            continue
        if s == "" and (not out or out[-1].strip() == ""):
            continue
        out.append(line)
    return out


def _section_has_nonempty_value(lines: list[str], section_name: str) -> bool:
    """True when the target section contains at least one non-empty key=value."""
    current = None
    for line in lines:
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            current = s[1:-1]
            continue
        if current != section_name:
            continue
        if not s or s.startswith(";") or "=" not in s:
            continue
        if s.split("=", 1)[1].strip() != "":
            return True
    return False


# ---------- Platform topology: COMe I2C → PCH native I2C ----------
# (hwid, channel) only — option bits not derivable from rules, left blank in output.
# hwid = Intel PCH PCI Device ID (fixed per CPU family).
# channel = COMe R3.0 Type6 spec defines I2C_CK/DAT (B33/B34) as channel 1.
COME_I2C_TABLE = {
    "Intel-RPL-P": ("0x80860009", "0x80000001"),
}
COME_I2C_DEFAULT_KEY = "Intel-RPL-P"


# ---------- digit JSON helpers ----------

def _find_digit_json(root: Path, stem: str) -> Path | None:
    """Auto-discover digit_{project}_interface_device_pin.json beside input JSON."""
    for candidate in [
        root / f"digit_{stem}_interface_device_pin.json",
        root / f"digit_{stem.lower()}_interface_device_pin.json",
        root / f"digit_{stem.lower().replace('-', '')}_interface_device_pin.json",
    ]:
        if candidate.exists():
            return candidate
    return None


def parse_digit_json(path: Path) -> dict:
    """Return interface presence hints from circuit diagram JSON."""
    with open(path, encoding="utf-8") as f:
        items = json.load(f).get("items", [])
    hints: dict = {"i2c_come": False}
    for item in items:
        iface  = (item.get("interface") or "").upper()
        device = (item.get("device")    or "").lower()
        signal = (item.get("signal")    or "").upper()
        if iface == "I2C" and "come" in device and ("_CK" in signal or "_CLK" in signal):
            hints["i2c_come"] = True
    return hints


# ---------- path helpers ----------

def resolve_paths(project: str | None, in_json: str | None, out_ini: str | None, root: Path):
    """
    Flexible path resolution. --project accepts:
      - plain name       "SOM-9590"           → tries root/SOM-9590/SOM-9590.json, then root/SOM-9590.json
      - absolute dir     "/abs/path/SOM-9590" → SOM-9590/SOM-9590.json inside that dir
      - absolute .json   "/abs/path/foo.json" → used directly
    """
    if in_json:
        json_path = Path(in_json)
        if not json_path.is_absolute():
            json_path = root / in_json
        name = json_path.stem.replace("-pre", "")
        proj_dir = json_path.parent
    elif project:
        p = Path(project)
        if p.suffix == ".json":
            # explicit json file path
            json_path = p if p.is_absolute() else root / p
            name = json_path.stem.replace("-pre", "")
            proj_dir = json_path.parent
        elif p.is_absolute() or (root / p).is_dir():
            # directory path (absolute) or subdirectory name
            proj_dir = p if p.is_absolute() else root / p
            name = proj_dir.name
            json_path = proj_dir / f"{name}.json"
        else:
            # plain project name — try subfolder first, then flat
            name = project
            subdir = root / name / f"{name}.json"
            flat   = root / f"{name}.json"
            json_path = subdir if subdir.exists() else flat
            proj_dir = json_path.parent
    else:
        name = "result"
        json_path = root / "form.json"
        proj_dir = root

    if out_ini:
        ini_path = Path(out_ini)
        if not ini_path.is_absolute():
            ini_path = root / out_ini
    else:
        ini_path = proj_dir / f"{name}-pre.ini"

    return json_path, ini_path


def _first_port(ports: list[str]) -> str:
    """Return first hex port address (e.g. '0x2E') from DB port list.
    Returns '0' for EC/SMBus chips whose port list is ['0','1'] (channel IDs, not addresses)."""
    for p in ports:
        if p.strip().lower().startswith("0x"):
            return p.strip()
    return "0"


# ---------- form.json parsers ----------

def _cell(v) -> str:
    return (v or "").strip()


def _find_table(form_data: dict, keyword: str):
    """Return first table whose first row contains keyword in any cell."""
    kw = keyword.lower()
    for page in form_data.get("pages", []):
        for table in page.get("tables", []):
            if not table:
                continue
            for cell in (table[0] or []):
                if kw in _cell(cell).lower():
                    return table
    return None


def _section_enabled(form_data: dict, keyword: str) -> bool:
    """Return True if '■' and keyword appear together in any cell."""
    kw = keyword.lower()
    for page in form_data.get("pages", []):
        for table in page.get("tables", []):
            for row in table:
                for cell in (row or []):
                    c = _cell(cell)
                    if kw in c.lower() and "■" in c:
                        return True
        if re.search(rf'■[^\n]*{re.escape(keyword)}', page.get("text", ""), re.IGNORECASE):
            return True
    return False


def _section_present(form_data: dict, keyword: str) -> bool:
    """Return True if keyword appears anywhere (for 'default included' sections)."""
    kw = keyword.lower()
    for page in form_data.get("pages", []):
        for table in page.get("tables", []):
            for row in table:
                for cell in (row or []):
                    if kw in _cell(cell).lower():
                        return True
        if kw in page.get("text", "").lower():
            return True
    return False


def _section_checkbox_state(form_data: dict, keyword: str):
    """Checkbox state of a section header cell: True (■), False (□), None (no box).

    Matches cells like "■ Thermal Protect" / "□ Storage (EEPROM)": the keyword
    and the box glyph must be in the SAME cell. Any "■" wins (leftover "□"
    glyphs next to it are extraction noise, per the checkbox reading rule).
    """
    kw = keyword.lower()
    state = None
    for page in form_data.get("pages", []):
        for table in page.get("tables", []):
            for row in table:
                for cell in (row or []):
                    c = _cell(cell)
                    if kw not in c.lower():
                        continue
                    if "■" in c:
                        return True
                    if "□" in c:
                        state = False
    return state


def _section_default_included(form_data: dict, keyword: str) -> bool:
    """Return True when section text carries the EC-support wording.

    Hybrid gate layer 2 (user decision 2026-07-14): consulted only when the
    section has no checkbox. Both wordings count — the full "Only EC support
    this function and default included." and the short "Only EC support this
    function" (some forms, e.g. AIMB-234/MIO-5354, print the short one).
    """
    kw = keyword.lower()
    phrase = re.compile(
        r"only\s+ec\s+support\s+this\s+function"
        r"(\s+and\s+default\s+included)?",
        re.IGNORECASE,
    )

    for page in form_data.get("pages", []):
        page_text = page.get("text", "")
        if kw in page_text.lower() and phrase.search(page_text):
            return True

        for table in page.get("tables", []):
            table_has_kw = False
            for row in table:
                for cell in (row or []):
                    if kw in _cell(cell).lower():
                        table_has_kw = True
                        break
                if table_has_kw:
                    break
            if not table_has_kw:
                continue

            for row in table:
                for cell in (row or []):
                    if phrase.search(_cell(cell)):
                        return True
    return False


def _extract_platform_name(form_data: dict) -> str:
    for page in form_data.get("pages", []):
        for table in page.get("tables", []):
            for row in table:
                if not isinstance(row, list) or len(row) < 2:
                    continue
                if "platform name" in (_cell(row[0])).lower():
                    return _cell(row[1])
    for page in form_data.get("pages", []):
        m = re.search(r"Platform\s+Name\s+([^\n\r]+)", page.get("text", ""), re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def _extract_bios_version(form_data: dict) -> str:
    """Best-effort BIOS version extraction from form JSON text/tables."""
    for page in form_data.get("pages", []):
        for table in page.get("tables", []):
            for row in table:
                if not isinstance(row, list) or len(row) < 2:
                    continue
                left = _cell(row[0]).lower()
                if "bios version" in left:
                    return _cell(row[1])
    for page in form_data.get("pages", []):
        text = page.get("text", "")
        m = re.search(r"BIOS\s*Version\s*[:：]?\s*([^\n\r]+)", text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def _resolve_information_values(req: dict) -> tuple[str, str]:
    """Resolve [Information] values with spec+bios mixed priority.

    Priority:
    - PlatformVersion: spec(info/platform/platform_version) -> bios(info/platform) -> req.platform
    - BIOSVersion: spec(info/bios_version) -> bios(info/bios_version) -> req.bios_version
    """
    info = req.get("_spec_information", {}) or {}
    bios = req.get("_bios_info", {}) or {}

    platform_version = (
        info.get("PlatformVersion")
        or info.get("platform")
        or info.get("platform_version")
        or bios.get("PlatformVersion")
        or bios.get("platform")
        or req.get("platform", "")
    )
    bios_version = (
        info.get("BIOSVersion")
        or info.get("bios_version")
        or bios.get("BIOSVersion")
        or bios.get("bios_version")
        or req.get("bios_version", "")
    )
    return (platform_version or "", bios_version or "")


def _extract_hwm_chip(form_data: dict) -> str:
    """Find HWM EC chip name from the HWM table.

    Two layouts exist across platforms:
    - Layout A (ITA-580): 'Chip|Name|Remark' sub-table has the EC chip in the Chip column.
      The earlier 'Chip|Remark' (no Name) sub-table holds the SPI-flash chip (not HWM).
    - Layout B (SOM-6884): 'Chip|Name|Remark' Chip column is empty; actual chip is in
      the preceding 'Chip|Remark' sub-table.
    Try Layout A first; fall back to Layout B.
    """
    table = _find_table(form_data, "Hardware Monitor")
    if not table:
        return ""

    def _bad(v: str) -> bool:
        return not v or "<add if needed>" in v.lower() or "■" in v or "□" in v

    # Pass 1 — Layout A: "Chip | Name | Remark" Chip column
    chip_col = None
    for row in table:
        cells = [_cell(c) for c in (row or [])]
        if chip_col is None:
            if "Chip" in cells and "Name" in cells:
                chip_col = cells.index("Chip")
                continue
        else:
            if chip_col < len(cells) and not _bad(cells[chip_col]):
                return cells[chip_col]

    # Pass 2 — Layout B: "Chip | Remark" (no Name column) first data row
    seen_header = False
    for row in table:
        cells = [_cell(c) for c in (row or [])]
        if not seen_header:
            if "Chip" in cells and "Name" not in cells and "Remark" in cells:
                seen_header = True
                continue
        else:
            for c in cells:
                if (not _bad(c) and "Chip" not in c and "Remark" not in c
                        and "Need" not in c and "Follow" not in c):
                    return c
    return ""


def _clean_ex(s: str) -> str:
    """Strip '<Ex: ...>' placeholders, fix common misspellings, strip whitespace."""
    s = re.sub(r"<Ex:[^>]*>", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\btemerature\b", "temperature", s, flags=re.IGNORECASE)
    return s.strip()


def _extract_hwm_items(form_data: dict) -> list[str]:
    """Extract signal names from HWM 'Chip | Name | Remark' sub-table."""
    table = _find_table(form_data, "Hardware Monitor")
    if not table:
        return []
    items = []
    name_col = None
    for row in table:
        cells = [_cell(c) for c in (row or [])]
        if name_col is None:
            if "Chip" in cells and "Name" in cells:
                name_col = cells.index("Name")
                continue
        else:
            if name_col < len(cells):
                name = _clean_ex(cells[name_col])
                if name and "<Add If Needed>" not in name:
                    items.append(name)
    return items


def _extract_gpio_chip(form_data: dict) -> str:
    """Find GPIO chip name from the GPIO table's Chip column."""
    for page in form_data.get("pages", []):
        for table in page.get("tables", []):
            if not table or not table[0]:
                continue
            if "gpio" not in _cell(table[0][0]).lower():
                continue
            chip_col = None
            for row in table:
                cells = [_cell(c) for c in (row or [])]
                if chip_col is None:
                    if "Chip" in cells:
                        chip_col = cells.index("Chip")
                        continue
                else:
                    if chip_col < len(cells) and cells[chip_col]:
                        return cells[chip_col]
    return ""


def _extract_gpio_pins(form_data: dict) -> list[dict]:
    """Extract per-pin direction from GPIO table.
    Returns list of {pin: int, support: str, name: str} sorted by pin number."""
    def _gpio_index(value):
        text = _cell(value)
        if text.isdigit():
            return int(text)
        if not re.match(r"^\s*(?:GPI|GPO|GPIO|GP)", text, re.IGNORECASE):
            return None
        numbers = re.findall(r"\d+", text)
        return int(numbers[-1]) if numbers else None

    pins = {}
    gpio_started = False
    for page in form_data.get("pages", []):
        for table in page.get("tables", []):
            if not table or not table[0]:
                continue
            first_cells = [_cell(c) for c in (table[0] or [])]
            has_gpio_header = any("gpio" in cell.lower() for cell in first_cells)
            has_gpio_row = len(first_cells) > 1 and _gpio_index(first_cells[1]) is not None
            if not has_gpio_header and not (gpio_started and has_gpio_row):
                continue
            gpio_started = True
            pin_col = sup_col = name_col = None
            for row in table:
                cells = [_cell(c) for c in (row or [])]
                if pin_col is None:
                    lowered = [cell.lower() for cell in cells]
                    if "pin" in lowered and "support" in lowered:
                        pin_col = lowered.index("pin")
                        sup_col = lowered.index("support")
                        for candidate in ("Name", "GPIO Name", "Signal Name"):
                            candidate_lower = candidate.lower()
                            if candidate_lower in lowered:
                                name_col = lowered.index(candidate_lower)
                                break
                        continue
                    if has_gpio_row and len(cells) > 1 and _gpio_index(cells[1]) is not None:
                        pin_col = 1
                        sup_col = 3
                        name_col = 6 if len(cells) > 6 else None
                    else:
                        continue

                idx = _gpio_index(cells[pin_col]) if pin_col < len(cells) else None
                if idx is None:
                    continue
                sup = cells[sup_col] if sup_col is not None and sup_col < len(cells) else ""
                direction = "input" if "input" in sup.lower() else "output"
                name = cells[name_col] if name_col is not None and name_col < len(cells) else ""
                pins[idx] = {"pin": idx, "support": direction, "name": name}

    return [pins[idx] for idx in sorted(pins)]


def _extract_gpio_count(form_data: dict) -> int:
    """Extract GPIO count from GPIO table header.
    Handles 'Total:N Pins' and 'Total:Nin/Mout' (returns N+M)."""
    for page in form_data.get("pages", []):
        for table in page.get("tables", []):
            if not table or not table[0]:
                continue
            fc = _cell(table[0][0])
            if "gpio" not in fc.lower():
                continue
            # "Total: 4in/4out" format
            m = re.search(r'Total\s*:\s*(\d+)\s*in\s*/\s*(\d+)\s*out', fc, re.IGNORECASE)
            if m:
                return int(m.group(1)) + int(m.group(2))
            # "Total:N Pins" or plain "Total:N"
            m = re.search(r'Total\s*:\s*(\d+)', fc, re.IGNORECASE)
            if m:
                return int(m.group(1))
    return 0


def _extract_smartfan(form_data: dict) -> list[str]:
    """Extract fan names from SmartFan table."""
    table = _find_table(form_data, "SmartFan")
    if not table:
        return []
    fans = []
    past_header = False
    for row in table:
        cells = [_cell(c) for c in (row or [])]
        if not past_header:
            if any("fan name" in c.lower() for c in cells):
                past_header = True
            continue
        if len(cells) > 1:
            name = _clean_ex(cells[1])
            if name and "<Add If Needed>" not in name:
                fans.append(name)
    return fans


def _extract_i2c_ec(form_data: dict) -> bool:
    """Return True if any I2C socket is assigned to an EC chip."""
    table = _find_table(form_data, "I2C")
    if not table:
        return False
    past_header = False
    for row in table:
        cells = [_cell(c) for c in (row or [])]
        if not past_header:
            if any("hw socket" in c.lower() for c in cells):
                past_header = True
            continue
        if len(cells) > 1 and re.match(r'^I2C\d+$', cells[1], re.IGNORECASE):
            return True
    return False


def _match_alias(name: str, alias: dict, *, fuzzy: bool = True, threshold: float = 0.84) -> tuple | None:
    """Try exact → substring → normalized → fuzzy matching against alias dict.

    Returns (matched_alias_key, ini_key, ch_candidates) or None.
    ch_candidates is a list of xml channel names to try in order.

    Fuzzy matching is deterministic (difflib), used as typo-tolerant fallback.
    It is guarded by a confidence threshold to avoid aggressive mis-matches.
    """
    if not name:
        return None

    # 1) exact
    if name in alias:
        return (name,) + alias[name]

    # 2) substring (prefer longer keys first to prevent shadowing)
    name_lower = name.lower()
    for key in sorted(alias, key=len, reverse=True):
        if key.lower() in name_lower:
            return (key,) + alias[key]

    # 3) normalized exact (ignore spaces/punctuations)
    def _norm(s: str) -> str:
        return re.sub(r"[^a-z0-9]+", "", (s or "").lower())

    norm_name = _norm(name)
    for key in alias:
        if _norm(key) == norm_name:
            return (key,) + alias[key]

    # 4) deterministic fuzzy fallback for minor typos
    if fuzzy and norm_name:
        best_key = None
        best_score = 0.0
        for key in alias:
            score = difflib.SequenceMatcher(None, norm_name, _norm(key)).ratio()
            if score > best_score:
                best_score = score
                best_key = key

        if best_key and best_score >= threshold:
            print(f"[II] Alias fuzzy-match: '{name}' -> '{best_key}' (score={best_score:.2f})")
            return (best_key,) + alias[best_key]

    return None


def _find_channel(ch_candidates: list[str], ch_dict: dict) -> str:
    """Return first code found in ch_dict for any candidate channel name."""
    for ch in ch_candidates:
        if ch in ch_dict:
            return ch_dict[ch]
    return ""


def _norm_token(s: str) -> str:
    return re.sub(r"[\s\-_.]", "", (s or "")).upper()


def _find_channel_soft(ch_name: str, ch_dict: dict) -> str:
    """Resolve channel name with exact+normalized+prefix matching.

    Used for routing-table names like AUXOUT that may appear as AUXOUT0 on
    some chips.
    """
    if not ch_name:
        return ""

    code = _find_channel([ch_name], ch_dict)
    if code:
        return code

    target = _norm_token(ch_name)
    if not target:
        return ""

    for k, v in ch_dict.items():
        if _norm_token(k) == target:
            return v

    for k, v in ch_dict.items():
        nk = _norm_token(k)
        if nk.startswith(target) or target.startswith(nk):
            return v

    return ""


def _load_brightness_routing_rules(root: Path) -> list[dict]:
    """Load optional board-level brightness routing rules.

    Lookup order:
      1) <root>/mappings/brightness_routing.json
      2) <repo>/mappings/brightness_routing.json
    """
    candidates = [
        root / "mappings" / "brightness_routing.json",
        Path(__file__).parent / "mappings" / "brightness_routing.json",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                rules = data.get("rules", [])
            elif isinstance(data, list):
                rules = data
            else:
                rules = []
            return [r for r in rules if isinstance(r, dict)]
        except Exception as e:
            print(f"[WW] Failed to read brightness routing file: {p} ({e})")
            return []
    return []


def _match_brightness_route(
    rules: list[dict],
    *,
    project: str,
    controller_chip: str,
    socket: str,
    display_chip: str,
) -> str:
    """Return routed channel name (e.g. AUXOUT) when a rule matches."""
    p = _norm_token(project)
    c = _norm_token(controller_chip)
    s = _norm_token(socket)
    d = _norm_token(display_chip)

    for r in rules:
        rp = _norm_token(str(r.get("project", "")))
        rc = _norm_token(str(r.get("controller_chip", "")))
        rs = _norm_token(str(r.get("socket", "")))
        rd = _norm_token(str(r.get("display_chip", "")))

        if rp and rp != p:
            continue
        if rc and rc != c:
            continue
        if rs and rs != s:
            continue
        if rd and rd != d:
            continue

        ch = (r.get("channel") or "").strip()
        if ch:
            return ch

    return ""


def parse_req_from_json(in_json: Path) -> dict:
    with open(in_json, "r", encoding="utf-8") as f:
        form_data = json.load(f)

    return {
        "platform":        _extract_platform_name(form_data),
        "bios_version":    _extract_bios_version(form_data),
        "_bios_info":      {},
        "_spec_information": {},
        "chip_raw":        _extract_hwm_chip(form_data),
        "gpio_chip_raw":   _extract_gpio_chip(form_data),
        # Optional per-feature chip overrides (kept empty for raw-form path).
        "wdt_chip_raw":    "",
        "storage_chip_raw": "",
        "thermal_chip_raw": "",
        "hwm_items":       _extract_hwm_items(form_data),
        "gpio_count":      _extract_gpio_count(form_data),
        "gpio_pins":       _extract_gpio_pins(form_data),
        "smartfan":        _extract_smartfan(form_data),
        "has_ec_i2c":      _extract_i2c_ec(form_data),
        "smbus":           _section_enabled(form_data, "SMBus"),
        "brightness":      _section_enabled(form_data, "Brightness"),
        "backlight":       _section_enabled(form_data, "Backlight"),
        "wdt":             _section_enabled(form_data, "Watchdog"),
        "pinevent":        _section_enabled(form_data, "PinEvent"),
        "storage":         _section_present(form_data, "Storage"),
        "thermalprotect":  _section_present(form_data, "Thermal Protect"),
    }


def parse_req_from_spec(spec_path: Path) -> dict:
    """Load a {project}-spec.json (Stage 1 LLM output) into the same req dict
    that generate_ini consumes.  spec.json is already clean and normalized —
    no alias matching needed for chip names or ini_key values."""
    with open(spec_path, "r", encoding="utf-8") as f:
        s = json.load(f)

    feats = s.get("features", {})
    gpio  = s.get("gpio", {})
    screen_control = s.get("screen_control", {})
    info = s.get("information", {}) if isinstance(s.get("information", {}), dict) else {}
    bios_info = s.get("bios", {}) if isinstance(s.get("bios", {}), dict) else {}

    # Rebuild hwm_items as a flat list of labels (volt/temp/fan/current interleaved)
    # so the existing alias-match path in generate_ini still works as fallback.
    # But also store the pre-resolved spec lists for direct use.
    volt_specs = s.get("voltages", [])
    temp_specs = s.get("temperatures", [])
    fan_specs  = s.get("fans", [])
    cur_specs  = s.get("currents", [])
    case_specs = s.get("caseopen", [])

    return {
        "platform":        s.get("platform", s.get("project", "")),
        "bios_version":    s.get("bios_version", ""),
        "_spec_information": info,
        "_bios_info":      bios_info,
        "chip_raw":        s.get("chips", {}).get("hwm", ""),
        "gpio_chip_raw":   s.get("chips", {}).get("gpio", ""),
        # Optional explicit overrides: when provided, these can route EC-only
        # features to another named controller.
        "wdt_chip_raw":    s.get("chips", {}).get("wdt", ""),
        "storage_chip_raw": s.get("chips", {}).get("storage", ""),
        "thermal_chip_raw": s.get("chips", {}).get("thermalprotect", ""),

        # Pre-resolved spec items — generate_ini checks for these first
        "_spec_voltages":  volt_specs,   # [{"ini_key": "V120", "label": "+12V"}, ...]
        "_spec_temps":     temp_specs,
        "_spec_fans":      fan_specs,
        "_spec_currents":  cur_specs,
        "_spec_caseopen":  case_specs,

        # Legacy flat list as fallback (label strings)
        "hwm_items":       [x["label"] for x in volt_specs + temp_specs + fan_specs + cur_specs + case_specs],

        "gpio_count":      gpio.get("count", 0),
        "gpio_pins":       [{
                                "pin": p["index"],
                                "support": p.get("direction", "both"),
                                "chip": p.get("chip", ""),
                                "location": p.get("location", ""),
                                "name": p.get("name", ""),
                             }
                            for p in gpio.get("pins", [])],
        "smartfan":        [f["label"] for f in fan_specs
                            if f["ini_key"] in s.get("smartfan", [])],
        "_spec_smartfan":  s.get("smartfan", []),   # list of ini_key strings
        "_spec_screen_control": screen_control,

        "has_ec_i2c":      feats.get("i2c", False),
        "smbus":           feats.get("smbus", False),
        "brightness":      feats.get("brightness", False),
        "backlight":       feats.get("backlight", False),
        "wdt":             feats.get("wdt", False),
        "pinevent":        feats.get("wdt_pinevent", False),
        "storage":         feats.get("storage", False),
        "thermalprotect":  feats.get("thermalprotect", False),
    }


# ---------- INI generator ----------

def generate_ini(root: Path, in_json: Path, out_ini: Path,
                 digit_json: Path | None = None,
                 only_section: str | None = None,
                 probe_spec: dict | None = None) -> Path | None:
    """Generate a pre-INI.

    only_section : when set (per-section split mode), output keeps only
        [Information] + this section. Empty-value keys are dropped.
        If the target section has no non-empty values, no file is written
        and None is returned.
    probe_spec : parsed probe report (see parse_probe.parse_probe_report). When
        given, the probe's [OK] channel list is authoritative for WHICH HWM
        channels are emitted; PDF supplies labels, chip_db supplies codes. A
        probe channel whose code cannot be resolved is left empty (no fabrication).
    """
    def _is_active(_cat: str) -> bool:
        # Value derivation is always full; section pruning happens at output stage.
        return True

    # Keep raw form JSON available for conservative fallback heuristics.
    raw_form = None
    if in_json.exists():
        try:
            with open(in_json, "r", encoding="utf-8") as f:
                raw_form = json.load(f)
        except Exception:
            raw_form = None

    # Prefer spec.json (Stage 1 LLM output) over raw form.json when available.
    # spec.json lives next to in_json with stem "{project}-spec".
    spec_path = in_json.parent / (in_json.stem.replace("-pre", "") + "-spec.json")
    if spec_path.exists():
        req = parse_req_from_spec(spec_path)
    elif in_json.exists():
        req = parse_req_from_json(in_json)
    else:
        raise FileNotFoundError(f"Input not found: {in_json}")

    # Load supplementary circuit diagram JSON if available
    if digit_json is None:
        digit_json = _find_digit_json(root, in_json.stem)
    digit_hints = parse_digit_json(digit_json) if (digit_json and digit_json.exists()) else {}

    # Load chip DB
    # db = ChipDB.load()
    raise RuntimeError(
        "chip_db flow is disabled by policy. Use config_new.db query flow (query_config_db.py / candidate_query_skill)."
    )

    # ── HWM chip lookup ──────────────────────────────────────────────────────
    chip_raw = req.get("chip_raw", "")
    # If chip_raw is only a template placeholder (e.g. "<Ex: Winbond W83627>"),
    # fall back to gpio_chip_raw — the same EC chip typically handles both.
    if not chip_raw or re.fullmatch(r"\s*<[^>]+>\s*", chip_raw):
        chip_raw = req.get("gpio_chip_raw", "")

    def _lookup_hwm(chip: str):
        h_hwid, v_sup, v_ports = db.lookup(chip, "HWM/Voltage")
        _,      t_sup, _       = db.lookup(chip, "HWM/Temperature")
        _,      f_sup, _       = db.lookup(chip, "HWM/Fan")
        _,      c_sup, _       = db.lookup(chip, "HWM/Current")
        _,      co_sup, co_ports = db.lookup(chip, "HWM/CaseOpen")
        return h_hwid, v_sup, v_ports, t_sup, f_sup, c_sup, co_sup, co_ports

    def _expand_chip_candidates(text: str) -> list[str]:
        """Expand a possibly composite chip field into concrete lookup candidates.
        Example: 'NCT6694B-\nA(EIO-300)' -> ['NCT6694B-', 'EIO-300', ...]."""
        t = (text or "").strip()
        if not t:
            return []

        cands: list[str] = []

        def _push(x: str):
            x = (x or "").strip()
            if not x:
                return
            if x not in cands:
                cands.append(x)

        _push(t)

        # Split by common separators first (keep meaningful tokens intact).
        for part in re.split(r"[\n;,/|]+", t):
            _push(part)

        # Add parenthesized content, often where actual module code lives.
        for m in re.finditer(r"\(([^)]+)\)", t):
            inner = m.group(1).strip()
            if re.match(r"P/N\s*:", inner, re.IGNORECASE):
                continue
            _push(inner)

        # Explicit model-like tokens (EIO-300 / NCT6694B / ITE8528 ...).
        token_pat = r"\b(?:EIO-\d+|NCT\d+[A-Z0-9\-]*|ITE\d+[A-Z0-9\-]*|PCA\d+[A-Z0-9\-]*|TCA\d+[A-Z0-9\-]*)\b"
        for tok in re.findall(token_pat, t, flags=re.IGNORECASE):
            _push(tok)

        return cands

    def _try_hwm_candidates(candidates: list[str]):
        nonlocal chip_raw
        # Preserve order, drop semantic duplicates.
        _norm = lambda s: re.sub(r"[\s\-_.]", "", (s or "")).upper()
        seen = set()
        for cand in candidates:
            norm = _norm(cand)
            if not cand or norm in seen:
                continue
            seen.add(norm)
            h_hwid, v_sup, v_ports, t_sup, f_sup, c_sup, co_sup, co_ports = _lookup_hwm(cand)
            if h_hwid:
                return cand, h_hwid, v_sup, v_ports, t_sup, f_sup, c_sup, co_sup, co_ports
        return None

    # 1) Try spec/raw primary chip directly, then its expanded sub-candidates.
    picked = _try_hwm_candidates(_expand_chip_candidates(chip_raw))
    if picked:
        chip_raw, hwm_hwid, volt_sup, volt_ports, temp_sup, fan_sup, curr_sup, case_sup, case_ports = picked
    else:
        hwm_hwid, volt_sup, volt_ports, temp_sup, fan_sup, curr_sup, case_sup, case_ports = _lookup_hwm(chip_raw)

    # Spec can occasionally mislabel chips.hwm as a non-HWM device (e.g. SPI flash).
    # When that happens, try chip hints from raw form.json before giving up.
    if not hwm_hwid:
        fallback_cands = []
        try:
            if raw_form is not None:
                req_json = parse_req_from_json(in_json)
                fallback_cands.extend([
                    req_json.get("chip_raw", ""),
                    req_json.get("gpio_chip_raw", ""),
                ])
        except Exception:
            pass

        # Expand each fallback field into concrete chip candidates.
        expanded = []
        for cand in fallback_cands:
            expanded.extend(_expand_chip_candidates(cand))

        picked = _try_hwm_candidates(expanded)
        if picked:
            prev_chip = chip_raw
            chip_raw, hwm_hwid, volt_sup, volt_ports, temp_sup, fan_sup, curr_sup, case_sup, case_ports = picked
            print(f"[II] HWM chip fallback: '{prev_chip}' -> '{chip_raw}'")

    hwid = hwm_hwid or ""
    volt = db.support_as_dict(volt_sup)
    temp = db.support_as_dict(temp_sup)
    fan  = db.support_as_dict(fan_sup)
    curr = db.support_as_dict(curr_sup)
    case = db.support_as_dict(case_sup)
    hwm_ioport  = _first_port(volt_ports)   # e.g. "0x2E" for SuperIO, "0" for EC
    case_ioport = _first_port(case_ports) or hwm_ioport
    # Use resolved chip key for capability gating (avoid composite/raw-text drift).
    resolved_chip = (
        db.find_chip_key_for_category(chip_raw, "HWM/Voltage")
        or db.find_chip_key(chip_raw)
        or chip_raw
    )
    chip_cats   = set(db.categories_for(resolved_chip))  # which functions this chip supports
    chip_is_ec  = "Storage" in chip_cats                # EC chips have Storage; SuperIO chips don't

    # EC-only rule: WDT/Storage/ThermalProtect resolve against EC chip hint by
    # default. Only when an explicit per-feature chip override is provided do we
    # query additional chip names.
    ec_chip_hint = req.get("gpio_chip_raw", "") or chip_raw

    def _has_explicit_chip(v: str) -> bool:
        if not v:
            return False
        vv = str(v).strip()
        if not vv:
            return False
        if re.fullmatch(r"\s*<[^>]+>\s*", vv):
            return False
        # Template noise is not an explicit chip: "Ex: ..." placeholder
        # examples and red-print form-filler guidance sentences.
        if re.match(r"^\s*[<{(\[]?\s*Ex\s*[:：]", vv, re.IGNORECASE):
            return False
        if re.search(r"only ec support", vv, re.IGNORECASE):
            return False
        return True

    def _resolve_chip_for_category(category: str, explicit_chip: str = "") -> str:
        hints: list[str] = []
        if _has_explicit_chip(explicit_chip):
            hints.append(str(explicit_chip).strip())
        if ec_chip_hint:
            hints.append(ec_chip_hint)
        # Only when explicit override exists, broaden search to HWM chip text.
        if _has_explicit_chip(explicit_chip) and chip_raw:
            hints.append(chip_raw)

        seen = set()
        for hint in hints:
            if hint in seen:
                continue
            seen.add(hint)
            key = db.find_chip_key_for_category(hint, category)
            if key:
                return key
        return ""

    wdt_chip = _resolve_chip_for_category("WDT", req.get("wdt_chip_raw", ""))
    storage_chip = _resolve_chip_for_category("Storage", req.get("storage_chip_raw", ""))
    thermal_chip = _resolve_chip_for_category("ThermalProtect", req.get("thermal_chip_raw", ""))

    wdt_hwid, wdt_sup, wdt_ports = db.lookup(wdt_chip or ec_chip_hint, "WDT")
    storage_hwid, _, _ = db.lookup(storage_chip or ec_chip_hint, "Storage")
    thermal_hwid, _, _ = db.lookup(thermal_chip or ec_chip_hint, "ThermalProtect")
    wdt_ioport = _first_port(wdt_ports)

    # Hybrid gate for Storage / ThermalProtect (user decision 2026-07-14):
    #   layer 0  non-EC has neither — enforced naturally by the DB lookup
    #            (storage/thermal_hwid stay None for non-EC chips);
    #   layer 1  section header checkbox wins: "■ Storage (EEPROM)" -> on,
    #            "□ Storage (EEPROM)" -> off (even if golden disagrees —
    #            the form is the requirement source);
    #   layer 2  no checkbox -> spec feature or EC-support wording in the
    #            section text ("Only EC support this function [and default
    #            included]") counts as on.
    # WDT deliberately stays feature+DB driven: SuperIO chips do have WDT
    # (MIO-5152 golden: WDT1 on NCT6126D), so no EC-only gate applies.
    storage_req = bool(req.get("storage"))
    thermal_req = bool(req.get("thermalprotect"))
    if raw_form is not None:
        storage_box = _section_checkbox_state(raw_form, "storage (eeprom)")
        thermal_box = _section_checkbox_state(raw_form, "thermal protect")
        storage_text = _section_default_included(raw_form, "Storage")
        thermal_text = _section_default_included(raw_form, "Thermal Protect")

        new_storage = storage_box if storage_box is not None \
            else (storage_req or storage_text)
        new_thermal = thermal_box if thermal_box is not None \
            else (thermal_req or thermal_text)
        new_storage = new_storage and bool(storage_hwid)
        new_thermal = new_thermal and bool(thermal_hwid)

        if new_storage != storage_req:
            src = "checkbox" if storage_box is not None else "EC-support note"
            print(f"[II] Storage {'enabled' if new_storage else 'disabled'} by hybrid gate ({src})")
        if new_thermal != thermal_req:
            src = "checkbox" if thermal_box is not None else "EC-support note"
            print(f"[II] ThermalProtect {'enabled' if new_thermal else 'disabled'} by hybrid gate ({src})")
        storage_req = new_storage
        thermal_req = new_thermal

    # ── VGA/Brightness lookup (for IOPort and channel) ───────────────────────
    _, bright_sup, bright_ports = db.lookup(chip_raw, "VGA/Brightness")
    bright_ch_dict = db.support_as_dict(bright_sup)
    bright_ioport  = _first_port(bright_ports)

    # Default fallback: first channel from chip DB (legacy behavior).
    bright_ch_code = next(iter(bright_ch_dict.values()), "") if bright_ch_dict else ""

    # Optional board-level routing override from screen_control + mapping table.
    # Example: Nuvoton NCT6126D + LVDS1 + PTN3460 -> AUXOUT
    sc = req.get("_spec_screen_control", {})
    bnode = sc.get("brightness", {}) if isinstance(sc, dict) else {}
    bitems = bnode.get("items", []) if isinstance(bnode, dict) else []
    rules = _load_brightness_routing_rules(root)
    controller_chip = db.find_chip_key(chip_raw) or chip_raw
    project_token = req.get("platform", "")

    if bright_ch_dict and isinstance(bitems, list) and rules:
        for it in bitems:
            if not isinstance(it, dict):
                continue
            ch_name = _match_brightness_route(
                rules,
                project=project_token,
                controller_chip=controller_chip,
                socket=str(it.get("socket", "")),
                display_chip=str(it.get("chip", "")),
            )
            if not ch_name:
                continue

            routed_code = _find_channel_soft(ch_name, bright_ch_dict)
            if routed_code:
                bright_ch_code = routed_code
                print(
                    f"[II] Brightness route matched: chip='{controller_chip}' "
                    f"socket='{it.get('socket', '')}' display='{it.get('chip', '')}' -> {ch_name}"
                )
            else:
                print(
                    f"[WW] Brightness route channel '{ch_name}' not supported by chip '{controller_chip}'"
                )
            break

    # ── GPIO chip lookup ─────────────────────────────────────────────────────
    gpio_chip_raw = req.get("gpio_chip_raw", "")
    gpio_pin_rows = req.get("gpio_pins", [])
    _has_per_pin_gpio_chip = any(
        isinstance(p, dict) and _cell(p.get("chip", ""))
        for p in gpio_pin_rows
    )

    _gpio_profile_cache: dict[str, dict] = {}

    def _resolve_gpio_lookup_str(chip_text: str) -> str:
        """Pick the best lookup token for a GPIO chip string.
        Handles compound module strings like 'EIO-300 (Nuvoton_NCT6694B)'."""
        raw = (chip_text or "").strip()
        if not raw:
            return ""
        out = raw
        for _m in re.finditer(r"\(([^)]+)\)", raw.replace("\n", " ")):
            _inner = _m.group(1).strip()
            if re.match(r"P/N\s*:", _inner, re.IGNORECASE):
                continue
            _k = db.find_chip_key(_inner)
            if _k and "GPIO" in set(db.categories_for(_k)):
                out = _inner
                break
        return out

    def _gpio_profile(chip_text: str) -> dict:
        cache_key = (chip_text or "").strip() or "<default>"
        if cache_key in _gpio_profile_cache:
            return _gpio_profile_cache[cache_key]

        lookup_str = _resolve_gpio_lookup_str(chip_text or gpio_chip_raw)
        chip_cats = set(db.categories_for(lookup_str)) if lookup_str else set()
        gpio_hwid, gpio_sup, gpio_ports = db.lookup(lookup_str, "GPIO")
        pins_db = db.support_as_pins(gpio_sup)

        # I2C GPIO expanders include '0x00' in port list (device-address mode).
        _gpio_is_i2c = "0x00" in {p.strip().lower() for p in gpio_ports}
        if _gpio_is_i2c:
            base_opt = OPT_INIT_HW
        elif "Storage" in chip_cats:
            base_opt = OPT_BACKUP_S3S4
        elif "HWM/Voltage" in chip_cats:
            base_opt = OPT_BOTH
        else:
            base_opt = 0

        ioport = "0" if _gpio_is_i2c else _first_port(gpio_ports)
        chip_key = db.find_chip_key(lookup_str) or lookup_str or (chip_text or "")
        prof = {
            "hwid": gpio_hwid or hwid,
            "pins_db": pins_db,
            "base_opt": base_opt,
            "ioport": ioport,
            "chip_key": chip_key,
        }
        _gpio_profile_cache[cache_key] = prof
        return prof

    default_gpio_profile = _gpio_profile(gpio_chip_raw)
    gpio_pins_db = default_gpio_profile["pins_db"]
    gpio_base_opt = default_gpio_profile["base_opt"]
    gpio_ioport = default_gpio_profile["ioport"]
    g_hwid = default_gpio_profile["hwid"]

    # NOTE(case-candidate, disabled):
    # ITA-580 golden suggests TCA9554 uses IOPort 0x40 instead of 0, and bit order
    # maps as [4,5,6,7,0,1,2,3] based on DIO1 PIN6..9 then PIN1..4 table order.
    # Keep disabled until more projects confirm this is chip/connector-level behavior.
    #
    # if _gpio_is_i2c:
    #     # Prefer first non-0x00 address from DB, fallback to current behavior.
    #     _nz_ports = [p for p in gpio_ports if p.strip().lower() != "0x00"]
    #     gpio_ioport = _nz_ports[0] if _nz_ports else gpio_ioport
    #
    # _gpio_pin_map_override = None
    # if re.search(r"\bTCA9554\b", _gpio_lookup_str, re.IGNORECASE):
    #     # Observed in ITA-580 (DIO1 DSUB-9): GPIO00..07 -> bit 4,5,6,7,0,1,2,3
    #     _gpio_pin_map_override = [4, 5, 6, 7, 0, 1, 2, 3]

    # ── WDT option ───────────────────────────────────────────────────────────
    # EC chips (have 'Storage') use OPT_BACKUP_S3S4; SuperIO chips use OPT_BOTH.
    if "Storage" in chip_cats:
        wdt_opt = OPT_BACKUP_S3S4 | (WDT_PIN_EVENT if req.get("pinevent") else 0)
    else:
        wdt_opt = OPT_BOTH

    # ---------- alias tables (form signal name → (ini_key, [xml_ch_candidates])) ----------
    # Multiple channel candidates handle cross-chip differences in channel naming.
    # _find_channel() returns the first candidate that resolves in the chip's support dict.

    # Industry default: plain "+5V" means the main 5V rail (V50).
    # Standby rail must be explicitly labeled as 5VSB/+5VSB/5V Standby.

    volt_alias = {
        "VCORE":  ("VCORE", ["VCORE", "VCOREA", "ADCVCOREA"]),
        "AVCC":   ("VTT",   ["VACC"]),
        "3VVCC":  ("V33",   ["3VCC", "ADC33VS0"]),
        "3VSB":   ("V3SB",  ["V3SB", "33VSB", "ADC33VS5"]),
        # 5V standby: named channel first (EIO_IS200), ADC prefix (Advantech EC), generic fallback
        "+5VSB":  ("V5SB",  ["5VSB", "ADC5VS5", "VIN0"]),
        "5V Standby": ("V5SB", ["5VSB", "ADC5VS5", "VIN0"]),
        "5VSB":   ("V5SB",  ["5VSB", "ADC5VS5", "VIN0"]),
        "+5V":    ("V50",   ["5VS0", "ADC5VS0", "VIN1"]),
        "+12V":   ("V120",  ["12VS0", "ADC12VS0", "VIN2"]),
        "12V":    ("V120",  ["12VS0", "ADC12VS0", "VIN2"]),
        "VBAT":   ("VBAT",  ["VBAT", "CMOSBAT", "ADCCMOSBAT"]),
        "+Vin":   ("DC",    ["DC", "ADCDC"]),
    }
    # Chip-aware option bits:
    # EC chips: HWM voltage = OPT_INIT_HW, SmartFan = OPT_BACKUP_S3S4, Brightness = 0
    # SuperIO:  HWM voltage = OPT_BOTH,    SmartFan = OPT_BOTH,         Brightness = OPT_BOTH
    hwm_volt_opt  = OPT_INIT_HW     if chip_is_ec else OPT_BOTH
    tcpu_temp_opt = TEMP_MODE_BIT   if chip_is_ec else (OPT_BOTH | TEMP_MODE_BIT)
    sf_opt        = OPT_BACKUP_S3S4 if chip_is_ec else OPT_BOTH
    bright_opt    = 0               if chip_is_ec else OPT_BOTH
    temp_alias = {
        "CPU temperature":    ("TCPU", ["SMIOVT2 (Default: CPUTIN)",
                                        "Thermal 1 Remote (0x02 CPU)"]),
        "CPU temp":           ("TCPU", ["SMIOVT2 (Default: CPUTIN)",
                                        "Thermal 1 Remote (0x02 CPU)"]),
        "System temperature": ("TSYS", ["SMIOVT1 (Default: SYSTIN)",
                                        "Thermal 1 Local (0x01 SYS)"]),
        "System temp":        ("TSYS", ["SMIOVT1 (Default: SYSTIN)",
                                        "Thermal 1 Local (0x01 SYS)"]),
    }
    fan_alias = {
        "COM Module FAN":    ("FCPU",  ["CPU", "CPU1", "SYS1"]),
        "Carrier Board FAN": ("FSYS",  ["SYS", "SYS1", "OEM4"]),
        "SYS FAN1 Speed":    ("FSYS",  ["SYS"]),
        "System Fan":        ("FSYS",  ["SYS"]),
        "CPU fan":           ("FCPU",  ["CPU", "CPU1"]),
        "CPU Fan":           ("FCPU",  ["CPU", "CPU1"]),
        "System fan":        ("FSYS",  ["SYS", "SYS1"]),
        "SYS FAN":           ("FSYS",  ["SYS", "SYS1"]),
        "OEM Fan":           ("FOEM0", ["OEM", "OEM4"]),
    }
    current_alias = {
        "Current":  ("OEM0", ["Currnet", "Current"]),
        "Current1": ("OEM1", ["Currnet1", "Current1"]),
        "Current2": ("OEM2", ["Currnet2", "Current2"]),
    }
    caseopen_alias = {
        "CaseOpen":            ("CO0", ["CaseOpen", "CaseOpen#", "CaseOpen0", "CaseOpen#0", "Chassis Intrusion"]),
        "Case Open":           ("CO0", ["CaseOpen", "CaseOpen#", "CaseOpen0", "CaseOpen#0", "Chassis Intrusion"]),
        "CaseOpen1":           ("CO1", ["CaseOpen1", "CaseOpen#1"]),
        "Case Open 1":         ("CO1", ["CaseOpen1", "CaseOpen#1"]),
        "CaseOpen2":           ("CO2", ["CaseOpen2", "CaseOpen#2"]),
        "Case Open 2":         ("CO2", ["CaseOpen2", "CaseOpen#2"]),
    }

    # Categorize HWM items via alias tables (substring match, multi-channel lookup).
    # Only emit a value when BOTH hwid and channel code resolve — otherwise leave empty.
    volt_vals = {}
    temp_vals = {}
    fan_vals  = {}
    curr_vals = {}
    case_vals = {}
    for name in req["hwm_items"]:
        m = _match_alias(name, volt_alias)
        if m:
            canonical, key, ch_cands = m
            code = _find_channel(ch_cands, volt)
            if hwid and code:
                volt_vals[key] = f"{hwid},{code},{hwm_ioport},{hx(hwm_volt_opt)},0,0,{canonical},0"
            continue
        m = _match_alias(name, temp_alias)
        if m:
            canonical, key, ch_cands = m
            code = _find_channel(ch_cands, temp)
            if hwid and code:
                # Alias-derived Name fields are intentionally left empty to avoid
                # writing guessed labels into golden-sensitive sections.
                # TCPU uses chip-aware option; other temp signals use TEMP_MODE_BIT only
                t_opt = tcpu_temp_opt if key == "TCPU" else TEMP_MODE_BIT
                temp_vals[key] = f"{hwid},{code},{hwm_ioport},{hx(t_opt)},0,"
            continue
        m = _match_alias(name, fan_alias)
        if m:
            canonical, key, ch_cands = m
            code = _find_channel(ch_cands, fan)
            if hwid and code:
                fan_vals[key] = f"{hwid},{code},{hwm_ioport},{hx(OPT_INIT_HW)},0,"
            continue
        m = _match_alias(name, current_alias)
        if m:
            _, key, ch_cands = m
            code = _find_channel(ch_cands, curr)
            if hwid and code:
                curr_vals[key] = f"{hwid},{code},{hwm_ioport},{hx(OPT_BOTH)},"
            continue
        m = _match_alias(name, caseopen_alias)
        if m:
            _, key, ch_cands = m
            code = _find_channel(ch_cands, case)
            if hwid and code:
                case_vals[key] = f"{hwid},{code},{case_ioport},{hx(OPT_BOTH)},"

    # SmartFan
    sf_vals = {}
    for name in req.get("smartfan", []):
        m = _match_alias(name, fan_alias)
        if m:
            canonical, key, ch_cands = m
            code = _find_channel(ch_cands, fan)
            if hwid and code:
                sf_vals[key] = f"{hwid},{code},{hwm_ioport},{hx(sf_opt)},"

    # ── Probe reconciliation: probe is authoritative for WHICH HWM channels ───
    # (decided 2026-06-29) The probe [OK] list decides the emitted HWM key set.
    # Overlap with the PDF path keeps the PDF-resolved label + channel; a
    # probe-only key resolves its channel from chip_db with an empty label;
    # if no channel code resolves, the key stays empty (template-first).
    if probe_spec and _is_active("HWM"):
        def _invert(alias):
            rev: dict = {}
            for _label, (k, cands) in alias.items():
                rev.setdefault(k, [])
                for c in cands:
                    if c not in rev[k]:
                        rev[k].append(c)
            return rev

        rev_volt = _invert(volt_alias)
        rev_temp = _invert(temp_alias)
        rev_fan  = _invert(fan_alias)
        rev_case = _invert(caseopen_alias)
        phwm = probe_spec.get("hwm", {})

        def _reconcile(keys, cur_vals, rev, ch_dict, render):
            out = {}
            for k in keys:
                if k in cur_vals:          # PDF already resolved label + channel
                    out[k] = cur_vals[k]
                elif hwid:                 # probe-only: resolve channel, blank label
                    code = _find_channel(rev.get(k, []), ch_dict)
                    if code:
                        out[k] = render(k, code)
                # else: no chip / no channel → leave key empty (no fabrication)
            return out

        volt_vals = _reconcile(
            phwm.get("voltages", []), volt_vals, rev_volt, volt,
            lambda k, code: f"{hwid},{code},{hwm_ioport},{hx(hwm_volt_opt)},0,0,,0")
        temp_vals = _reconcile(
            phwm.get("temperatures", []), temp_vals, rev_temp, temp,
            lambda k, code: f"{hwid},{code},{hwm_ioport},"
                            f"{hx(tcpu_temp_opt if k == 'TCPU' else TEMP_MODE_BIT)},0,")
        fan_vals = _reconcile(
            phwm.get("fans", []), fan_vals, rev_fan, fan,
            lambda k, code: f"{hwid},{code},{hwm_ioport},{hx(OPT_INIT_HW)},0,")
        case_vals = _reconcile(
            phwm.get("caseopen", []), case_vals, rev_case, case,
            lambda k, code: f"{hwid},{code},{case_ioport},{hx(OPT_BOTH)},")
    # SmartFan / Fan.Control under probe — runs for BOTH the HWM and SmartFan
    # files so they stay in agreement.
    if probe_spec and (_is_active("HWM") or _is_active("SmartFan")):
        probe_fans = set((probe_spec.get("hwm") or {}).get("fans", []))
        # Keep only fans the probe confirms exist.
        sf_vals = {k: v for k, v in sf_vals.items() if k in probe_fans}
        # Fill spec-requested, probe-confirmed fans even when the PDF
        # enumerated no fan rows (the "Follow All BIOS HWM items" pattern):
        # fan control needs only a live fan channel, not PCB routing
        # (decided 2026-06-29; first hit in practice on SOM-6833).
        rev_fan_sf: dict = {}
        for _label, (k, cands) in fan_alias.items():
            rev_fan_sf.setdefault(k, [])
            for c in cands:
                if c not in rev_fan_sf[k]:
                    rev_fan_sf[k].append(c)
        for k in req.get("_spec_smartfan", []):
            if k in sf_vals or k not in probe_fans or not hwid:
                continue
            code = _find_channel(rev_fan_sf.get(k, []), fan)
            if code:
                sf_vals[k] = f"{hwid},{code},{hwm_ioport},{hx(sf_opt)},"

    # ---------- output builders ----------
    lines = []

    def section(name):    lines.append(f"[{name}]")
    def comment(text=""): lines.append((";" + text) if text else ";")
    def kv(k, v=""):      lines.append(f"{k}={v}")
    def blank():          lines.append("")

    # ── [Information] ────────────────────────────────────────────────────────
    platform_version, bios_version = _resolve_information_values(req)
    section("Information")
    kv("IniVersion",      "1.0.1.0")
    kv("PlatformVersion", platform_version)
    kv("BIOSVersion",     bios_version)
    kv("SusiAi",          "0")
    kv("FollowConfigure", "1")
    comment("------------------------------------------------------------------------------")
    comment(" Option descrption")
    comment("------------------------------------------------------------------------------")
    comment(" Bit[31:24] is for all devices")
    comment("  0x0X000000 = Bit[24:27]\tSelect two wire channel if device need")
    comment("  0x20000000 = Bit[29]\t\tGlobal Backup HW Settinga when S3 / S4")
    comment("  0x80000000 = Bit[31]\t\tGlobal Initial HW Settings after boot")
    comment(" Bit[23:0] is for individual devices")
    comment("------------------------------------------------------------------------------")
    blank()
    blank()

    # ── [SMBus] ──────────────────────────────────────────────────────────────
    section("SMBus")
    comment(" * SMBus Channel maximum number is 5.")
    comment("[Channel]=[HW],[Channel],[IOPort],[Option],[Name]")
    kv("Channel1", f"0x00000001,0,0,{hx(OPT_BOTH)}," if _is_active("SMBus") else "")   # FCH/ACH native
    if _is_active("SMBus") and req.get("smbus") and hwid and "SMBus" in chip_cats:
        kv("Channel2", f"{hwid},0x80000000,0,{hx(OPT_BOTH)},")
    else:
        kv("Channel2")
    kv("Channel3")
    kv("Channel4")
    kv("Channel5")
    blank()

    # ── [I2C] ────────────────────────────────────────────────────────────────
    section("I2C")
    comment(" * I2C Channel maximum number is 5.")
    comment("[Channel]=[HW],[Channel],[IOPort],[Option],[Name]")
    if _is_active("I2C") and digit_hints.get("i2c_come") and COME_I2C_DEFAULT_KEY in COME_I2C_TABLE:
        pch_hwid, pch_ch = COME_I2C_TABLE[COME_I2C_DEFAULT_KEY]
        kv("Channel1", f"{pch_hwid},{pch_ch},0,,")
    else:
        kv("Channel1")
    if _is_active("I2C") and req.get("has_ec_i2c") and hwid and "I2C" in chip_cats:
        kv("Channel2", f"{hwid},0x80000000,0,{hx(OPT_BACKUP_S3S4)},")
        kv("Channel3", f"{hwid},0x80000001,0,{hx(OPT_BACKUP_S3S4)},")
    else:
        kv("Channel2")
        kv("Channel3")
    kv("Channel4")
    kv("Channel5")
    blank()

    # ── [VGA] ────────────────────────────────────────────────────────────────
    section("VGA")
    comment(" TBD")
    blank()

    # ── [VGA.Backlight] ──────────────────────────────────────────────────────
    section("VGA.Backlight")
    comment(" For On/Off")
    comment("[Backlight]=[HW],[Channel],[IOPort/Address],[Option]")
    if _is_active("VGA") and req.get("backlight") and hwid and "VGA/Backlight" in chip_cats:
        kv("Backlight1", f"{hwid},0x80000000,{hwm_ioport},{hx(OPT_BOTH)},")
    else:
        kv("Backlight1")
    kv("Backlight2")
    kv("Backlight3")
    blank()

    # ── [VGA.Brightness] ─────────────────────────────────────────────────────
    section("VGA.Brightness")
    comment("[Brightness]=[HW],[Channel],[IOPort/Address],[Option],[Max],[Min],[Frequency],[Name]")
    comment(" Option: (Flag mode)")
    comment("  0x00000001 = PWM Interval - 0 = Disable, 1 = Enable")
    comment("  0x00000008 = Set PWM Interval after boot")
    comment("  0x00000080 = Set PWM Frequency after boot (Hz)")
    if _is_active("VGA") and req.get("brightness") and hwid and "VGA/Brightness" in chip_cats and bright_ch_code:
        bright_max = "100" if chip_is_ec else "255"
        kv("Brightness1", f"{hwid},{bright_ch_code},{bright_ioport},{hx(bright_opt)},{bright_max},0,0,")
    else:
        kv("Brightness1")
    kv("Brightness2")
    kv("Brightness3")
    blank()

    # ── [HWM.Voltage] ────────────────────────────────────────────────────────
    volt_order = [
        "VCORE", "VCORE2", "V25", "V33", "V50",
        "V120", "V5SB", "V3SB", "VBAT",
        "VN50", "VN120", "VTT", "V240", "DC", "DCSTBY", "VBATLI",
        "V15", "V18", "V105", "VOEM0", "VOEM1", "VOEM2", "VOEM3", "V5S5", "V3S5",
    ]
    section("HWM.Voltage")
    comment("[Item]=[HW],[Channel],[IOPort/Device Address],[option],[R1],[R2],[Name]")
    for k in volt_order:
        kv(k, volt_vals.get(k, ""))
    blank()

    # ── [HWM.Current] ────────────────────────────────────────────────────────
    section("HWM.Current")
    comment("[Item]=[HW],[Channel],[IOPort/Device Address],[option],[Name]")
    kv("OEM0", curr_vals.get("OEM0", ""))
    kv("OEM1", curr_vals.get("OEM1", ""))
    kv("OEM2", curr_vals.get("OEM2", ""))
    blank()

    # ── [HWM.Temperature] ────────────────────────────────────────────────────
    temp_order = [
        "TCPU", "TCPU2", "TSYS", "TCHIPSET",
        "TOEM0", "TOEM1", "TOEM2", "TOEM3", "TOEM4", "TOEM5", "TOEM6",
    ]
    section("HWM.Temperature")
    comment("[Item]=[HW],[Channel],[IOPort],[option],[Temp Offset] ,[Name]")
    for k in temp_order:
        kv(k, temp_vals.get(k, ""))
    blank()

    # ── [HWM.Fan] ────────────────────────────────────────────────────────────
    fan_base_order = ["FCPU", "FSYS", "FCPU2"]
    fan_sources = set(fan_vals.keys()) | set(sf_vals.keys())
    if probe_spec:
        fan_sources |= set(((probe_spec.get("hwm") or {}).get("fans") or []))
    fan_sources |= set(req.get("_spec_smartfan", []) or [])

    dyn_foem = sorted(
        [k for k in fan_sources if re.fullmatch(r"FOEM\d+", k or "")],
        key=lambda k: int(k[4:]),
    )
    extra_fan_keys = sorted(
        [k for k in fan_sources if k and k not in fan_base_order and not re.fullmatch(r"FOEM\d+", k)],
    )
    fan_order = fan_base_order + dyn_foem + extra_fan_keys

    section("HWM.Fan")
    comment("[Item]=[HW],[Channel],[IOPort],[option],[Pulses],[Name]")
    for k in fan_order:
        kv(k, fan_vals.get(k, ""))
    blank()

    # ── [HWM.Fan.Control] ────────────────────────────────────────────────────
    section("HWM.Fan.Control")
    comment("[Item]=[HW],[Channel],[IOPort],[option],[Name]")
    comment(" Option: (Flag mode)")
    comment("  0x00000001 = Default Output Mode \t- 0 = PWM, 1 = DC")
    comment("  0x00000008 = Set default Output Mode after boot (Dependency with flag 0x80000000)")
    comment("  0x00000010 = Default Output Type \t- 0 = Open Drain, 1 = Push-pull")
    comment("  0x00000080 = Set default Output Type after boot (Dependency with flag 0x80000000)")
    for k in fan_order:
        kv(k, sf_vals.get(k, ""))
    blank()

    # ── [HWM.CaseOpen] ───────────────────────────────────────────────────────
    section("HWM.CaseOpen")
    comment("[Item]=[HW],[Channel],[IOPort/Device Address],[option],[Name]")
    kv("CO0", case_vals.get("CO0", ""))
    kv("CO1", case_vals.get("CO1", ""))
    kv("CO2", case_vals.get("CO2", ""))
    blank()

    # ── [WDT] ────────────────────────────────────────────────────────────────
    section("WDT")
    comment("[Item]=[HW],[Channel],[IOPort],[option],[Name]")
    comment(" Option: (Flag mode)")
    comment("  0x00000001 = #KBRST\t\t\t-  0 = Disable.     1 = Enable ")
    comment("  0x00000008 = Sets output low pulse to #KBRST pin after boot (Dependency with flag 0x80000000)")
    if _is_active("WDT") and req.get("wdt") and wdt_hwid:
        kv("WDT1", f"{wdt_hwid},0x80000000,{wdt_ioport},{hx(wdt_opt)},")
    else:
        kv("WDT1")
    kv("WDT2")
    kv("WDT3")
    blank()

    # ── [GPIO] ───────────────────────────────────────────────────────────────
    section("GPIO")
    comment("[GPIO]=[HW],[IOBase],[IOPort/Device Address],[Option],[Group],[Bit],[Name]")
    comment("Option: (Flag mode)")
    comment("  0x00000001 = Support Input")
    comment("  0x00000002 = Support Output")
    comment("  0x00000010 = Default direction\t- 0 = output, 1 = input")
    comment("  0x00000080 = Set default direction after boot")
    comment("  0x00000100 = Default I/O State\t- 0 = Low, 1 = High")
    comment("  0x00000800 = Set default state after boot")
    comment("  0x00001000 = Default Polarity\t- 0 = Non-inverting, 1 = Inverting")
    comment("  0x00008000 = Set default polarity after boot (Dependency with flag 0x80000000)")
    comment("  0x00010000 = Default output type \t- 0 = Open Drain, 1 = Push-pull")
    comment("  0x00080000 = Set default output type after boot (Dependency with flag 0x80000000)")

    gpio_count = req.get("gpio_count", 0)
    gpio_pin_rows = req.get("gpio_pins", [])
    gpio_pin_dir = {p["pin"]: p["support"] for p in gpio_pin_rows if isinstance(p, dict) and "pin" in p}
    gpio_pin_meta = {p["pin"]: p for p in gpio_pin_rows if isinstance(p, dict) and "pin" in p}

    chip_local_rank: dict[str, int] = {}
    composite_gpio: dict[int, tuple[str, str, str, int, int, str]] = {}

    for i in range(gpio_count):
        meta = gpio_pin_meta.get(i, {})
        pin_chip = _cell(meta.get("chip", "")) or gpio_chip_raw
        prof = _gpio_profile(pin_chip) if (_has_per_pin_gpio_chip and pin_chip) else default_gpio_profile

        chip_key = prof.get("chip_key", "") or pin_chip or gpio_chip_raw or "<default>"
        rank = chip_local_rank.get(chip_key, 0)
        chip_local_rank[chip_key] = rank + 1

        gbase = prof.get("base_opt", 0)
        if gbase == 0 and _has_per_pin_gpio_chip:
            # Composite GPIO case: secondary GPIO-only chips (e.g. Intel BoardWell)
            # often still follow the board's main GPIO option base from EC chip.
            gbase = default_gpio_profile.get("base_opt", 0)
        gopt = hx(gbase | GPIO_IN | GPIO_OUT)

        grp, bit = 0, i
        loc_raw = _cell(meta.get("location", ""))
        if re.fullmatch(r"-?\d+", loc_raw):
            bit = int(loc_raw)
        else:
            pins_db = prof.get("pins_db", [])
            if rank < len(pins_db):
                grp, bit = pins_db[rank]
            elif i < len(pins_db):
                grp, bit = pins_db[i]

        pin_name = _cell(meta.get("name", ""))

        composite_gpio[i] = (
            prof.get("hwid", ""),
            prof.get("ioport", "0"),
            gopt,
            grp,
            bit,
            pin_name,
        )

    for i in range(128):
        if _is_active("GPIO") and i < gpio_count:
            pin_cfg = composite_gpio.get(i)
            if pin_cfg and pin_cfg[0]:
                phwid, pioport, pgopt, pgrp, pbit, pname = pin_cfg
                kv(f"GPIO{i:02d}", f"{phwid},0,{pioport},{pgopt},{pgrp},{pbit},{pname}")
            else:
                kv(f"GPIO{i:02d}")
        else:
            kv(f"GPIO{i:02d}")
    blank()

    # ── [StorageArea] ────────────────────────────────────────────────────────
    section("StorageArea")
    comment("[Item]=[HW],[Channel],[IOPort],[option],[Name]")
    if _is_active("Storage") and storage_req and storage_hwid:
        kv("Area0", f"{storage_hwid},0x80000000,0,{hx(OPT_BOTH)},")
    else:
        kv("Area0")
    kv("Area1")
    kv("Area2")
    blank()

    # ── [ThermalProtect] ─────────────────────────────────────────────────────
    section("ThermalProtect")
    comment("[Item]=[HW],[Channel],[IOPort],[option],[Name]")
    if _is_active("ThermalProtect") and thermal_req and thermal_hwid:
        for i in range(4):
            kv(f"TPCH{i}", f"{thermal_hwid},0x8000000{i},0,{hx(OPT_BOTH)},")
    else:
        for i in range(4):
            kv(f"TPCH{i}")

    # Per-section slim output: keep only [Information] + target section.
    if only_section is not None:
        lines = _filter_lines_for_section(lines, only_section)
        if not _section_has_nonempty_value(lines, only_section):
            return None

    out_ini.parent.mkdir(parents=True, exist_ok=True)
    with open(out_ini, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")

    return out_ini


def generate_split(root: Path, in_json: Path, out_ini: Path,
                   digit_json: Path | None = None,
                   probe_spec: dict | None = None) -> list[Path]:
    """Emit one INI per section in SPLIT_SECTIONS.

    Output files are named {name}_{section}.ini in the same directory as out_ini.
    Only [Information] + target section are kept.
    Files for sections with no non-empty values are skipped.
    """
    proj_dir = out_ini.parent
    name = out_ini.stem.replace("-pre", "")
    outputs = []
    for sec in SPLIT_SECTIONS:
        sec_out = proj_dir / f"{name}_{sec}.ini"
        result = generate_ini(root=root, in_json=in_json, out_ini=sec_out,
                              digit_json=digit_json, only_section=sec,
                              probe_spec=probe_spec)
        if result is not None:
            outputs.append(result)
    return outputs


def main():
    parser = argparse.ArgumentParser(description="Generate SUSI pre-INI from project JSON.")
    parser.add_argument("--project",  help="Project name: <project>.json -> <project>-pre.ini")
    parser.add_argument("--in-json",  help="Input JSON path (overrides --project default)")
    parser.add_argument("--out-ini",  help="Output INI path (overrides --project default)")
    parser.add_argument("--root",     default=".", help="Base directory for relative paths")
    parser.add_argument("--digit-json", help="Supplementary circuit diagram JSON (auto-discovered if omitted)")
    parser.add_argument("--split", action="store_true",
                        help="Emit section files {name}_{section}.ini (14 sections); skip sections with no data")
    parser.add_argument("--section", choices=SPLIT_SECTIONS,
                        help="Emit a single section file containing [Information] + this section")
    parser.add_argument("--probe", help="Probe report .txt — makes the probe [OK] list "
                                         "authoritative for which HWM channels are emitted")
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    in_json, out_ini = resolve_paths(args.project, args.in_json, args.out_ini, root)
    if args.section and not args.out_ini:
        name = out_ini.stem.replace("-pre", "")
        out_ini = out_ini.parent / f"{name}_{args.section}.ini"
    digit_json = Path(args.digit_json) if args.digit_json else None

    probe_spec = None
    if args.probe:
        from parse_probe import parse_probe_report
        probe_spec = parse_probe_report(Path(args.probe))

    if args.split:
        # Keep full {project}-pre.ini and emit section files in parallel workflow.
        full_result = generate_ini(root=root, in_json=in_json, out_ini=out_ini,
                                   digit_json=digit_json, only_section=None,
                                   probe_spec=probe_spec)
        if full_result is not None:
            print(f"Wrote: {full_result}")

        results = generate_split(root=root, in_json=in_json, out_ini=out_ini,
                                 digit_json=digit_json, probe_spec=probe_spec)
        for r in results:
            print(f"Wrote: {r}")
    else:
        result = generate_ini(root=root, in_json=in_json, out_ini=out_ini,
                              digit_json=digit_json, only_section=args.section,
                              probe_spec=probe_spec)
        if result is None and args.section:
            print(f"Skipped (no data): section={args.section}")
        elif result is not None:
            print(f"Wrote: {result}")


if __name__ == "__main__":
    main()
