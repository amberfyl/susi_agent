#!/usr/bin/env python3
"""Build independent Machine B dispatch configs from generated section INIs."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


SECTION_OUTPUTS = {
    "SMBus": "{model}_smbus.json",
    "I2C": "{model}_i2c.json",
    "GPIO": "{model}_gpio.json",
    "HWM.Temperature": "{model}_temperature.json",
    "HWM.Voltage": "{model}_voltage.json",
    "HWM.CaseOpen": "{model}_caseopen.json",
    "HWM.Current": "{model}_current.json",
    "HWM.Fan": "{model}_fan.json",
    "HWM.Fan.Control": "{model}_fancontrol.json",
    "StorageArea": "{model}_storage.json",
    "ThermalProtect": "{model}_thermalprotect.json",
    "WDT": "{model}_wdt.json",
    "VGA.Backlight": "{model}_backlight.json",
    "VGA.Brightness": "{model}_brightness.json",
}

# HWM.Temperature INI tuples use 0x80000000 + index as the configuration
# channel.  SusiBoardGetValue uses a different public API namespace.  This
# mapping mirrors targetB_task/susi_board_probe.ps1 (0x00020000 + index).
TEMPERATURE_API_INDEX = {
    "TCPU": 0,
    "TCHIPSET": 1,
    "TSYS": 2,
    "TCPU2": 3,
    "TOEM0": 4,
    "TOEM1": 5,
    "TOEM2": 6,
    "TOEM3": 7,
    "TOEM4": 8,
    "TOEM5": 9,
    "TSYS2": 10,
    "TGRAPHIC": 11,
}

# HWM.Voltage INI tuples use project/configuration channels, while
# SusiBoardGetValue uses the public voltage namespace 0x00021000 + index.
# The report/display name is authoritative for aliases such as V50/+5V and
# V120/+12V; the key is retained as a fallback for standard names.
VOLTAGE_API_INDEX = {
    "VCORE": 0, "VCORE2": 1, "2V5": 2, "3V3": 3,
    "5V": 4, "+5V": 4, "V50": 4,
    "12V": 5, "+12V": 5, "V120": 5,
    "5VSB": 6, "+5VSB": 6, "V5SB": 6,
    "3VSB": 7, "+3VSB": 7, "V3SB": 7,
    "VBAT": 8, "5NV": 9, "12NV": 10, "VTT": 11,
    "24V": 12, "DC": 13, "DCSTBY": 14, "VBATLI": 15,
    "OEM0": 16, "OEM1": 17, "OEM2": 18,
    "1V05": 19, "1V5": 20, "1V8": 21,
    "12VS5": 22, "5VS5": 23, "3V3S5": 24,
}

# HWM.Current INI tuples use 0x80000000 + index as configuration channels,
# while SusiBoardGetValue uses the public current namespace 0x00023000 + index.
CURRENT_API_INDEX = {"OEM0": 0, "OEM1": 1, "OEM2": 2}
CASEOPEN_API_INDEX = {"CO0": 0, "CO1": 1, "CO2": 2}
THERMALPROTECT_API_INDEX = {
    "TPCH0": 0,
    "TPCH1": 1,
    "TPCH2": 2,
    "TPCH3": 3,
}
STORAGE_API_INDEX = {f"AREA{i}": i for i in range(12)}
BACKLIGHT_API_INDEX = {f"BACKLIGHT{i}": i - 1 for i in range(1, 5)}
BRIGHTNESS_API_INDEX = {f"BRIGHTNESS{i}": i - 1 for i in range(1, 5)}

DEFAULTS = {
    "fan": {
        "read_check": {
            "sample_count": 30,
            "sample_interval_ms": 1000,
            "min_success_rate": 0.95,
            "min_rpm": 1,
            "max_rpm": 10000,
        },
        "stimulus": {
            "settle_time_sec": 10,
            "expected_delta_rpm_min": 200,
        },
        "profiles": {
            # bringup: only primary channel is fixture-required by default.
            # formal: all channels are fixture-required.
            "primary_channel_candidates": ["FCPU", "CPU", "CPU_FAN"],
        },
    },
    "control": {
        "control_check": {
            "enabled": True,
            "sequence_pwm": [30, 50, 70],
            "settle_time_sec": 10,
            "set_get_tolerance": 2,
            "rpm_sample_count": 6,
            "rpm_sample_interval_ms": 1000,
            "expected_direction": "up",
            "expected_delta_rpm_min": 200,
        },
    },
    "smbus": {
        "capability_check": {
            "supported_id": "0x00030000",
            "sample_count": 5,
            "sample_interval_ms": 200,
            "min_success_rate": 1.0,
            "require_stable_mask": True,
            "enforce_mask_match": True,
        },
    },
    "i2c": {
        "capability_check": {
            "supported_id": "0x00030100",
            "sample_count": 5,
            "sample_interval_ms": 200,
            "min_success_rate": 1.0,
            "require_stable_mask": True,
            "enforce_mask_match": True,
        },
        "frequency_check": {
            "min_khz": 1,
            "max_khz": 1000,
            "require_api_success": False,
        },
        "caps_check": {
            "maximum_block_length_item_id": "0x00000000",
            "require_api_success": False,
        },
    },
    "temperature": {
        "read_check": {
            "sample_count": 10,
            "sample_interval_ms": 1000,
            "min_success_rate": 0.9,
            "min_celsius": -40.0,
            "max_celsius": 125.0,
            "max_span_celsius": 30.0,
        },
    },
    "current": {
        "read_check": {
            "sample_count": 10,
            "sample_interval_ms": 500,
            "min_success_rate": 0.9,
            "min_milliamps": 0.0,
            "max_milliamps": 100000.0,
            "max_span_milliamps": 10000.0,
        },
    },
    "caseopen": {
        "read_check": {
            "sample_count": 10,
            "sample_interval_ms": 500,
            "min_success_rate": 0.9,
            "allowed_values": [0, 1],
        },
    },
    "storage": {
        "read_check": {
            "offset": 0,
            "length": 16,
            "sample_count": 1,
            "min_success_rate": 1.0,
        },
        "write_check": {
            "enabled": True,
            "verify": True,
            "restore_original": True,
            "pattern_hex": "A5",
        },
    },
    "wdt": {
        "capability_check": {
            "sample_count": 3,
            "sample_interval_ms": 200,
            "min_success_rate": 1.0,
        },
        "nondestructive_check": {
            "enabled": True,
            "test_timeout_sec": 30,
            "refresh_interval_sec": 5,
            "refresh_cycles": 3,
            "stop_wait_margin_sec": 5,
        },
        # SAFETY: keep reboot/power-cycle WDT tests disabled until an approved
        # recovery harness is available. The runner also has an independent gate.
        "destructive_check": {
            "enabled": False,
            "allow_destructive_reset": False,
            "pending_result_when_disabled": "PENDING",
            "pending_reason_when_disabled": "Destructive reboot-required WDT checks are pending by policy",
        },
    },
}


class BuildError(Exception):
    """Raised when a generated section cannot be converted safely."""


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BuildError(f"cannot read JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise BuildError(f"JSON root must be an object: {path}")
    return value


def read_ini_section(path: Path, section_name: str, minimum_fields: int) -> list[str]:
    parser = configparser.ConfigParser(
        interpolation=None,
        strict=True,
        empty_lines_in_values=False,
    )
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise BuildError(f"cannot read INI {path}: {exc}") from exc

    matching = [name for name in parser.sections() if name.lower() == section_name.lower()]
    if len(matching) != 1:
        raise BuildError(f"{path} must contain [{section_name}]")

    section = parser[matching[0]]
    keys = []
    for key, value in section.items():
        key = key.strip()
        value = value.strip()
        if not key or not value:
            continue
        if len(value.split(",")) < minimum_fields:
            raise BuildError(
                f"[{section_name}]{key} in {path} has fewer than {minimum_fields} tuple fields"
            )
        keys.append(key)
    if not keys:
        raise BuildError(f"[{section_name}] in {path} has no non-empty entries")
    if len(keys) != len(set(key.lower() for key in keys)):
        raise BuildError(f"[{section_name}] in {path} has duplicate keys")
    return keys


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise BuildError(f"cannot hash INI {path}: {exc}") from exc
    return digest.hexdigest()


def matrix_section(matrix: dict[str, Any], section_name: str) -> dict[str, Any] | None:
    sections = matrix.get("sections")
    if not isinstance(sections, list):
        raise BuildError("section matrix must contain a sections list")
    for entry in sections:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("section", "")).lower() == section_name.lower():
            return entry
    return None


def resolve_source_path(matrix_path: Path, raw_path: str) -> Path:
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return candidate
    return matrix_path.parent / candidate


def source_metadata(path: Path, section_name: str) -> dict[str, Any]:
    return {
        "file": path.name,
        "path": str(path),
        "section": section_name,
        "sha256": sha256(path),
    }


def policy_section(policy: dict[str, Any], name: str) -> dict[str, Any]:
    value = policy.get(name, {})
    return value if isinstance(value, dict) else {}


def policy_value(
    values: dict[str, Any], name: str, default: Any, *fallbacks: tuple[dict[str, Any], str]
) -> Any:
    if name in values:
        return values[name]
    for source, source_name in fallbacks:
        if source_name in source:
            return source[source_name]
    return default


def parse_int_auto(text: str) -> int:
    s = text.strip()
    if not s:
        raise BuildError("empty numeric field")
    return int(s, 16) if s.lower().startswith("0x") else int(s, 10)


def _resolve_primary_fan_channel(keys: list[str]) -> str | None:
    candidates = [str(x).upper() for x in DEFAULTS["fan"]["profiles"]["primary_channel_candidates"]]
    upper_to_original = {k.upper(): k for k in keys}
    for candidate in candidates:
        if candidate in upper_to_original:
            return upper_to_original[candidate]
    return keys[0] if keys else None


def _build_fan_channel_policies(keys: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    primary = _resolve_primary_fan_channel(keys)
    bringup_required = [primary] if primary else []
    formal_required = list(keys)

    channel_policy: dict[str, Any] = {}
    for key in keys:
        channel_policy[key] = {
            "tach_required": True,
            "rpm_zero_policy": {
                "bringup": "CONDITIONAL_NO_FIXTURE",
                "formal": "FAIL_FUNCTIONAL",
            },
            "notes": "If tach API is readable and mapping is valid, 0 RPM can be treated as no-fan fixture in bringup profile.",
        }

    profiles = {
        "bringup": {
            "fixture_required_channels": bringup_required,
            "optional_channels": [k for k in keys if k not in bringup_required],
            "missing_fixture_result": "CONDITIONAL_NO_FIXTURE",
        },
        "formal": {
            "fixture_required_channels": formal_required,
            "optional_channels": [],
            "missing_fixture_result": "FAIL_FUNCTIONAL",
        },
    }
    return channel_policy, profiles


def build_fan_config(
    model: str,
    path: Path,
    keys: list[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    sampling = policy_section(policy, "sampling")
    fan_defaults = DEFAULTS["fan"]
    read_defaults = fan_defaults["read_check"]
    stimulus_defaults = fan_defaults["stimulus"]

    sample_count = int(policy_value(sampling, "baseline_count", read_defaults["sample_count"]))
    sample_interval_ms = int(
        policy_value(sampling, "sample_interval_ms", read_defaults["sample_interval_ms"])
    )
    min_success_rate = float(
        policy_value(sampling, "minimum_success_rate", read_defaults["min_success_rate"])
    )
    max_rpm = int(
        policy_value(sampling, "maximum_plausible_rpm", read_defaults["max_rpm"])
    )
    min_rpm = int(policy_value(sampling, "minimum_plausible_rpm", read_defaults["min_rpm"]))
    settle_time_sec = int(
        policy_value(sampling, "settle_time_sec", stimulus_defaults["settle_time_sec"])
    )
    expected_delta = int(
        policy_value(
            sampling,
            "expected_delta_rpm_min",
            stimulus_defaults["expected_delta_rpm_min"],
        )
    )

    channel_policy, profiles = _build_fan_channel_policies(keys)

    return {
        "schema_version": "1.2",
        "category": "HWM.Fan",
        "model": model,
        "source_ini": source_metadata(path, "HWM.Fan"),
        "required_channels": keys,
        "read_check": {
            "sample_count": sample_count,
            "sample_interval_ms": sample_interval_ms,
            "min_success_rate": min_success_rate,
            "min_rpm": min_rpm,
            "max_rpm": max_rpm,
        },
        "stimulus_check": {
            "settle_time_sec": settle_time_sec,
            "expected_delta_rpm_min": expected_delta,
        },
        "channel_policy": channel_policy,
        "profiles": profiles,
        "default_profile": "bringup",
        "result_semantics": {
            "tach_mapping_ok_but_fixture_missing": "CONDITIONAL_NO_FIXTURE",
            "tach_mapping_or_api_failure": "FAIL_API",
            "formal_fixture_missing": "FAIL_FUNCTIONAL",
        },
        "verdict_policy": {
            "sw_verdict": {
                "pass_when_layers": ["L1_configuration", "L2_capability", "L3_api", "L4_readback"],
                "fail_on": ["FAIL*"]
            },
            "dqa_verdict": {
                "layers": ["L5_functional", "L6_recovery"],
                "na_states": ["N_A", "NOT_REQUIRED"],
                "pending_states": ["PENDING", "CONDITIONAL*"]
            },
            "ci_exit_code": {
                "FAIL_SW": 1,
                "PASS_SW": 0
            }
        },
        "safety": {
            "hardware_write": False,
        },
        # legacy compatibility for existing runners
        "sample_count": sample_count,
        "sample_interval_ms": sample_interval_ms,
        "settle_time_sec": settle_time_sec,
        "expected_delta_rpm_min": expected_delta,
        "maximum_plausible_rpm": max_rpm,
        "minimum_success_rate": min_success_rate,
    }


def build_control_config(
    model: str,
    path: Path,
    keys: list[str],
    policy: dict[str, Any],
    control_entry: dict[str, Any],
    fan_entry: dict[str, Any] | None,
    fan_json_name: str,
) -> dict[str, Any]:
    control_policy = policy_section(policy, "control_policy")
    defaults = DEFAULTS["control"]["control_check"]
    fan_available = bool(
        fan_entry
        and str(fan_entry.get("status", "")).upper() == "GENERATED"
        and fan_entry.get("path")
    )
    fan_ini_path = None
    fan_keys: list[str] = []
    if fan_available:
        fan_ini_path = Path(str(fan_entry["path"]))
        fan_keys = read_ini_section(fan_ini_path, "HWM.Fan", 6)

    channel_map = {
        control_key: control_key
        for control_key in keys
        if any(control_key.lower() == fan_key.lower() for fan_key in fan_keys)
    }
    missing = [key for key in keys if key not in channel_map]
    matrix_pairing_status = str(control_entry.get("fan_pairing_status") or "")
    dependency_status = "READY" if not missing and fan_available else "BLOCKED"
    dependency = {
        "status": dependency_status,
        "config_file": fan_json_name if fan_available else None,
        "ini_file": fan_ini_path.name if fan_ini_path is not None else None,
        "section": "HWM.Fan",
        "channel_map": channel_map,
        "missing_control_channels": missing,
        "mapping_source": (
            matrix_pairing_status
            or "EXACT_SECTION_KEY_MATCH"
            if channel_map
            else "NO_EXPLICIT_FAN_KEY_MATCH"
        ),
    }

    sequence_pwm = [
        int(value)
        for value in control_policy.get("sequence", defaults["sequence_pwm"])
    ]
    enabled = bool(control_policy.get("enabled", defaults["enabled"]))
    settle_time_sec = int(control_policy.get("settle_time_sec", defaults["settle_time_sec"]))
    set_get_tolerance = float(
        control_policy.get("set_get_tolerance", defaults["set_get_tolerance"])
    )
    rpm_sample_count = int(
        control_policy.get("rpm_sample_count", defaults["rpm_sample_count"])
    )
    rpm_sample_interval_ms = int(
        control_policy.get("rpm_sample_interval_ms", defaults["rpm_sample_interval_ms"])
    )
    expected_direction = str(
        control_policy.get("expected_direction", defaults["expected_direction"])
    )
    expected_delta_rpm_min = control_policy.get(
        "expected_delta_rpm_min", defaults["expected_delta_rpm_min"]
    )

    return {
        "schema_version": "1.1",
        "category": "HWM.Fan.Control",
        "model": model,
        "source_ini": source_metadata(path, "HWM.Fan.Control"),
        "control_channels": keys,
        "control_check": {
            "enabled": enabled,
            "sequence_pwm": sequence_pwm,
            "settle_time_sec": settle_time_sec,
            "set_get_tolerance": set_get_tolerance,
            "rpm_sample_count": rpm_sample_count,
            "rpm_sample_interval_ms": rpm_sample_interval_ms,
            "expected_direction": expected_direction,
            "expected_delta_rpm_min": expected_delta_rpm_min,
        },
        "rpm_dependency": dependency,
        "verdict_policy": {
            "sw_verdict": {
                "pass_when_layers": ["L1_configuration", "L2_capability", "L3_api", "L4_readback"],
                "fail_on": ["FAIL*"]
            },
            "dqa_verdict": {
                "layers": ["L5_functional", "L6_recovery"],
                "na_states": ["N_A", "NOT_REQUIRED"],
                "pending_states": ["PENDING", "CONDITIONAL*"]
            },
            "ci_exit_code": {
                "FAIL_SW": 1,
                "PASS_SW": 0
            }
        },
        "safety": {
            "requires_explicit_allow_control": True,
            "restore_required": True,
            "abort_on_restore_failure": True,
        },
        # legacy compatibility for existing runners
        "test_sequence": sequence_pwm,
        "enabled": enabled,
        "settle_time_sec": settle_time_sec,
        "set_get_tolerance": set_get_tolerance,
        "rpm_sample_count": rpm_sample_count,
        "rpm_sample_interval_ms": rpm_sample_interval_ms,
        "expected_direction": expected_direction,
        "expected_delta_rpm_min": expected_delta_rpm_min,
    }


def build_temperature_config(
    model: str,
    path: Path,
    keys: list[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    read_policy = policy_section(policy, "temperature_sampling")
    defaults = DEFAULTS["temperature"]["read_check"]

    parser = configparser.ConfigParser(
        interpolation=None,
        strict=True,
        empty_lines_in_values=False,
    )
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise BuildError(f"cannot read INI {path}: {exc}") from exc

    section_name = next(
        (name for name in parser.sections() if name.lower() == "hwm.temperature"),
        None,
    )
    if section_name is None:
        raise BuildError(f"{path} must contain [HWM.Temperature]")

    channels: dict[str, Any] = {}
    for key in keys:
        raw = parser[section_name].get(key, "").strip()
        fields = [item.strip() for item in raw.split(",")]
        if len(fields) < 4:
            raise BuildError(
                f"[HWM.Temperature]{key} in {path} has fewer than 4 tuple fields"
            )
        normalized_key = key.strip().upper()
        if normalized_key not in TEMPERATURE_API_INDEX:
            raise BuildError(
                f"[HWM.Temperature]{key} has no canonical SUSI Board API mapping"
            )
        tuple_channel = parse_int_auto(fields[1])
        api_id = 0x00020000 + TEMPERATURE_API_INDEX[normalized_key]
        channels[key] = {
            "raw_tuple": raw,
            "tuple_fields": fields,
            "tuple_channel": f"0x{tuple_channel:08X}",
            "api_id": f"0x{api_id:08X}",
            "api_id_source": "SUSI_HWM_TEMPERATURE_NAMESPACE",
            "decode": "kelvin_x10_to_celsius",
            "display_name": fields[5].strip('"') if len(fields) >= 6 else key,
        }

    sample_count = int(policy_value(read_policy, "sample_count", defaults["sample_count"]))
    sample_interval_ms = int(
        policy_value(
            read_policy,
            "sample_interval_ms",
            defaults["sample_interval_ms"],
        )
    )
    min_success_rate = float(
        policy_value(
            read_policy,
            "min_success_rate",
            defaults["min_success_rate"],
        )
    )
    min_celsius = float(policy_value(read_policy, "min_celsius", defaults["min_celsius"]))
    max_celsius = float(policy_value(read_policy, "max_celsius", defaults["max_celsius"]))
    max_span_celsius = float(
        policy_value(read_policy, "max_span_celsius", defaults["max_span_celsius"])
    )

    return {
        "schema_version": "1.0",
        "category": "HWM.Temperature",
        "model": model,
        "source_ini": source_metadata(path, "HWM.Temperature"),
        "required_channels": keys,
        "channels": channels,
        "read_check": {
            "sample_count": sample_count,
            "sample_interval_ms": sample_interval_ms,
            "min_success_rate": min_success_rate,
            "min_celsius": min_celsius,
            "max_celsius": max_celsius,
            "max_span_celsius": max_span_celsius,
        },
        "result_semantics": {
            "api_or_mapping_failure": "FAIL_API",
            "readback_out_of_range": "FAIL_READBACK",
            "fixture_missing": "CONDITIONAL",
        },
        "verdict_policy": {
            "sw_verdict": {
                "pass_when_layers": ["L1_configuration", "L2_capability", "L3_api", "L4_readback"],
                "fail_on": ["FAIL*"]
            },
            "dqa_verdict": {
                "layers": ["L5_functional", "L6_recovery"],
                "na_states": ["N_A", "NOT_REQUIRED"],
                "pending_states": ["PENDING", "CONDITIONAL*"]
            },
            "ci_exit_code": {
                "FAIL_SW": 1,
                "PASS_SW": 0
            }
        },
        # legacy compatibility for simple runner access
        "sample_count": sample_count,
        "sample_interval_ms": sample_interval_ms,
        "minimum_success_rate": min_success_rate,
        "min_celsius": min_celsius,
        "max_celsius": max_celsius,
        "max_span_celsius": max_span_celsius,
    }


def build_voltage_config(
    model: str,
    path: Path,
    keys: list[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    read_policy = policy_section(policy, "voltage_sampling")
    defaults = {
        "sample_count": 10,
        "sample_interval_ms": 500,
        "min_success_rate": 0.9,
        "min_millivolts": 0.0,
        "max_millivolts": 30000.0,
        "max_span_millivolts": 5000.0,
    }
    parser = configparser.ConfigParser(interpolation=None, strict=True, empty_lines_in_values=False)
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise BuildError(f"cannot read INI {path}: {exc}") from exc

    section_name = next((name for name in parser.sections() if name.lower() == "hwm.voltage"), None)
    if section_name is None:
        raise BuildError(f"{path} must contain [HWM.Voltage]")

    channels: dict[str, Any] = {}
    for key in keys:
        raw = parser[section_name].get(key, "").strip()
        fields = [item.strip() for item in raw.split(",")]
        if len(fields) < 4:
            raise BuildError(f"[HWM.Voltage]{key} in {path} has fewer than 4 tuple fields")
        report_name = fields[6].strip('"') if len(fields) >= 7 else key
        report_norm = report_name.upper().replace(" ", "")
        key_norm = key.strip().upper()
        api_index = VOLTAGE_API_INDEX.get(report_norm)
        if api_index is None:
            api_index = VOLTAGE_API_INDEX.get(key_norm)
        if api_index is None:
            raise BuildError(f"[HWM.Voltage]{key} has no canonical SUSI Board API mapping (report_name={report_name})")
        tuple_channel = parse_int_auto(fields[1])
        api_id = 0x00021000 + api_index
        channels[key] = {
            "raw_tuple": raw,
            "tuple_fields": fields,
            "tuple_channel": f"0x{tuple_channel:08X}",
            "api_id": f"0x{api_id:08X}",
            "api_id_source": "SUSI_HWM_VOLTAGE_NAMESPACE",
            "decode": "millivolts",
            "display_name": report_name,
        }

    sample_count = int(policy_value(read_policy, "sample_count", defaults["sample_count"]))
    sample_interval_ms = int(policy_value(read_policy, "sample_interval_ms", defaults["sample_interval_ms"]))
    min_success_rate = float(policy_value(read_policy, "min_success_rate", defaults["min_success_rate"]))
    min_millivolts = float(policy_value(read_policy, "min_millivolts", defaults["min_millivolts"]))
    max_millivolts = float(policy_value(read_policy, "max_millivolts", defaults["max_millivolts"]))
    max_span_millivolts = float(policy_value(read_policy, "max_span_millivolts", defaults["max_span_millivolts"]))

    return {
        "schema_version": "1.0",
        "category": "HWM.Voltage",
        "model": model,
        "source_ini": source_metadata(path, "HWM.Voltage"),
        "required_channels": keys,
        "channels": channels,
        "read_check": {
            "sample_count": sample_count,
            "sample_interval_ms": sample_interval_ms,
            "min_success_rate": min_success_rate,
            "min_millivolts": min_millivolts,
            "max_millivolts": max_millivolts,
            "max_span_millivolts": max_span_millivolts,
        },
        "result_semantics": {
            "api_or_mapping_failure": "FAIL_API",
            "readback_out_of_range": "FAIL_READBACK",
        },
        "verdict_policy": {
            "sw_verdict": {
                "pass_when_layers": ["L1_configuration", "L2_capability", "L3_api", "L4_readback"],
                "fail_on": ["FAIL*"]
            },
            "dqa_verdict": {
                "layers": ["L5_functional", "L6_recovery"],
                "na_states": ["N_A", "NOT_REQUIRED"],
                "pending_states": ["PENDING", "CONDITIONAL*"]
            },
            "ci_exit_code": {"FAIL_SW": 1, "PASS_SW": 0}
        },
        "sample_count": sample_count,
        "sample_interval_ms": sample_interval_ms,
        "minimum_success_rate": min_success_rate,
        "min_millivolts": min_millivolts,
        "max_millivolts": max_millivolts,
        "max_span_millivolts": max_span_millivolts,
    }


def build_current_config(
    model: str,
    path: Path,
    keys: list[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    read_policy = policy_section(policy, "current_sampling")
    defaults = DEFAULTS["current"]["read_check"]
    parser = configparser.ConfigParser(interpolation=None, strict=True, empty_lines_in_values=False)
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise BuildError(f"cannot read INI {path}: {exc}") from exc
    section_name = next((name for name in parser.sections() if name.lower() == "hwm.current"), None)
    if section_name is None:
        raise BuildError(f"{path} must contain [HWM.Current]")
    channels: dict[str, Any] = {}
    for key in keys:
        raw = parser[section_name].get(key, "").strip()
        fields = [item.strip() for item in raw.split(",")]
        if len(fields) < 4:
            raise BuildError(f"[HWM.Current]{key} in {path} has fewer than 4 tuple fields")
        normalized_key = key.strip().upper()
        if normalized_key not in CURRENT_API_INDEX:
            raise BuildError(f"[HWM.Current]{key} has no canonical SUSI Board API mapping")
        tuple_channel = parse_int_auto(fields[1])
        api_id = 0x00023000 + CURRENT_API_INDEX[normalized_key]
        channels[key] = {
            "raw_tuple": raw,
            "tuple_fields": fields,
            "tuple_channel": f"0x{tuple_channel:08X}",
            "api_id": f"0x{api_id:08X}",
            "api_id_source": "SUSI_HWM_CURRENT_NAMESPACE",
            "decode": "milliamps",
            "display_name": "",
        }
    values = {}
    for name in ("sample_count", "sample_interval_ms", "min_success_rate", "min_milliamps", "max_milliamps", "max_span_milliamps"):
        values[name] = policy_value(read_policy, name, defaults[name])
    values["sample_count"] = int(values["sample_count"])
    values["sample_interval_ms"] = int(values["sample_interval_ms"])
    values["min_success_rate"] = float(values["min_success_rate"])
    for name in ("min_milliamps", "max_milliamps", "max_span_milliamps"):
        values[name] = float(values[name])
    return {
        "schema_version": "1.0",
        "category": "HWM.Current",
        "model": model,
        "source_ini": source_metadata(path, "HWM.Current"),
        "required_channels": keys,
        "channels": channels,
        "read_check": values,
        "result_semantics": {"api_or_mapping_failure": "FAIL_API", "readback_out_of_range": "FAIL_READBACK"},
        "verdict_policy": {
            "sw_verdict": {"pass_when_layers": ["L1_configuration", "L2_capability", "L3_api", "L4_readback"], "fail_on": ["FAIL*"]},
            "dqa_verdict": {"layers": ["L5_functional", "L6_recovery"], "na_states": ["N_A", "NOT_REQUIRED"], "pending_states": ["PENDING", "CONDITIONAL*"]},
            "ci_exit_code": {"FAIL_SW": 1, "PASS_SW": 0},
        },
        **values,
        "minimum_success_rate": values["min_success_rate"],
    }


def build_thermalprotect_config(
    model: str,
    path: Path,
    keys: list[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    parser = configparser.ConfigParser(interpolation=None, strict=True, empty_lines_in_values=False)
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise BuildError(f"cannot read INI {path}: {exc}") from exc
    section_name = next((name for name in parser.sections() if name.lower() == "thermalprotect"), None)
    if section_name is None:
        raise BuildError(f"{path} must contain [ThermalProtect]")

    channels: dict[str, Any] = {}
    for key in keys:
        raw = parser[section_name].get(key, "").strip()
        fields = [item.strip() for item in raw.split(",")]
        if len(fields) < 4:
            raise BuildError(f"[ThermalProtect]{key} in {path} has fewer than 4 tuple fields")
        normalized_key = key.strip().upper()
        if normalized_key not in THERMALPROTECT_API_INDEX:
            raise BuildError(f"[ThermalProtect]{key} has no canonical SUSI Thermal API mapping")
        tuple_channel = parse_int_auto(fields[1])
        thermal_id = THERMALPROTECT_API_INDEX[normalized_key]
        channels[key] = {
            "raw_tuple": raw,
            "tuple_fields": fields,
            "tuple_channel": f"0x{tuple_channel:08X}",
            "thermal_api_id": f"0x{thermal_id:08X}",
            "thermal_api_id_name": f"SUSI_ID_THERMAL_PROTECT_{thermal_id + 1}",
            "api_id_source": "SUSI_THERMAL_PROTECT_CHANNEL_ID",
            "config_source_id_type": "SUSI_HWM_TEMPERATURE_ID",
        }

    return {
        "schema_version": "1.0",
        "category": "ThermalProtect",
        "model": model,
        "source_ini": source_metadata(path, "ThermalProtect"),
        "required_channels": keys,
        "channels": channels,
        "capability_check": {
            "item_ids": {
                "support_flags": "0x00000000",
                "trigger_maximum": "0x00000001",
                "trigger_minimum": "0x00000002",
                "clear_maximum": "0x00000003",
                "clear_minimum": "0x00000004",
            },
            "read_only": True,
        },
        "config_check": {
            "read_only": True,
            "temperature_unit": "0.1_kelvins",
            "allowed_event_types": {
                "shutdown": "0x00000000",
                "throttle": "0x00000001",
                "poweroff": "0x00000002",
                "none": "0x000000FF",
            },
        },
        "result_semantics": {
            "api_or_mapping_failure": "FAIL_API",
            "invalid_config": "FAIL_READBACK",
            "functional_stimulus": "CONDITIONAL",
        },
        "verdict_policy": {
            "sw_verdict": {
                "pass_when_layers": ["L1_configuration", "L2_capability", "L3_api", "L4_readback"],
                "fail_on": ["FAIL*"]
            },
            "dqa_verdict": {
                "layers": ["L5_functional", "L6_recovery"],
                "na_states": ["N_A", "NOT_REQUIRED"],
                "pending_states": ["PENDING", "CONDITIONAL*"]
            },
            "ci_exit_code": {"FAIL_SW": 1, "PASS_SW": 0},
        },
    }


def build_caseopen_config(
    model: str,
    path: Path,
    keys: list[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    read_policy = policy_section(policy, "caseopen_sampling")
    defaults = DEFAULTS["caseopen"]["read_check"]
    parser = configparser.ConfigParser(interpolation=None, strict=True, empty_lines_in_values=False)
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise BuildError(f"cannot read INI {path}: {exc}") from exc

    section_name = next((name for name in parser.sections() if name.lower() == "hwm.caseopen"), None)
    if section_name is None:
        raise BuildError(f"{path} must contain [HWM.CaseOpen]")

    channels: dict[str, Any] = {}
    for key in keys:
        raw = parser[section_name].get(key, "").strip()
        fields = [item.strip() for item in raw.split(",")]
        if len(fields) < 4:
            raise BuildError(f"[HWM.CaseOpen]{key} in {path} has fewer than 4 tuple fields")
        normalized_key = key.strip().upper()
        if normalized_key not in CASEOPEN_API_INDEX:
            raise BuildError(f"[HWM.CaseOpen]{key} has no canonical SUSI Board API mapping")
        tuple_channel = parse_int_auto(fields[1])
        api_id = 0x00024000 + CASEOPEN_API_INDEX[normalized_key]
        channels[key] = {
            "raw_tuple": raw,
            "tuple_fields": fields,
            "tuple_channel": f"0x{tuple_channel:08X}",
            "api_id": f"0x{api_id:08X}",
            "api_id_source": "SUSI_HWM_CASEOPEN_NAMESPACE",
            "decode": "boolean_u32",
            "display_name": key,
        }

    sample_count = int(policy_value(read_policy, "sample_count", defaults["sample_count"]))
    sample_interval_ms = int(policy_value(read_policy, "sample_interval_ms", defaults["sample_interval_ms"]))
    min_success_rate = float(policy_value(read_policy, "min_success_rate", defaults["min_success_rate"]))
    allowed_values = list(policy_value(read_policy, "allowed_values", defaults["allowed_values"]))
    return {
        "schema_version": "1.0",
        "category": "HWM.CaseOpen",
        "model": model,
        "source_ini": source_metadata(path, "HWM.CaseOpen"),
        "required_channels": keys,
        "channels": channels,
        "read_check": {
            "sample_count": sample_count,
            "sample_interval_ms": sample_interval_ms,
            "min_success_rate": min_success_rate,
            "allowed_values": allowed_values,
        },
        "result_semantics": {
            "api_or_mapping_failure": "FAIL_API",
            "readback_invalid_value": "FAIL_READBACK",
            "fixture_missing": "CONDITIONAL",
        },
        "verdict_policy": {
            "sw_verdict": {
                "pass_when_layers": ["L1_configuration", "L2_capability", "L3_api", "L4_readback"],
                "fail_on": ["FAIL*"]
            },
            "dqa_verdict": {
                "layers": ["L5_functional", "L6_recovery"],
                "na_states": ["N_A", "NOT_REQUIRED"],
                "pending_states": ["PENDING", "CONDITIONAL*"]
            },
            "ci_exit_code": {"FAIL_SW": 1, "PASS_SW": 0}
        },
        "sample_count": sample_count,
        "sample_interval_ms": sample_interval_ms,
        "minimum_success_rate": min_success_rate,
        "allowed_values": allowed_values,
    }


def build_storage_config(
    model: str,
    path: Path,
    keys: list[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    read_policy = policy_section(policy, "storage_read")
    write_policy = policy_section(policy, "storage_write")
    defaults_read = DEFAULTS["storage"]["read_check"]
    defaults_write = DEFAULTS["storage"]["write_check"]
    parser = configparser.ConfigParser(interpolation=None, strict=True, empty_lines_in_values=False)
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise BuildError(f"cannot read INI {path}: {exc}") from exc
    section_name = next((name for name in parser.sections() if name.lower() == "storagearea"), None)
    if section_name is None:
        raise BuildError(f"{path} must contain [StorageArea]")

    channels: dict[str, Any] = {}
    for key in keys:
        raw = parser[section_name].get(key, "").strip()
        fields = [item.strip() for item in raw.split(",")]
        if len(fields) < 4:
            raise BuildError(f"[StorageArea]{key} in {path} has fewer than 4 tuple fields")
        normalized_key = key.strip().upper()
        if normalized_key not in STORAGE_API_INDEX:
            raise BuildError(f"[StorageArea]{key} has no canonical SUSI Storage API mapping")
        tuple_channel = parse_int_auto(fields[1])
        storage_id = STORAGE_API_INDEX[normalized_key]
        channels[key] = {
            "raw_tuple": raw,
            "tuple_fields": fields,
            "tuple_channel": f"0x{tuple_channel:08X}",
            "storage_api_id": f"0x{storage_id:08X}",
            "storage_api_id_name": "SUSI_ID_STORAGE_STD" if storage_id == 0 else f"SUSI_ID_STORAGE_OEM{storage_id - 1}",
            "api_id_source": "SUSI_STORAGE_AREA_ID",
        }

    offset = int(policy_value(read_policy, "offset", defaults_read["offset"]))
    length = int(policy_value(read_policy, "length", defaults_read["length"]))
    sample_count = int(policy_value(read_policy, "sample_count", defaults_read["sample_count"]))
    min_success_rate = float(policy_value(read_policy, "min_success_rate", defaults_read["min_success_rate"]))
    if offset < 0 or length <= 0 or sample_count <= 0:
        raise BuildError("StorageArea read policy requires offset >= 0, length > 0, sample_count > 0")

    return {
        "schema_version": "1.0",
        "category": "StorageArea",
        "model": model,
        "source_ini": source_metadata(path, "StorageArea"),
        "required_channels": keys,
        "channels": channels,
        "capability_check": {
            "item_ids": {
                "total_size": "0x00000000",
                "block_size": "0x00000001",
                "lock_status": "0x00010000",
                "password_max_length": "0x00010001",
            },
        },
        "read_check": {
            "offset": offset,
            "length": length,
            "sample_count": sample_count,
            "min_success_rate": min_success_rate,
        },
        "write_check": {
            "enabled": bool(policy_value(write_policy, "enabled", defaults_write["enabled"])),
            "verify": bool(policy_value(write_policy, "verify", defaults_write["verify"])),
            "restore_original": bool(policy_value(write_policy, "restore_original", defaults_write["restore_original"])),
            "pattern_hex": str(policy_value(write_policy, "pattern_hex", defaults_write["pattern_hex"])),
        },
        "result_semantics": {
            "capability_or_mapping_failure": "FAIL_API",
            "read_failure": "FAIL_READBACK",
            "write_not_requested": "CONDITIONAL",
        },
        "verdict_policy": {
            "sw_verdict": {
                "pass_when_layers": ["L1_configuration", "L2_capability", "L3_api", "L4_readback"],
                "fail_on": ["FAIL*"]
            },
            "dqa_verdict": {
                "layers": ["L5_functional", "L6_recovery"],
                "na_states": ["N_A", "NOT_REQUIRED"],
                "pending_states": ["PENDING", "CONDITIONAL*"]
            },
            "ci_exit_code": {"FAIL_SW": 1, "PASS_SW": 0}
        },
    }


def build_gpio_config(
    model: str,
    path: Path,
    keys: list[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    """Build a GPIO bank/mask contract from the generated section INI."""
    parser = configparser.ConfigParser(interpolation=None, strict=True, empty_lines_in_values=False)
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise BuildError(f"cannot read INI {path}: {exc}") from exc
    section_name = next((name for name in parser.sections() if name.lower() == "gpio"), None)
    if section_name is None:
        raise BuildError(f"{path} must contain [GPIO]")

    channels: dict[str, Any] = {}
    bank_masks: dict[int, int] = {}
    for key in keys:
        normalized_key = key.strip().upper()
        suffix = normalized_key[4:] if normalized_key.startswith("GPIO") else ""
        if not suffix.isdigit():
            raise BuildError(f"[GPIO]{key} must use GPIO<number> naming")
        gpio_id = int(suffix, 10)
        if gpio_id > 127:
            raise BuildError(f"[GPIO]{key} public ID must be in range 0..127")

        raw = parser[section_name].get(key, "").strip()
        fields = [item.strip() for item in raw.split(",")]
        if len(fields) < 6:
            raise BuildError(f"[GPIO]{key} in {path} has fewer than 6 tuple fields")
        tuple_group = parse_int_auto(fields[4])
        tuple_pin = parse_int_auto(fields[5])
        bank_no = gpio_id >> 5
        bank_bitmask = 1 << (gpio_id & 0x1F)
        bank_masks[bank_no] = bank_masks.get(bank_no, 0) | bank_bitmask
        channels[key] = {
            "raw_tuple": raw,
            "tuple_fields": fields,
            "tuple_group": tuple_group,
            "tuple_pin": tuple_pin,
            "gpio_api_id": f"0x{gpio_id:08X}",
            "bank": bank_no,
            "bank_id": f"0x{0x00010000 + bank_no:08X}",
            "bank_bitmask": f"0x{bank_bitmask:08X}",
            "api_id_source": "GPIO_KEY_SUFFIX",
        }

    functional = policy_section(policy, "gpio_functional")
    banks = {
        f"Bank{bank_no}": {
            "bank_number": bank_no,
            "bank_id": f"0x{0x00010000 + bank_no:08X}",
            "expected_mask": f"0x{mask:08X}",
        }
        for bank_no, mask in sorted(bank_masks.items())
    }
    return {
        "schema_version": "1.0",
        "category": "GPIO",
        "model": model,
        "source_ini": source_metadata(path, "GPIO"),
        "required_channels": keys,
        "channels": channels,
        "banks": banks,
        "capability_items": {
            "input_support": "0x00000000",
            "output_support": "0x00000001",
        },
        "functional_check": {
            "enabled": bool(policy_value(functional, "enabled", True)),
            "patterns": ["0x00000000", "EXPECTED_MASK"],
            "settle_time_ms": int(policy_value(functional, "settle_time_ms", 100)),
            "restore_original": True,
        },
        "safety": {
            "requires_explicit_functional_switch": True,
            "restore_original_level_and_direction": True,
        },
        "result_semantics": {
            "capability_or_read_failure": "FAIL_API",
            "functional_not_requested": "CONDITIONAL",
            "set_readback_or_restore_failure": "FAIL_FUNCTIONAL",
        },
    }


def build_brightness_config(
    model: str,
    path: Path,
    keys: list[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    parser = configparser.ConfigParser(interpolation=None, strict=True, empty_lines_in_values=False)
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise BuildError(f"cannot read JSON/INI {path}: {exc}") from exc
    section_name = next((name for name in parser.sections() if name.lower() == "vga.brightness"), None)
    if section_name is None:
        raise BuildError(f"{path} must contain [VGA.Brightness]")
    channels: dict[str, Any] = {}
    for key in keys:
        raw = parser[section_name].get(key, "").strip()
        fields = [item.strip() for item in raw.split(",")]
        if len(fields) < 5:
            raise BuildError(f"[VGA.Brightness]{key} in {path} has fewer than 5 tuple fields")
        normalized_key = key.strip().upper()
        if normalized_key not in BRIGHTNESS_API_INDEX:
            raise BuildError(f"[VGA.Brightness]{key} has no canonical SUSI Brightness API mapping")
        tuple_channel = parse_int_auto(fields[1])
        api_id = BRIGHTNESS_API_INDEX[normalized_key]
        min_value = parse_int_auto(fields[4]) if fields[4] else 0
        channels[key] = {
            "raw_tuple": raw,
            "tuple_fields": fields,
            "tuple_channel": f"0x{tuple_channel:08X}",
            "brightness_api_id": f"0x{api_id:08X}",
            "api_id": f"0x{api_id:08X}",
            "api_id_source": "SUSI_ID_BACKLIGHT_N",
            "configured_max": min_value,
        }
    functional = policy_section(policy, "brightness_functional")
    return {
        "schema_version": "1.0",
        "category": "VGA.Brightness",
        "model": model,
        "source_ini": source_metadata(path, "VGA.Brightness"),
        "required_channels": keys,
        "channels": channels,
        "read_check": {"min_brightness": 0, "max_brightness": 100},
        "functional_check": {
            "enabled": bool(policy_value(functional, "enabled", True)),
            "test_value": int(policy_value(functional, "test_value", 0)),
            "verify": bool(policy_value(functional, "verify", True)),
            "restore_original": bool(policy_value(functional, "restore_original", True)),
        },
        "result_semantics": {
            "api_or_mapping_failure": "FAIL_API",
            "set_or_restore_failure": "FAIL_FUNCTIONAL",
            "functional_not_requested": "CONDITIONAL",
        },
        "safety": {"reversible_set": True, "restore_original": True},
    }


def build_backlight_config(
    model: str,
    path: Path,
    keys: list[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    parser = configparser.ConfigParser(interpolation=None, strict=True, empty_lines_in_values=False)
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise BuildError(f"cannot read INI {path}: {exc}") from exc
    section_name = next((name for name in parser.sections() if name.lower() == "vga.backlight"), None)
    if section_name is None:
        raise BuildError(f"{path} must contain [VGA.Backlight]")

    channels: dict[str, Any] = {}
    for key in keys:
        raw = parser[section_name].get(key, "").strip()
        fields = [item.strip() for item in raw.split(",")]
        if len(fields) < 4:
            raise BuildError(f"[VGA.Backlight]{key} in {path} has fewer than 4 tuple fields")
        normalized_key = key.strip().upper()
        if normalized_key not in BACKLIGHT_API_INDEX:
            raise BuildError(f"[VGA.Backlight]{key} has no canonical SUSI Backlight API mapping")
        tuple_channel = parse_int_auto(fields[1])
        api_id = BACKLIGHT_API_INDEX[normalized_key]
        channels[key] = {
            "raw_tuple": raw,
            "tuple_fields": fields,
            "tuple_channel": f"0x{tuple_channel:08X}",
            "backlight_api_id": f"0x{api_id:08X}",
            "api_id": f"0x{api_id:08X}",
            "api_id_source": "SUSI_ID_BACKLIGHT_N",
        }

    functional = policy_section(policy, "backlight_functional")
    return {
        "schema_version": "1.0",
        "category": "VGA.Backlight",
        "model": model,
        "source_ini": source_metadata(path, "VGA.Backlight"),
        "required_channels": keys,
        "channels": channels,
        "functional_check": {
            "enabled": bool(policy_value(functional, "enabled", True)),
            "toggle_to": int(policy_value(functional, "toggle_to", 0)),
            "verify": bool(policy_value(functional, "verify", True)),
            "restore_original": bool(policy_value(functional, "restore_original", True)),
        },
        "result_semantics": {
            "api_or_mapping_failure": "FAIL_API",
            "toggle_or_restore_failure": "FAIL_FUNCTIONAL",
            "functional_not_requested": "CONDITIONAL",
        },
        "verdict_policy": {
            "sw_verdict": {
                "pass_when_layers": ["L1_configuration", "L2_capability", "L3_api", "L4_readback"],
                "fail_on": ["FAIL*"]
            },
            "dqa_verdict": {
                "layers": ["L5_functional", "L6_recovery"],
                "na_states": ["N_A", "NOT_REQUIRED"],
                "pending_states": ["PENDING", "CONDITIONAL*"]
            },
            "ci_exit_code": {"FAIL_SW": 1, "PASS_SW": 0}
        },
        "safety": {
            "reversible_toggle": True,
            "restore_original": True,
        },
    }


def build_smbus_config(
    model: str,
    path: Path,
    keys: list[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    capability_policy = policy_section(policy, "capability")
    defaults = DEFAULTS["smbus"]["capability_check"]
    channel_map: dict[str, Any] = {}

    parser = configparser.ConfigParser(
        interpolation=None,
        strict=True,
        empty_lines_in_values=False,
    )
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise BuildError(f"cannot read INI {path}: {exc}") from exc

    section_name = next(
        (name for name in parser.sections() if name.lower() == "smbus"),
        None,
    )
    if section_name is None:
        raise BuildError(f"{path} must contain [SMBus]")

    for key in keys:
        raw = parser[section_name].get(key, "").strip()
        fields = [item.strip() for item in raw.split(",")]
        if len(fields) < 4:
            raise BuildError(f"[SMBus]{key} in {path} has fewer than 4 tuple fields")
        # SUSI SMBus tuples encode the channel as 0x80000000 + idx;
        # Machine-B capability checks need the zero-based idx rather than
        # the encoded channel value. Keep the original tuple unchanged.
        encoded_channel = parse_int_auto(fields[1])
        bus_index = encoded_channel - 0x80000000 if encoded_channel >= 0x80000000 else encoded_channel
        if bus_index < 0 or bus_index > 31:
            raise BuildError(f"[SMBus]{key} bus index must be in [0,31], got {bus_index}")
        channel_map[key] = {
            "raw_tuple": raw,
            "tuple_fields": fields,
            "encoded_channel": encoded_channel,
            "bus_index": bus_index,
            "capability_bit": bus_index,
        }

    sample_count = int(policy_value(capability_policy, "sample_count", defaults["sample_count"]))
    sample_interval_ms = int(
        policy_value(
            capability_policy,
            "sample_interval_ms",
            defaults["sample_interval_ms"],
        )
    )
    min_success_rate = float(
        policy_value(
            capability_policy,
            "min_success_rate",
            defaults["min_success_rate"],
        )
    )
    require_stable_mask = bool(
        policy_value(
            capability_policy,
            "require_stable_mask",
            defaults["require_stable_mask"],
        )
    )
    enforce_mask_match = bool(
        policy_value(
            capability_policy,
            "enforce_mask_match",
            defaults["enforce_mask_match"],
        )
    )
    supported_id = str(policy_value(capability_policy, "supported_id", defaults["supported_id"]))

    return {
        "schema_version": "1.0",
        "category": "SMBus",
        "model": model,
        "source_ini": source_metadata(path, "SMBus"),
        "required_channels": keys,
        "channels": channel_map,
        "capability_check": {
            "supported_id": supported_id,
            "sample_count": sample_count,
            "sample_interval_ms": sample_interval_ms,
            "min_success_rate": min_success_rate,
            "require_stable_mask": require_stable_mask,
            "enforce_mask_match": enforce_mask_match,
        },
        "transaction_policy": {
            "read_only": True,
            "require_fixture_for_transaction": True,
            "blocked_result_without_fixture": "CONDITIONAL",
        },
        "expected_without_fixture": {
            "result": "CONDITIONAL",
            "reason": "BLOCKED_FIXTURE: no approved SMBus slave/register contract for transaction validation",
        },
        "verdict_policy": {
            "sw_verdict": {
                "pass_when_layers": ["L1_configuration", "L2_capability", "L3_api", "L4_readback"],
                "fail_on": ["FAIL*"]
            },
            "dqa_verdict": {
                "layers": ["L5_functional", "L6_recovery"],
                "na_states": ["N_A", "NOT_REQUIRED"],
                "pending_states": ["PENDING", "CONDITIONAL*"]
            },
            "ci_exit_code": {
                "FAIL_SW": 1,
                "PASS_SW": 0
            }
        },
        # legacy compatibility for simple runner access
        "sample_count": sample_count,
        "sample_interval_ms": sample_interval_ms,
        "minimum_success_rate": min_success_rate,
        "require_stable_mask": require_stable_mask,
        "enforce_mask_match": enforce_mask_match,
        "supported_id": supported_id,
    }


def build_i2c_config(
    model: str,
    path: Path,
    keys: list[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    capability_policy = policy_section(policy, "i2c_capability")
    frequency_policy = policy_section(policy, "i2c_frequency")
    caps_policy = policy_section(policy, "i2c_caps")
    capability_defaults = DEFAULTS["i2c"]["capability_check"]
    frequency_defaults = DEFAULTS["i2c"]["frequency_check"]
    caps_defaults = DEFAULTS["i2c"]["caps_check"]

    parser = configparser.ConfigParser(
        interpolation=None,
        strict=True,
        empty_lines_in_values=False,
    )
    parser.optionxform = str
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise BuildError(f"cannot read INI {path}: {exc}") from exc

    section_name = next((name for name in parser.sections() if name.lower() == "i2c"), None)
    if section_name is None:
        raise BuildError(f"{path} must contain [I2C]")

    channels: dict[str, Any] = {}
    used_api_ids: set[int] = set()
    for key in keys:
        raw = parser[section_name].get(key, "").strip()
        fields = [item.strip() for item in raw.split(",")]
        if len(fields) < 4:
            raise BuildError(f"[I2C]{key} in {path} has fewer than 4 tuple fields")
        encoded_channel = parse_int_auto(fields[1])
        api_id = encoded_channel - 0x80000000 if encoded_channel >= 0x80000000 else encoded_channel
        if api_id < 0 or api_id > 31:
            raise BuildError(f"[I2C]{key} public API ID must be in [0,31], got {api_id}")
        if api_id in used_api_ids:
            raise BuildError(f"[I2C] duplicate public API ID {api_id} at {key}")
        used_api_ids.add(api_id)
        channels[key] = {
            "raw_tuple": raw,
            "tuple_fields": fields,
            "encoded_channel": f"0x{encoded_channel:08X}",
            "i2c_api_id": f"0x{api_id:08X}",
            "i2c_api_id_value": api_id,
            "capability_bit": api_id,
        }

    sample_count = int(policy_value(capability_policy, "sample_count", capability_defaults["sample_count"]))
    sample_interval_ms = int(policy_value(capability_policy, "sample_interval_ms", capability_defaults["sample_interval_ms"]))
    min_success_rate = float(policy_value(capability_policy, "min_success_rate", capability_defaults["min_success_rate"]))
    require_stable_mask = bool(policy_value(capability_policy, "require_stable_mask", capability_defaults["require_stable_mask"]))
    enforce_mask_match = bool(policy_value(capability_policy, "enforce_mask_match", capability_defaults["enforce_mask_match"]))
    supported_id = str(policy_value(capability_policy, "supported_id", capability_defaults["supported_id"]))

    return {
        "schema_version": "1.0",
        "category": "I2C",
        "model": model,
        "source_ini": source_metadata(path, "I2C"),
        "required_channels": keys,
        "channels": channels,
        "capability_check": {
            "supported_id": supported_id,
            "sample_count": sample_count,
            "sample_interval_ms": sample_interval_ms,
            "min_success_rate": min_success_rate,
            "require_stable_mask": require_stable_mask,
            "enforce_mask_match": enforce_mask_match,
        },
        "frequency_check": {
            "min_khz": int(policy_value(frequency_policy, "min_khz", frequency_defaults["min_khz"])),
            "max_khz": int(policy_value(frequency_policy, "max_khz", frequency_defaults["max_khz"])),
            "require_api_success": bool(policy_value(frequency_policy, "require_api_success", frequency_defaults["require_api_success"])),
        },
        "caps_check": {
            "maximum_block_length_item_id": str(policy_value(caps_policy, "maximum_block_length_item_id", caps_defaults["maximum_block_length_item_id"])),
            "require_api_success": bool(policy_value(caps_policy, "require_api_success", caps_defaults["require_api_success"])),
        },
        "transaction_policy": {
            "enabled": False,
            "read_only": True,
            "require_fixture_for_transaction": True,
            "legacy_fixture_addresses_encoded": ["0xAC", "0xAE"],
            "legacy_fixture_addresses_7bit": ["0x56", "0x57"],
        },
        "expected_without_fixture": {
            "result": "CONDITIONAL",
            "sw_verdict": "PASS_SW",
            "dqa_verdict": "PENDING_DQA",
            "reason": "I2C transaction validation requires an approved slave/fixture contract",
        },
        "verdict_policy": {
            "sw_verdict": {
                "pass_when_layers": ["L1_configuration", "L2_capability", "L3_api", "L4_readback"],
                "fail_on": ["FAIL*"],
            },
            "dqa_verdict": {
                "layers": ["L5_functional", "L6_recovery"],
                "na_states": ["N_A", "NOT_REQUIRED"],
                "pending_states": ["PENDING", "CONDITIONAL*"],
            },
            "ci_exit_code": {"FAIL_SW": 1, "PASS_SW": 0},
        },
    }


def build_wdt_config(
    model: str,
    path: Path,
    keys: list[str],
    policy: dict[str, Any],
) -> dict[str, Any]:
    cap_policy = policy_section(policy, "wdt_capability")
    nondestructive_policy = policy_section(policy, "wdt_nondestructive")
    destructive_policy = policy_section(policy, "wdt_destructive")

    defaults_cap = DEFAULTS["wdt"]["capability_check"]
    defaults_non = DEFAULTS["wdt"]["nondestructive_check"]
    defaults_des = DEFAULTS["wdt"]["destructive_check"]

    sample_count = int(policy_value(cap_policy, "sample_count", defaults_cap["sample_count"]))
    sample_interval_ms = int(
        policy_value(cap_policy, "sample_interval_ms", defaults_cap["sample_interval_ms"])
    )
    min_success_rate = float(
        policy_value(cap_policy, "min_success_rate", defaults_cap["min_success_rate"])
    )

    nondestructive_enabled = bool(
        policy_value(nondestructive_policy, "enabled", defaults_non["enabled"])
    )
    test_timeout_sec = int(
        policy_value(
            nondestructive_policy,
            "test_timeout_sec",
            defaults_non["test_timeout_sec"],
        )
    )
    refresh_interval_sec = int(
        policy_value(
            nondestructive_policy,
            "refresh_interval_sec",
            defaults_non["refresh_interval_sec"],
        )
    )
    refresh_cycles = int(
        policy_value(nondestructive_policy, "refresh_cycles", defaults_non["refresh_cycles"])
    )
    stop_wait_margin_sec = int(
        policy_value(
            nondestructive_policy,
            "stop_wait_margin_sec",
            defaults_non["stop_wait_margin_sec"],
        )
    )

    destructive_enabled = bool(
        policy_value(destructive_policy, "enabled", defaults_des["enabled"])
    )
    allow_destructive_reset = bool(
        policy_value(
            destructive_policy,
            "allow_destructive_reset",
            defaults_des["allow_destructive_reset"],
        )
    )
    pending_result_when_disabled = str(
        policy_value(
            destructive_policy,
            "pending_result_when_disabled",
            defaults_des["pending_result_when_disabled"],
        )
    )
    pending_reason_when_disabled = str(
        policy_value(
            destructive_policy,
            "pending_reason_when_disabled",
            defaults_des["pending_reason_when_disabled"],
        )
    )

    return {
        "schema_version": "1.0",
        "category": "WDT",
        "model": model,
        "source_ini": source_metadata(path, "WDT"),
        "required_channels": keys,
        "capability_check": {
            "sample_count": sample_count,
            "sample_interval_ms": sample_interval_ms,
            "min_success_rate": min_success_rate,
        },
        "nondestructive_check": {
            "enabled": nondestructive_enabled,
            "test_timeout_sec": test_timeout_sec,
            "refresh_interval_sec": refresh_interval_sec,
            "refresh_cycles": refresh_cycles,
            "stop_wait_margin_sec": stop_wait_margin_sec,
        },
        "destructive_check": {
            "enabled": destructive_enabled,
            "allow_destructive_reset": allow_destructive_reset,
            "pending_result_when_disabled": pending_result_when_disabled,
            "pending_reason_when_disabled": pending_reason_when_disabled,
        },
        "result_semantics": {
            "nonreboot_cases_pass_but_destructive_skipped": "CONDITIONAL",
            "destructive_skipped_breakdown_bucket": "pending",
            "api_or_capability_failure": "FAIL_API",
            "functional_failure": "FAIL_FUNCTIONAL",
        },
        "verdict_policy": {
            "sw_verdict": {
                "pass_when_layers": ["L1_configuration", "L2_capability", "L3_api", "L4_readback"],
                "fail_on": ["FAIL*"]
            },
            "dqa_verdict": {
                "layers": ["L5_functional", "L6_recovery"],
                "na_states": ["N_A", "NOT_REQUIRED"],
                "pending_states": ["PENDING", "CONDITIONAL*"]
            },
            "ci_exit_code": {
                "FAIL_SW": 1,
                "PASS_SW": 0
            }
        },
        "safety": {
            "allow_destructive_reset": allow_destructive_reset,
            "max_reset_attempts": 1,
            "abort_on_checkpoint_mismatch": True,
        },
        # legacy compatibility for simple runner implementation
        "sample_count": sample_count,
        "sample_interval_ms": sample_interval_ms,
        "minimum_success_rate": min_success_rate,
        "test_timeout_sec": test_timeout_sec,
        "refresh_interval_sec": refresh_interval_sec,
        "refresh_cycles": refresh_cycles,
        "allow_destructive_reset": allow_destructive_reset,
    }


def write_config(path: Path, config: dict[str, Any]) -> None:
    """Atomically write one generated Machine-B section config."""

    temporary_path = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path.write_text(
            json.dumps(config, indent=2, ensure_ascii=True) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(path)
    except OSError as exc:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise BuildError(f"cannot write config {path}: {exc}") from exc


def _build_config_for_section(
    *,
    section_name: str,
    model: str,
    source_path: Path,
    keys: list[str],
    policy: dict[str, Any],
    entry: dict[str, Any],
    fan_entry: dict[str, Any] | None,
) -> dict[str, Any]:
    if section_name == "HWM.Fan":
        return build_fan_config(model, source_path, keys, policy)
    if section_name == "HWM.Fan.Control":
        return build_control_config(
            model,
            source_path,
            keys,
            policy,
            entry,
            fan_entry,
            f"{model}_fan.json",
        )
    if section_name == "HWM.Temperature":
        return build_temperature_config(model, source_path, keys, policy)
    if section_name == "HWM.Voltage":
        return build_voltage_config(model, source_path, keys, policy)
    if section_name == "HWM.CaseOpen":
        return build_caseopen_config(model, source_path, keys, policy)
    if section_name == "HWM.Current":
        return build_current_config(model, source_path, keys, policy)
    if section_name == "ThermalProtect":
        return build_thermalprotect_config(model, source_path, keys, policy)
    if section_name == "StorageArea":
        return build_storage_config(model, source_path, keys, policy)
    if section_name == "GPIO":
        return build_gpio_config(model, source_path, keys, policy)
    if section_name == "I2C":
        return build_i2c_config(model, source_path, keys, policy)
    if section_name == "WDT":
        return build_wdt_config(model, source_path, keys, policy)
    if section_name == "VGA.Backlight":
        return build_backlight_config(model, source_path, keys, policy)
    if section_name == "VGA.Brightness":
        return build_brightness_config(model, source_path, keys, policy)
    return build_smbus_config(model, source_path, keys, policy)


def build_section_configs(
    *,
    matrix_path: str | Path,
    output_dir: str | Path | None = None,
    policy_path: str | Path | None = None,
    model: str | None = None,
    prune_stale: bool = False,
) -> dict[str, Any]:
    """Build all GENERATED section configs through a callable Python API.

    Config objects are fully constructed before any output is changed. This
    prevents a source/configuration failure from leaving a partially refreshed
    config set for the orchestrator to consume.
    """

    resolved_matrix = Path(matrix_path).expanduser().resolve()
    if not resolved_matrix.is_file():
        raise BuildError(f"missing matrix: {resolved_matrix}")

    matrix = read_json(resolved_matrix)
    resolved_model = str(
        model
        or matrix.get("project")
        or resolved_matrix.stem.replace("-section-matrix", "")
    )
    resolved_output_dir = Path(output_dir or resolved_matrix.parent).expanduser().resolve()
    policy = (
        read_json(Path(policy_path).expanduser().resolve()) if policy_path else {}
    )
    entries: dict[str, dict[str, Any] | None] = {
        name: matrix_section(matrix, name) for name in SECTION_OUTPUTS
    }
    fan_entry = entries["HWM.Fan"]
    pending_configs: list[tuple[str, Path, dict[str, Any]]] = []
    skipped_sections: list[str] = []
    stale_paths: list[Path] = []
    errors: list[str] = []

    for section_name, template in SECTION_OUTPUTS.items():
        entry = entries[section_name]
        output_path = resolved_output_dir / template.format(model=resolved_model)
        generated = bool(
            entry and str(entry.get("status", "")).upper() == "GENERATED"
        )
        raw_path = entry.get("path") if entry else None

        if not generated or not raw_path:
            skipped_sections.append(section_name)
            if prune_stale and output_path.exists():
                stale_paths.append(output_path)
            continue

        source_path = resolve_source_path(resolved_matrix, str(raw_path)).resolve()
        if not source_path.is_file():
            errors.append(f"{section_name}: missing source INI {source_path}")
            continue

        try:
            if section_name in ("HWM.Fan", "GPIO"):
                minimum_fields = 6
            elif section_name == "HWM.Fan.Control":
                minimum_fields = 5
            else:
                minimum_fields = 4
            keys = read_ini_section(source_path, section_name, minimum_fields)
            config = _build_config_for_section(
                section_name=section_name,
                model=resolved_model,
                source_path=source_path,
                keys=keys,
                policy=policy,
                entry=entry or {},
                fan_entry=fan_entry,
            )
            pending_configs.append((section_name, output_path, config))
        except (BuildError, KeyError, TypeError, ValueError) as exc:
            errors.append(f"{section_name}: {exc}")

    if errors:
        raise BuildError("; ".join(errors))

    for section_name, output_path, config in pending_configs:
        write_config(output_path, config)
    removed_stale_configs: list[str] = []
    for stale_path in stale_paths:
        try:
            stale_path.unlink()
        except OSError as exc:
            raise BuildError(f"cannot remove stale config {stale_path}: {exc}") from exc
        removed_stale_configs.append(str(stale_path))

    config_paths = {
        section_name: str(output_path)
        for section_name, output_path, _ in pending_configs
    }
    return {
        "status": "PASS",
        "model": resolved_model,
        "matrix_path": str(resolved_matrix),
        "output_dir": str(resolved_output_dir),
        "generated_sections": list(config_paths),
        "config_paths": config_paths,
        "skipped_sections": skipped_sections,
        "removed_stale_configs": removed_stale_configs,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Build independent section JSON dispatch files for Machine-B validators."
    )
    parser.add_argument("--matrix", type=Path, required=True, help="section-matrix.json path")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="output directory (defaults to the matrix directory)",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        default=None,
        help="optional legacy combined policy used only for test-policy values",
    )
    parser.add_argument("--model", default=None, help="override the model name")
    parser.add_argument(
        "--prune-stale",
        action="store_true",
        help="remove known output files for sections that are not generated",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = build_section_configs(
            matrix_path=args.matrix,
            output_dir=args.output_dir,
            policy_path=args.policy,
            model=args.model,
            prune_stale=args.prune_stale,
        )
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    for section_name, output_path in result["config_paths"].items():
        print(f"WRITE {output_path}")
    for section_name in result["skipped_sections"]:
        print(f"SKIP {section_name}: section not generated")
    for stale_path in result["removed_stale_configs"]:
        print(f"REMOVE {stale_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
