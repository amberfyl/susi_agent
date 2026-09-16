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
    "HWM.Fan": "{model}_fan.json",
    "HWM.Fan.Control": "{model}_fancontrol.json",
    "WDT": "{model}_wdt.json",
}

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
            "enabled": False,
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
        # legacy compatibility for simple runner access
        "sample_count": sample_count,
        "sample_interval_ms": sample_interval_ms,
        "minimum_success_rate": min_success_rate,
        "require_stable_mask": require_stable_mask,
        "enforce_mask_match": enforce_mask_match,
        "supported_id": supported_id,
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
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")


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
    matrix_path = args.matrix.resolve()
    if not matrix_path.is_file():
        print(f"ERROR: missing matrix: {matrix_path}", file=sys.stderr)
        return 2

    try:
        matrix = read_json(matrix_path)
        model = str(args.model or matrix.get("project") or matrix_path.stem.replace("-section-matrix", ""))
        output_dir = (args.output_dir or matrix_path.parent).resolve()
        policy = read_json(args.policy.resolve()) if args.policy else {}
        results: list[str] = []
        errors: list[str] = []
        entries: dict[str, dict[str, Any] | None] = {
            name: matrix_section(matrix, name) for name in SECTION_OUTPUTS
        }
        fan_entry = entries["HWM.Fan"]
        for section_name, template in SECTION_OUTPUTS.items():
            entry = entries[section_name]
            output_path = output_dir / template.format(model=model)
            generated = bool(entry and str(entry.get("status", "")).upper() == "GENERATED")
            raw_path = entry.get("path") if entry else None

            if not generated or not raw_path:
                results.append(f"SKIP {section_name}: section not generated")
                if args.prune_stale and output_path.exists():
                    output_path.unlink()
                    results.append(f"REMOVE {output_path}")
                continue

            source_path = resolve_source_path(matrix_path, str(raw_path)).resolve()
            if not source_path.is_file():
                errors.append(f"{section_name}: missing source INI {source_path}")
                continue

            try:
                if section_name == "HWM.Fan":
                    minimum_fields = 6
                elif section_name == "HWM.Fan.Control":
                    minimum_fields = 5
                else:
                    minimum_fields = 4
                keys = read_ini_section(source_path, section_name, minimum_fields)
                if section_name == "HWM.Fan":
                    config = build_fan_config(model, source_path, keys, policy)
                elif section_name == "HWM.Fan.Control":
                    config = build_control_config(
                        model,
                        source_path,
                        keys,
                        policy,
                        entry or {},
                        fan_entry,
                        f"{model}_fan.json",
                    )
                elif section_name == "WDT":
                    config = build_wdt_config(model, source_path, keys, policy)
                else:
                    config = build_smbus_config(model, source_path, keys, policy)
                write_config(output_path, config)
                results.append(f"WRITE {output_path}")
            except (BuildError, KeyError, TypeError, ValueError) as exc:
                errors.append(f"{section_name}: {exc}")

        if errors:
            for message in errors:
                print(f"ERROR: {message}", file=sys.stderr)
            return 2

        for message in results:
            print(message)
        return 0
    except BuildError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
