#!/usr/bin/env python3
"""Deterministic helpers for Machine-B route fallback convergence."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping


FALLBACK_REGISTRY_SCHEMA_VERSION = "machineb.fallback_candidate_registry.v1"
FALLBACK_PLAN_SCHEMA_VERSION = "machineb.fallback_plan.v1"
PROJECT_OVERRIDE_SCHEMA_VERSION = "machineb.project_route_overrides.v1"
GPIO_TRIGGER_CODE = "EXPECTED_GPIO_ROUTE_PROBES_ALL_FAILED"
# A GPIO route candidate is correct when GetCaps/GetDirection/GetLevel succeed
# on every bank and more than half of the expected pins are supported. Missing
# pins are a pin-trace problem reported per GPIO, not a wrong route.
GPIO_SUCCESS_CONDITION = "GPIO_CAPS_READS_OK_AND_MAJORITY_PINS_SUPPORTED"
DEFAULT_TRIGGER_CODE = "EXPECTED_SECTION_ALL_CHANNEL_API_FAILED"
DEFAULT_SUCCESS_CONDITION = "ANY_CHANNEL_API_SUCCEEDED"

FALLBACK_PLAN_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": FALLBACK_PLAN_SCHEMA_VERSION,
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "project", "run_id", "baseline", "sections"],
    "properties": {
        "schema_version": {"const": FALLBACK_PLAN_SCHEMA_VERSION},
        "project": {"type": "string", "minLength": 1},
        "run_id": {"type": "string", "minLength": 1},
        "baseline": {"type": "object"},
        "sections": {"type": "array"},
    },
}


class FallbackRegistryError(ValueError):
    """Raised when the fallback candidate registry violates its contract."""


class FallbackPlanError(ValueError):
    """Raised when an AI-selected fallback plan violates deterministic policy."""


class FallbackMutationError(ValueError):
    """Raised when a candidate INI cannot be safely isolated to one section."""


class FallbackOverrideError(ValueError):
    """Raised when a persisted project route override is invalid or stale."""


@dataclass(frozen=True)
class FallbackSectionCandidates:
    section: str
    route_field: str
    route_candidates: tuple[str, ...]
    option_candidates: tuple[str, ...]
    route_fallback_enabled: bool
    option_fallback_enabled: bool


@dataclass(frozen=True)
class FallbackCandidateRegistry:
    schema_version: str
    sections: Mapping[str, FallbackSectionCandidates]


@dataclass(frozen=True)
class FallbackTriggerDecision:
    eligible: bool
    code: str
    reason: str
    passed_channels: tuple[str, ...]
    failed_channels: tuple[str, ...]


@dataclass(frozen=True)
class IniRouteMutation:
    text: str
    section: str
    route_value: str
    changed_keys: tuple[str, ...]
    before_routes: Mapping[str, str]


@dataclass(frozen=True)
class FallbackAttemptCandidate:
    index: int
    section: str
    route_value: str
    ini_text: str
    ini_sha256: str
    changed_keys: tuple[str, ...]
    before_routes: Mapping[str, str]


def _parse_uint32(value: object, *, field: str) -> int:
    text = str(value).strip()
    if not text:
        raise FallbackRegistryError(f"{field} must not be empty")
    try:
        parsed = int(text, 0)
    except ValueError as exc:
        raise FallbackRegistryError(f"{field} is not an integer: {text}") from exc
    if parsed < 0 or parsed > 0xFFFFFFFF:
        raise FallbackRegistryError(f"{field} is outside UInt32 range: {text}")
    return parsed


def _load_candidate_values(raw: object, *, field: str) -> tuple[str, ...]:
    if not isinstance(raw, list) or not raw:
        raise FallbackRegistryError(f"{field} must be a non-empty list")
    values: list[str] = []
    seen: set[int] = set()
    for value in raw:
        text = str(value).strip()
        numeric = _parse_uint32(text, field=field)
        if numeric in seen:
            kind = "route" if field.endswith("route_candidates") else "option"
            raise FallbackRegistryError(f"duplicate numeric {kind} candidate in {field}: {text}")
        seen.add(numeric)
        values.append(text)
    return tuple(values)


def load_candidate_registry(path: Path) -> FallbackCandidateRegistry:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FallbackRegistryError(f"cannot load fallback registry {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FallbackRegistryError("fallback registry root must be an object")

    schema_version = str(payload.get("schema_version") or "")
    if schema_version != FALLBACK_REGISTRY_SCHEMA_VERSION:
        raise FallbackRegistryError(
            f"unsupported fallback registry schema_version: {schema_version or '<missing>'}"
        )
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, dict) or not raw_sections:
        raise FallbackRegistryError("fallback registry sections must be a non-empty object")

    sections: dict[str, FallbackSectionCandidates] = {}
    for section, raw_entry in raw_sections.items():
        if not isinstance(section, str) or not section.strip():
            raise FallbackRegistryError("fallback registry section name must not be empty")
        if not isinstance(raw_entry, dict):
            raise FallbackRegistryError(f"section {section} entry must be an object")
        route_field = str(raw_entry.get("route_field") or "")
        if route_field != "IOPort/Address":
            raise FallbackRegistryError(
                f"section {section} route_field must be IOPort/Address"
            )
        route_enabled = raw_entry.get("route_fallback_enabled") is True
        option_enabled = raw_entry.get("option_fallback_enabled") is True
        if not route_enabled:
            raise FallbackRegistryError(f"section {section} route fallback must be enabled")
        if option_enabled:
            raise FallbackRegistryError(
                f"section {section} option fallback must remain disabled"
            )
        sections[section] = FallbackSectionCandidates(
            section=section,
            route_field=route_field,
            route_candidates=_load_candidate_values(
                raw_entry.get("route_candidates"),
                field=f"{section}.route_candidates",
            ),
            option_candidates=_load_candidate_values(
                raw_entry.get("option_candidates"),
                field=f"{section}.option_candidates",
            ),
            route_fallback_enabled=route_enabled,
            option_fallback_enabled=option_enabled,
        )

    return FallbackCandidateRegistry(
        schema_version=schema_version,
        sections=MappingProxyType(sections),
    )


def route_candidates_for(
    registry: FallbackCandidateRegistry,
    section: str,
    *,
    baseline_value: str,
) -> tuple[str, ...]:
    try:
        entry = registry.sections[section]
    except KeyError as exc:
        raise FallbackRegistryError(f"section has no fallback registry entry: {section}") from exc
    baseline = _parse_uint32(baseline_value, field=f"{section}.baseline_value")
    return tuple(
        candidate
        for candidate in entry.route_candidates
        if _parse_uint32(candidate, field=f"{section}.route_candidates") != baseline
    )


def _channel_names(values: object, *, require_prefix: bool = False) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    names: list[str] = []
    for value in values:
        if isinstance(value, str) and value.startswith("channel:"):
            name = value.split(":", 1)[1].strip()
        elif isinstance(value, str) and not require_prefix:
            name = value.strip()
        elif isinstance(value, dict) and not require_prefix:
            name = str(value.get("channel") or "").strip()
        else:
            name = ""
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _report_channel_outcomes(
    report: Mapping[str, object],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    channel_summary = report.get("channel_summary")
    if isinstance(channel_summary, dict):
        passed = _channel_names(channel_summary.get("passed_channels"))
        failed = _channel_names(channel_summary.get("failed_channels"))
        if passed or failed:
            return passed, failed

    breakdown = report.get("result_breakdown")
    if not isinstance(breakdown, dict):
        return (), ()
    return (
        _channel_names(breakdown.get("pass"), require_prefix=True),
        _channel_names(breakdown.get("fail"), require_prefix=True),
    )


def _status_code_succeeded(value: object) -> bool:
    try:
        return int(str(value).strip(), 0) == 0
    except (TypeError, ValueError):
        return False


def _gpio_route_probe_outcomes(
    report: Mapping[str, object],
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    metrics = report.get("metrics")
    banks = metrics.get("banks") if isinstance(metrics, dict) else None
    calls = report.get("api_calls")
    if not isinstance(banks, dict) or not banks or not isinstance(calls, list):
        return "MISSING", (), ()

    statuses: dict[str, object] = {}
    for call in calls:
        if isinstance(call, dict):
            statuses[str(call.get("name") or "")] = call.get("status_code")

    passed: list[str] = []
    failed: list[str] = []
    for bank in banks:
        required = (
            f"GPIO GetCaps input:{bank}",
            f"GPIO GetCaps output:{bank}",
        )
        if any(name not in statuses for name in required):
            return "MISSING", (), ()
        if any(_status_code_succeeded(statuses[name]) for name in required):
            passed.append(str(bank))
        else:
            failed.append(str(bank))

    if passed:
        return "PARTIAL_SUCCESS", tuple(passed), tuple(failed)
    return "ALL_FAILED", (), tuple(failed)


def _hex_or_none(value: object) -> int | None:
    try:
        return int(str(value).strip(), 0)
    except (TypeError, ValueError):
        return None


def gpio_capability_coverage(report: Mapping[str, object]) -> dict[str, dict[str, int]]:
    """Per bank: expected mask, mask supported for both input and output, missing.

    Only banks whose GetCaps input and output both returned SUCCESS are counted;
    a mask reported next to a failed GetCaps is not capability evidence.
    """

    metrics = report.get("metrics")
    banks = metrics.get("banks") if isinstance(metrics, dict) else None
    calls = report.get("api_calls")
    coverage: dict[str, dict[str, int]] = {}
    if not isinstance(banks, dict) or not isinstance(calls, list):
        return coverage
    statuses = {
        str(call.get("name") or ""): call.get("status_code")
        for call in calls
        if isinstance(call, dict)
    }
    for bank, raw in banks.items():
        if not isinstance(raw, dict):
            continue
        if not all(
            _status_code_succeeded(statuses.get(f"GPIO GetCaps {kind}:{bank}"))
            for kind in ("input", "output")
        ):
            continue
        expected = _hex_or_none(raw.get("expected_mask"))
        inputs = _hex_or_none(raw.get("input_support"))
        outputs = _hex_or_none(raw.get("output_support"))
        if expected is None or inputs is None or outputs is None:
            continue
        supported = expected & inputs & outputs
        coverage[str(bank)] = {
            "expected": expected,
            "supported": supported,
            "missing": expected & ~supported,
        }
    return coverage


def gpio_pin_counts(report: Mapping[str, object]) -> dict[str, object]:
    """Whole-report GPIO coverage: GetCaps state plus supported/expected pin counts."""

    probe_state, _, _ = _gpio_route_probe_outcomes(report)
    coverage = gpio_capability_coverage(report)
    return {
        "gpio_caps_state": probe_state,
        "gpio_expected_pins": sum(bin(item["expected"]).count("1") for item in coverage.values()),
        "gpio_supported_pins": sum(bin(item["supported"]).count("1") for item in coverage.values()),
    }


def section_baseline_route(ini_text: str, section: str) -> str | None:
    """The single IOPort/Address value used by every tuple row of a section."""

    try:
        routes = set(
            rewrite_section_route(ini_text, section=section, route_value="0").before_routes.values()
        )
    except FallbackMutationError:
        return None
    values = {_parse_uint32(route, field=f"{section}.route") for route in routes}
    return next(iter(routes)) if len(values) == 1 else None


def _gpio_candidate_succeeded(report: Mapping[str, object]) -> bool:
    metrics = report.get("metrics")
    banks = metrics.get("banks") if isinstance(metrics, dict) else None
    calls = report.get("api_calls")
    if not isinstance(banks, dict) or not banks or not isinstance(calls, list):
        return False

    statuses: dict[str, object] = {}
    for call in calls:
        if isinstance(call, dict):
            statuses[str(call.get("name") or "")] = call.get("status_code")

    coverage = gpio_capability_coverage(report)
    for bank, raw_metrics in banks.items():
        if not isinstance(raw_metrics, dict) or raw_metrics.get("reads_ok") is not True:
            return False
        required = (
            f"GPIO GetCaps input:{bank}",
            f"GPIO GetCaps output:{bank}",
            f"GPIO GetDirection:{bank}",
            f"GPIO GetLevel:{bank}",
        )
        if any(
            name not in statuses or not _status_code_succeeded(statuses[name])
            for name in required
        ):
            return False
        bank_coverage = coverage.get(str(bank))
        if bank_coverage is None:
            return False
        expected_pins = bin(bank_coverage["expected"]).count("1")
        supported_pins = bin(bank_coverage["supported"]).count("1")
        if expected_pins == 0 or supported_pins * 2 <= expected_pins:
            return False
    return True


def _trigger_code_for_section(section: str) -> str:
    return GPIO_TRIGGER_CODE if section == "GPIO" else DEFAULT_TRIGGER_CODE


def _success_condition_for_section(section: str) -> str:
    return GPIO_SUCCESS_CONDITION if section == "GPIO" else DEFAULT_SUCCESS_CONDITION


def analyze_section_trigger(
    summary_section: Mapping[str, object],
    report: Mapping[str, object],
) -> FallbackTriggerDecision:
    """Classify baseline evidence without guessing missing channel semantics."""

    passed, failed = _report_channel_outcomes(report)
    execution_status = str(summary_section.get("execution_status") or "").upper()
    if execution_status != "COMPLETED":
        return FallbackTriggerDecision(
            False,
            "INFRASTRUCTURE_ERROR_NO_FALLBACK",
            f"section execution_status is {execution_status or '<missing>'}",
            passed,
            failed,
        )

    combined_reason = " ".join(
        str(value or "")
        for value in (
            summary_section.get("reason"),
            report.get("reason"),
            report.get("result"),
        )
    ).upper()
    if any(
        marker in combined_reason
        for marker in ("FIXTURE", "BLOCKED_SAFETY", "BLOCKED_PARAMETER")
    ):
        return FallbackTriggerDecision(
            False,
            "FIXTURE_OR_SAFETY_BLOCK_NO_FALLBACK",
            "fixture, safety, or parameter gating is not a route fallback trigger",
            passed,
            failed,
        )

    if str(summary_section.get("section") or "") == "GPIO":
        probe_state, gpio_passed, gpio_failed = _gpio_route_probe_outcomes(report)
        if probe_state == "MISSING":
            return FallbackTriggerDecision(
                False,
                "GPIO_ROUTE_PROBE_EVIDENCE_MISSING",
                "report does not contain both GPIO GetCaps probes for every required bank",
                (),
                (),
            )
        if probe_state == "PARTIAL_SUCCESS":
            return FallbackTriggerDecision(
                False,
                "GPIO_ROUTE_PROBE_PARTIAL_SUCCESS_NO_FALLBACK",
                "at least one required GPIO GetCaps route probe succeeded",
                gpio_passed,
                gpio_failed,
            )
        return FallbackTriggerDecision(
            True,
            GPIO_TRIGGER_CODE,
            "all required GPIO GetCaps route probes failed",
            (),
            gpio_failed,
        )

    if passed:
        return FallbackTriggerDecision(
            False,
            "PARTIAL_FAIL_NO_FALLBACK",
            "at least one section channel already has a successful API result",
            passed,
            failed,
        )
    if not failed:
        return FallbackTriggerDecision(
            False,
            "CHANNEL_EVIDENCE_MISSING",
            "report does not identify failed section channels",
            passed,
            failed,
        )

    layers = report.get("validation_layers")
    api_layer = ""
    if isinstance(layers, dict):
        api_layer = str(layers.get("L3_api") or "").upper()
    if not api_layer.startswith("FAIL"):
        return FallbackTriggerDecision(
            False,
            "API_NOT_ALL_FAILED_NO_FALLBACK",
            f"L3_api is {api_layer or '<missing>'}, not FAIL",
            passed,
            failed,
        )

    return FallbackTriggerDecision(
        True,
        "EXPECTED_SECTION_ALL_CHANNEL_API_FAILED",
        "all reported section channels failed and L3_api is FAIL",
        passed,
        failed,
    )


def _require_sha256(value: object, *, field: str) -> str:
    text = str(value or "").lower()
    if len(text) != 64 or any(ch not in "0123456789abcdef" for ch in text):
        raise FallbackPlanError(f"{field} must be a SHA-256 hex digest")
    return text


def build_fallback_plan(
    *,
    project: str,
    run_id: str,
    summary_path: str,
    summary_sha256: str,
    full_ini_path: str,
    full_ini_sha256: str,
    section_decisions: list[Mapping[str, object]],
    registry: FallbackCandidateRegistry,
    applicable_sections: set[str] | None = None,
) -> dict[str, object]:
    """Build a deterministic plan from AI-selected, policy-eligible sections."""

    if not project.strip() or not run_id.strip():
        raise FallbackPlanError("project and run_id must not be empty")
    sections: list[dict[str, object]] = []
    seen: set[str] = set()
    for decision in section_decisions:
        section = str(decision.get("section") or "")
        if section in seen:
            raise FallbackPlanError(f"duplicate fallback section: {section}")
        seen.add(section)
        if applicable_sections is not None and section not in applicable_sections:
            raise FallbackPlanError(
                f"section is not applicable/generated and cannot enter fallback: {section}"
            )
        if section not in registry.sections:
            raise FallbackPlanError(f"section has no fallback registry entry: {section}")
        trigger_code = str(decision.get("trigger_code") or "")
        if trigger_code != _trigger_code_for_section(section):
            raise FallbackPlanError(
                f"section {section} trigger is not fallback eligible: "
                f"{trigger_code or '<missing>'}"
            )
        passed = [str(x) for x in (decision.get("passed_channels") or [])]
        failed = [str(x) for x in (decision.get("failed_channels") or [])]
        if passed or not failed:
            raise FallbackPlanError(
                f"section {section} must have zero passed and at least one failed channel"
            )
        baseline_route = str(decision.get("baseline_route") or "")
        candidates = route_candidates_for(
            registry,
            section,
            baseline_value=baseline_route,
        )
        sections.append(
            {
                "section": section,
                "status": "PLANNED" if candidates else "NO_ALTERNATIVE_CANDIDATE",
                "trigger_code": trigger_code,
                "trigger_reason": str(decision.get("trigger_reason") or ""),
                "passed_channels": passed,
                "failed_channels": failed,
                "baseline_route": baseline_route,
                "baseline_report_path": str(
                    decision.get("baseline_report_path") or ""
                ),
                "baseline_report_sha256": _require_sha256(
                    decision.get("baseline_report_sha256"),
                    field=f"{section}.baseline_report_sha256",
                ),
                "route_field": registry.sections[section].route_field,
                "route_candidates": list(candidates),
                "option_fallback_enabled": False,
                "success_condition": _success_condition_for_section(section),
            }
        )

    return {
        "schema_version": FALLBACK_PLAN_SCHEMA_VERSION,
        "project": project,
        "run_id": run_id,
        "baseline": {
            "summary_path": summary_path,
            "summary_sha256": _require_sha256(
                summary_sha256, field="summary_sha256"
            ),
            "full_ini_path": full_ini_path,
            "full_ini_sha256": _require_sha256(
                full_ini_sha256, field="full_ini_sha256"
            ),
        },
        "sections": sections,
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_and_validate_fallback_plan(
    path: Path,
    *,
    expected_project: str,
    expected_run_id: str,
    expected_full_ini: Path,
    registry: FallbackCandidateRegistry,
    applicable_sections: set[str] | None = None,
) -> dict[str, object]:
    """Load an AI-produced plan and enforce provenance plus registry policy."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FallbackPlanError(f"cannot read fallback plan {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FallbackPlanError("fallback plan root must be an object")
    if payload.get("schema_version") != FALLBACK_PLAN_SCHEMA_VERSION:
        raise FallbackPlanError("unsupported fallback plan schema_version")
    if payload.get("project") != expected_project:
        raise FallbackPlanError("fallback plan project mismatch")
    if payload.get("run_id") != expected_run_id:
        raise FallbackPlanError("fallback plan run_id mismatch")
    baseline = payload.get("baseline")
    if not isinstance(baseline, dict):
        raise FallbackPlanError("fallback plan baseline must be an object")
    planned_full_ini = Path(str(baseline.get("full_ini_path") or "")).resolve()
    if planned_full_ini != expected_full_ini.resolve():
        raise FallbackPlanError("fallback plan full INI path mismatch")
    if _sha256_file(expected_full_ini) != str(baseline.get("full_ini_sha256") or "").lower():
        raise FallbackPlanError("fallback plan full INI hash mismatch")
    summary_path = Path(str(baseline.get("summary_path") or ""))
    if not summary_path.is_file() or _sha256_file(summary_path) != str(
        baseline.get("summary_sha256") or ""
    ).lower():
        raise FallbackPlanError("fallback plan summary hash mismatch")

    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, list):
        raise FallbackPlanError("fallback plan sections must be a list")
    seen: set[str] = set()
    for raw_section in raw_sections:
        if not isinstance(raw_section, dict):
            raise FallbackPlanError("fallback plan section entry must be an object")
        section = str(raw_section.get("section") or "")
        if section in seen:
            raise FallbackPlanError(f"duplicate fallback section: {section}")
        seen.add(section)
        if applicable_sections is not None and section not in applicable_sections:
            raise FallbackPlanError(
                f"section is not applicable/generated and cannot enter fallback: {section}"
            )
        if section not in registry.sections:
            raise FallbackPlanError(f"section has no fallback registry entry: {section}")
        if raw_section.get("trigger_code") != _trigger_code_for_section(section):
            raise FallbackPlanError(f"section {section} trigger is not fallback eligible")
        if raw_section.get("option_fallback_enabled") is not False:
            raise FallbackPlanError("option fallback must remain disabled")
        if raw_section.get("success_condition") != _success_condition_for_section(section):
            raise FallbackPlanError("unsupported fallback success condition")
        expected_candidates = list(
            route_candidates_for(
                registry,
                section,
                baseline_value=str(raw_section.get("baseline_route") or ""),
            )
        )
        if raw_section.get("route_candidates") != expected_candidates:
            raise FallbackPlanError(
                f"section {section} route candidates do not match registry order"
            )
        expected_status = "PLANNED" if expected_candidates else "NO_ALTERNATIVE_CANDIDATE"
        if raw_section.get("status") != expected_status:
            raise FallbackPlanError(f"section {section} plan status mismatch")
        report_path = Path(str(raw_section.get("baseline_report_path") or ""))
        if not report_path.is_file() or _sha256_file(report_path) != str(
            raw_section.get("baseline_report_sha256") or ""
        ).lower():
            raise FallbackPlanError(f"section {section} baseline report hash mismatch")
    return payload


