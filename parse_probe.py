# -*- coding: utf-8 -*-
"""
SUSI board probe report -> structured probe spec (deterministic, no LLM).

Input : susi_board_probe_report.txt  (fetched from Machine B via fetch_probe.py)
Output: probe spec dict / JSON

Role in pipeline:
  - EC gate    : BOARD_EC_FW_STR [OK] => the board has an EC (decided 2026-07-13,
                 replaces the 2026-06-29 "HWM has values" rule — SUSI answers HWM
                 queries on SuperIO-only boards too, e.g. AIMB-286 returns 12 HWM
                 [OK] lines with BOARD_EC_FW_STR [ERR], so HWM presence cannot
                 distinguish EC from SuperIO; the EC firmware string can).
  - HWM source : the probe [OK] channel list is AUTHORITATIVE for *which* HWM
                 channels to emit. HWID / channel code still come from chip_db;
                 labels still come from the PDF. (decided 2026-06-29)

Probe does NOT cover Storage / ThermalProtect / VGA / WDT / GPIO / SmartFan;
those classes come from the PDF + chip_db, not from the probe.

susiID class encoding (high 16 bits), confirmed from a real report:
  0x0000 = board string info     0x0001 = board numeric / version
  0x0002 = HWM                   0x0003 = capability flags (SMBus/I2C)
HWM sub-class = bits [15:12] of the id:
  0 = temperature  1 = voltage  2 = fan  3 = current  4 = case-open
"""

import argparse
import json
import re
from pathlib import Path


# ---------- probe channel name -> ini_key (deterministic) ----------
# Keyed by the suffix after stripping the HWM_<KIND>_ prefix.
VOLT_KEY = {
    "VCORE": "VCORE", "VCORE2": "VCORE2", "2V5": "V25", "3V3": "V33",
    "5V": "V50", "12V": "V120", "5VSB": "V5SB", "3VSB": "V3SB", "VBAT": "VBAT",
    "5NV": "VN50", "12NV": "VN120", "VTT": "VTT", "24V": "V240",
    "DC": "DC", "DCSTBY": "DCSTBY", "VBATLI": "VBATLI",
    "1V05": "V105", "1V5": "V15", "1V8": "V18",
    "OEM0": "VOEM0", "OEM1": "VOEM1", "OEM2": "VOEM2", "OEM3": "VOEM3",
    "5VS5": "V5S5", "3V3S5": "V3S5",
}
TEMP_KEY = {
    "CPU": "TCPU", "CPU2": "TCPU2", "SYSTEM": "TSYS", "CHIPSET": "TCHIPSET",
    "OEM0": "TOEM0", "OEM1": "TOEM1", "OEM2": "TOEM2", "OEM3": "TOEM3",
    "OEM4": "TOEM4", "OEM5": "TOEM5", "OEM6": "TOEM6", "GRAPHIC": "GRAPHIC",
}
FAN_KEY = {
    "CPU": "FCPU", "SYSTEM": "FSYS", "CPU2": "FCPU2",
    "OEM0": "FOEM0", "OEM1": "FOEM1", "OEM2": "FOEM2", "OEM3": "FOEM3",
    "OEM4": "FOEM4", "OEM5": "FOEM5", "OEM6": "FOEM6",
}
_OEM_INDEX_RE = re.compile(r"^OEM(\d+)$")


def _map_fan_suffix(suffix: str) -> str | None:
    mapped = FAN_KEY.get(suffix)
    if mapped:
        return mapped
    m = _OEM_INDEX_RE.match(suffix)
    if m:
        return f"FOEM{int(m.group(1))}"
    return None


CURRENT_KEY = {"OEM0": "OEM0", "OEM1": "OEM1", "OEM2": "OEM2"}
CASEOPEN_KEY = {"OEM0": "CO0", "OEM1": "CO1", "OEM2": "CO2"}

# HWM sub-class (bits [15:12]) -> (kind, prefix, ini-key map, spec list name)
_HWM_SUBCLASS = {
    0x0: ("temperature", "HWM_TEMP_",     TEMP_KEY,     "temperatures"),
    0x1: ("voltage",     "HWM_VOLTAGE_",  VOLT_KEY,     "voltages"),
    0x2: ("fan",         "HWM_FAN_",      FAN_KEY,      "fans"),
    0x3: ("current",     "HWM_CURRENT_",  CURRENT_KEY,  "current"),
    0x4: ("caseopen",    "HWM_CASEOPEN_", CASEOPEN_KEY, "caseopen"),
}

_LINE = re.compile(
    r"^\[(?P<st>OK|ERR)\]\s+(?P<name>\S+)\s+Id=(?P<id>0x[0-9A-Fa-f]+)"
    r"(?:\s+(?:Value=(?P<val>.+?)|Status=\S+))?\s*$"
)


def _strip_value(raw: str) -> str:
    """Normalize a probe Value field: drop quotes and trailing units/decoded text."""
    if raw is None:
        return ""
    raw = raw.strip()
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    # e.g. '1136 mV', '3296 (=56.5 C)' -> keep the leading numeric token
    return raw


def parse_probe_report(path: Path) -> dict:
    """Parse a probe report into a structured spec.

    Returns:
      {
        "board_name": str,
        "platform_version": str,   # BOARD_PLATFORM_REV_VAL (raw value string)
        "bios_version": str,
        "is_ec": bool,             # True iff BOARD_EC_FW_STR was [OK]
        "ec_fw": str,              # EC firmware version string ("" when absent)
        "features": {"smbus": bool, "i2c": bool},
        "hwm": {                       # per sub-class: ordered, de-duped ini_keys
            "voltages": [...], "temperatures": [...], "fans": [...],
            "current": [...], "caseopen": [...],
        },
        "unmapped": [...],             # [OK] HWM channels with no ini_key mapping
      }
    """
    hwm = {name: [] for _, _, _, name in _HWM_SUBCLASS.values()}
    seen = {name: set() for name in hwm}
    unmapped = []
    board_name = platform_version = bios_version = ec_fw = ""
    ec_fw_ok = False
    smbus = i2c = False

    with open(path, "r", encoding="utf-8-sig") as f:
        for line in f:
            m = _LINE.match(line.rstrip("\n"))
            if not m:
                continue
            ok = m.group("st") == "OK"
            name = m.group("name")
            cid = int(m.group("id"), 16)
            val = _strip_value(m.group("val"))

            # board info (string IDs live in the 0x0000 class)
            if name == "BOARD_NAME_STR" and ok:
                board_name = val
                continue
            if name == "BOARD_BIOS_REVISION_STR" and ok:
                bios_version = val
                continue
            if name == "BOARD_PLATFORM_REV_VAL" and ok:
                platform_version = val
                continue
            if name == "BOARD_EC_FW_STR":
                ec_fw_ok = ok
                ec_fw = val if ok else ""
                continue

            high = cid >> 16
            if high == 0x0003:                       # capability flags
                if name == "SMBUS_SUPPORTED" and ok:
                    smbus = val not in ("0", "")
                elif name == "I2C_SUPPORTED" and ok:
                    i2c = val not in ("0", "")
                continue

            if high != 0x0002 or not ok:             # only mapped, responding HWM
                continue

            sub = (cid >> 12) & 0xF
            entry = _HWM_SUBCLASS.get(sub)
            if not entry:
                unmapped.append(name)
                continue
            _, prefix, key_map, list_name = entry
            suffix = name[len(prefix):] if name.startswith(prefix) else name
            ini_key = _map_fan_suffix(suffix) if sub == 0x2 else key_map.get(suffix)
            if not ini_key:
                unmapped.append(name)
                continue
            if ini_key not in seen[list_name]:
                seen[list_name].add(ini_key)
                hwm[list_name].append(ini_key)

    # EC gate: BOARD_EC_FW_STR responded => the board runs an EC.
    # HWM presence is NOT a valid EC signal: SUSI also answers HWM queries on
    # SuperIO-only boards (AIMB-286: 12 HWM [OK] but BOARD_EC_FW_STR [ERR]).
    is_ec = ec_fw_ok

    return {
        "board_name": board_name,
        "platform_version": platform_version,
        "bios_version": bios_version,
        "is_ec": is_ec,
        "ec_fw": ec_fw,
        "features": {"smbus": smbus, "i2c": i2c},
        "hwm": hwm,
        "unmapped": unmapped,
    }


def main():
    ap = argparse.ArgumentParser(description="Parse a SUSI board probe report into a probe spec.")
    ap.add_argument("report", help="Path to susi_board_probe_report.txt")
    ap.add_argument("--out", help="Write probe spec JSON here (default: stdout)")
    args = ap.parse_args()

    spec = parse_probe_report(Path(args.report))
    text = json.dumps(spec, ensure_ascii=False, indent=2)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"Wrote: {args.out}")
    else:
        print(text)


if __name__ == "__main__":
    main()