_SECTION_HEADER_RE = re.compile(r"^\s*\[([^]]+)\]\s*(?:[;#].*)?$")


def rewrite_section_route(
    ini_text: str,
    *,
    section: str,
    route_value: str,
    immutable_keys: tuple[str, ...] = (),
) -> IniRouteMutation:
    """Replace tuple field 3 in one INI section while preserving other text."""

    _parse_uint32(route_value, field=f"{section}.route_value")
    immutable = {key.casefold() for key in immutable_keys}
    lines = ini_text.splitlines(keepends=True)
    in_target = False
    found_section = False
    changed_keys: list[str] = []
    before_routes: dict[str, str] = {}
    output: list[str] = []

    for original in lines:
        content = original.rstrip("\r\n")
        newline = original[len(content) :]
        header = _SECTION_HEADER_RE.match(content)
        if header:
            in_target = header.group(1).strip().casefold() == section.casefold()
            found_section = found_section or in_target
            output.append(original)
            continue
        stripped = content.strip()
        if (
            not in_target
            or not stripped
            or stripped.startswith((";", "#"))
            or "=" not in content
        ):
            output.append(original)
            continue

        left, value = content.split("=", 1)
        key = left.strip()
        if key.casefold() in immutable:
            output.append(original)
            continue
        fields = value.split(",")
        if len(fields) < 4:
            raise FallbackMutationError(
                f"section {section} key {key} has fewer than four tuple fields"
            )
        route_field = fields[2]
        old_route = route_field.strip()
        _parse_uint32(old_route, field=f"{section}.{key}.route")
        before_routes[key] = old_route
        if _parse_uint32(old_route, field=f"{section}.{key}.route") == _parse_uint32(
            route_value, field=f"{section}.route_value"
        ):
            output.append(original)
            continue

        leading = route_field[: len(route_field) - len(route_field.lstrip())]
        trailing = route_field[len(route_field.rstrip()) :]
        fields[2] = f"{leading}{route_value}{trailing}"
        output.append(f"{left}={','.join(fields)}{newline}")
        changed_keys.append(key)

    if not found_section:
        raise FallbackMutationError(f"section not found in full INI: {section}")
    if not before_routes:
        raise FallbackMutationError(f"section {section} has no mutable tuple rows")

    return IniRouteMutation(
        text="".join(output),
        section=section,
        route_value=route_value,
        changed_keys=tuple(changed_keys),
        before_routes=MappingProxyType(before_routes),
    )


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _attempt_infrastructure_ok(payload: Mapping[str, object]) -> bool:
    deploy = payload.get("deploy")
    reload_result = payload.get("reload")
    report = payload.get("report")
    if not isinstance(deploy, dict) or str(deploy.get("status") or "").upper() != "PASS":
        return False
    if not isinstance(reload_result, dict) or str(reload_result.get("status") or "").upper() != "PASS":
        return False
    readiness = reload_result.get("readiness")
    if not isinstance(readiness, dict) or readiness.get("ready") is not True:
        return False
    return isinstance(report, dict)


def execute_section_fallback(
    *,
    baseline_ini_text: str,
    plan_section: Mapping[str, object],
    attempt_runner: Callable[[FallbackAttemptCandidate], Mapping[str, object]],
) -> dict[str, object]:
    """Execute deterministic candidate control flow through an injected runner."""

    section = str(plan_section.get("section") or "")
    if str(plan_section.get("status") or "") != "PLANNED":
        return {
            "section": section,
            "status": "NO_ALTERNATIVE_CANDIDATE",
            "selected_route": None,
            "attempts": [],
            "effective_ini_text": baseline_ini_text,
        }
    if plan_section.get("option_fallback_enabled") is not False:
        raise FallbackPlanError("option fallback must remain disabled")
    expected_success_condition = _success_condition_for_section(section)
    if plan_section.get("success_condition") != expected_success_condition:
        raise FallbackPlanError("unsupported fallback success condition")
    raw_candidates = plan_section.get("route_candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise FallbackPlanError(f"section {section} has no route candidates")

    immutable_keys = ("Channel1",) if section == "SMBus" else ()
    attempts: list[dict[str, object]] = []
    for index, raw_route in enumerate(raw_candidates, start=1):
        route = str(raw_route)
        mutation = rewrite_section_route(
            baseline_ini_text,
            section=section,
            route_value=route,
            immutable_keys=immutable_keys,
        )
        candidate = FallbackAttemptCandidate(
            index=index,
            section=section,
            route_value=route,
            ini_text=mutation.text,
            ini_sha256=_sha256_text(mutation.text),
            changed_keys=mutation.changed_keys,
            before_routes=mutation.before_routes,
        )
        runner_payload = attempt_runner(candidate)
        passed: tuple[str, ...] = ()
        failed: tuple[str, ...] = ()
        report = runner_payload.get("report")
        if isinstance(report, dict):
            if section == "GPIO":
                _, passed, failed = _gpio_route_probe_outcomes(report)
            else:
                passed, failed = _report_channel_outcomes(report)
        infrastructure_ok = _attempt_infrastructure_ok(runner_payload)
        if section == "GPIO" and isinstance(report, dict):
            success = infrastructure_ok and _gpio_candidate_succeeded(report)
        else:
            success = infrastructure_ok and bool(passed)
        attempt_evidence: dict[str, object] = {
            "index": index,
            "route_value": route,
            "candidate_ini_sha256": candidate.ini_sha256,
            "changed_keys": list(candidate.changed_keys),
            "before_routes": dict(candidate.before_routes),
            "deploy": runner_payload.get("deploy"),
            "reload": runner_payload.get("reload"),
            "report_path": runner_payload.get("report_path"),
            "report_sha256": runner_payload.get("report_sha256"),
            "passed_channels": list(passed),
            "failed_channels": list(failed),
            "success": success,
        }
        if section == "GPIO" and isinstance(report, dict):
            attempt_evidence.update(gpio_pin_counts(report))
        attempts.append(attempt_evidence)
        if not infrastructure_ok:
            return {
                "section": section,
                "status": "ORCHESTRATOR_ERROR",
                "selected_route": None,
                "attempts": attempts,
                "effective_ini_text": baseline_ini_text,
            }
        if success:
            return {
                "section": section,
                "status": "CONVERGED",
                "selected_route": route,
                "attempts": attempts,
                "effective_ini_text": candidate.ini_text,
            }

    return {
        "section": section,
        "status": "ALL_CANDIDATES_FAILED",
        "selected_route": None,
        "attempts": attempts,
        "effective_ini_text": baseline_ini_text,
    }


def execute_fallback_plan(
    *,
    baseline_ini_text: str,
    plan: Mapping[str, object],
    attempt_runner: Callable[[FallbackAttemptCandidate], Mapping[str, object]],
    restore_runner: Callable[[str, str], Mapping[str, object]],
) -> dict[str, object]:
    """Run planned sections sequentially and accumulate only converged changes."""

    raw_sections = plan.get("sections")
    if not isinstance(raw_sections, list):
        raise FallbackPlanError("fallback plan sections must be a list")
    current_ini = baseline_ini_text
    section_results: list[dict[str, object]] = []
    converged_count = 0
    failed_count = 0
    orchestrator_error = False

    for raw_section in raw_sections:
        if not isinstance(raw_section, dict):
            raise FallbackPlanError("fallback plan section entry must be an object")
        result = execute_section_fallback(
            baseline_ini_text=current_ini,
            plan_section=raw_section,
            attempt_runner=attempt_runner,
        )
        effective = str(result.pop("effective_ini_text"))
        status = str(result.get("status") or "")
        if status == "CONVERGED":
            current_ini = effective
            converged_count += 1
        elif status == "ALL_CANDIDATES_FAILED":
            failed_count += 1
            restore = dict(restore_runner(str(result.get("section") or ""), current_ini))
            result["restore"] = restore
            if str(restore.get("status") or "").upper() != "PASS":
                orchestrator_error = True
        elif status == "ORCHESTRATOR_ERROR":
            restore = dict(restore_runner(str(result.get("section") or ""), current_ini))
            result["restore"] = restore
            orchestrator_error = True
            section_results.append(result)
            break
        section_results.append(result)

    if orchestrator_error:
        status = "ORCHESTRATOR_ERROR"
    elif failed_count and converged_count:
        status = "PARTIAL"
    elif failed_count:
        status = "EXHAUSTED"
    elif converged_count:
        status = "CONVERGED"
    else:
        status = "NO_ACTION"
    return {
        "schema_version": "machineb.fallback_convergence.v1",
        "status": status,
        "baseline_ini_sha256": _sha256_text(baseline_ini_text),
        "effective_ini_sha256": _sha256_text(current_ini),
        "sections": section_results,
        "effective_ini_text": current_ini,
    }


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def persist_project_route_overrides(
    path: Path,
    *,
    project: str,
    run_id: str,
    convergence: Mapping[str, object],
) -> dict[str, object]:
    """Merge only validated convergence successes into a project-scoped file."""

    if path.exists():
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FallbackOverrideError(f"cannot read project override {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise FallbackOverrideError("project override root must be an object")
        if payload.get("schema_version") != PROJECT_OVERRIDE_SCHEMA_VERSION:
            raise FallbackOverrideError("unsupported project override schema_version")
        if payload.get("project") != project:
            raise FallbackOverrideError("project override project mismatch")
    else:
        payload = {
            "schema_version": PROJECT_OVERRIDE_SCHEMA_VERSION,
            "project": project,
            "sections": {},
        }
    sections = payload.get("sections")
    if not isinstance(sections, dict):
        raise FallbackOverrideError("project override sections must be an object")
    raw_results = convergence.get("sections")
    if not isinstance(raw_results, list):
        raise FallbackOverrideError("convergence sections must be a list")

    for result in raw_results:
        if not isinstance(result, dict) or result.get("status") != "CONVERGED":
            continue
        section = str(result.get("section") or "")
        route = str(result.get("selected_route") or "")
        if not section or not route:
            raise FallbackOverrideError("converged section requires section and selected_route")
        _parse_uint32(route, field=f"{section}.route_value")
        attempts = result.get("attempts")
        selected_attempt = attempts[-1] if isinstance(attempts, list) and attempts else {}
        report_sha256 = (
            selected_attempt.get("report_sha256")
            if isinstance(selected_attempt, dict)
            else None
        )
        sections[section] = {
            "route_field": "IOPort/Address",
            "route_value": route,
            "option_fallback_enabled": False,
            "validation_status": "CONVERGED",
            "validated_run_id": run_id,
            "report_sha256": report_sha256,
        }

    payload["sections"] = sections
    _atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return payload


def apply_project_route_overrides(
    *,
    project: str,
    override_path: Path,
    full_ini_path: Path,
    section_paths: Mapping[str, Path],
) -> dict[str, object]:
    """Apply validated project routes to newly generated full and split INIs."""

    if not override_path.exists():
        return {"status": "NOT_PRESENT", "sections": []}
    try:
        payload = json.loads(override_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FallbackOverrideError(f"cannot read project override {override_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise FallbackOverrideError("project override root must be an object")
    if payload.get("schema_version") != PROJECT_OVERRIDE_SCHEMA_VERSION:
        raise FallbackOverrideError("unsupported project override schema_version")
    if payload.get("project") != project:
        raise FallbackOverrideError("project override project mismatch")
    raw_sections = payload.get("sections")
    if not isinstance(raw_sections, dict):
        raise FallbackOverrideError("project override sections must be an object")

    full_text = full_ini_path.read_text(encoding="utf-8")
    applied: list[str] = []
    details: dict[str, object] = {}
    for section, raw_entry in raw_sections.items():
        if section not in section_paths:
            raise FallbackOverrideError(
                f"project override section is not generated: {section}"
            )
        if not isinstance(raw_entry, dict):
            raise FallbackOverrideError(f"project override section {section} must be an object")
        if raw_entry.get("validation_status") != "CONVERGED":
            raise FallbackOverrideError(
                f"project override section {section} is not validated"
            )
        if raw_entry.get("option_fallback_enabled") not in (None, False):
            raise FallbackOverrideError("option fallback must remain disabled")
        route = str(raw_entry.get("route_value") or "")
        immutable = ("Channel1",) if section == "SMBus" else ()
        full_mutation = rewrite_section_route(
            full_text,
            section=section,
            route_value=route,
            immutable_keys=immutable,
        )
        split_path = Path(section_paths[section])
        split_mutation = rewrite_section_route(
            split_path.read_text(encoding="utf-8"),
            section=section,
            route_value=route,
            immutable_keys=immutable,
        )
        full_text = full_mutation.text
        _atomic_write_text(split_path, split_mutation.text)
        applied.append(section)
        details[section] = {
            "route_value": route,
            "changed_full_ini_keys": list(full_mutation.changed_keys),
            "changed_split_ini_keys": list(split_mutation.changed_keys),
            "split_ini_sha256": _sha256_text(split_mutation.text),
        }

    _atomic_write_text(full_ini_path, full_text)
    return {
        "status": "APPLIED" if applied else "EMPTY",
        "path": str(override_path),
        "sha256": hashlib.sha256(override_path.read_bytes()).hexdigest(),
        "sections": applied,
        "details": details,
        "full_ini_sha256": _sha256_text(full_text),
    }
