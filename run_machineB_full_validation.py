#!/usr/bin/env python3
"""Deterministic Machine-B full-validation orchestrator (P1-P12).

This is the post-INI execution engine used by the public
``/susiagent <PROJECT> --all --host <HOST>`` workflow.  The public ``--all``
mode is translated by the susiagent skill into this module's explicit
``--execute --host ... --user ...`` CLI.  Without public ``--all``, susiagent
stops after its existing generation/convergence phase and does not call this
module's execute mode.

Contract construction and dry-run remain local-only. Execute mode performs
deterministic staging, one validation activation reload, safe-default section
execution, report aggregation, runtime-INI rollback, and a separate recovery
reload.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PureWindowsPath
from types import MappingProxyType
from typing import Any, Mapping

from build_machineB_section_configs import BuildError, build_section_configs
from machineb_fallback import (
    FallbackAttemptCandidate,
    apply_project_route_overrides,
    execute_fallback_plan,
    gpio_capability_coverage,
    load_and_validate_fallback_plan,
    load_candidate_registry,
    persist_project_route_overrides,
)


CONTRACT_SCHEMA_VERSION = "machineb.post_ini_contract.v1"
MANIFEST_SCHEMA_VERSION = "machineb.execution_manifest.v1"
SUMMARY_SCHEMA_VERSION = "machineb.validation_summary.v1"
WRITE_POLICY = "phase1_reversible_writes_enabled"
READ_ONLY_POLICY = "read_only_no_write_tests"
DEFAULT_REMOTE_VERIFY_ROOT = PureWindowsPath(r"C:\Users\susiaa\Desktop\verify")
DEFAULT_REMOTE_SUSI_ROOT = PureWindowsPath(r"C:\Windows\SUSI")
DEFAULT_RELOAD_BAT = PureWindowsPath(
    r"C:\Users\susiaa\Desktop\reload driver\reload_susi4_driver.bat"
)

EXECUTION_MANIFEST_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": MANIFEST_SCHEMA_VERSION,
    "type": "object",
    "additionalProperties": True,
    "required": [
        "schema_version",
        "contract_schema_version",
        "project",
        "run_id",
        "mode",
        "inputs",
        "target",
        "sections",
        "safety_policy",
        "config_build",
        "staging_plan",
    ],
    "properties": {
        "schema_version": {"const": MANIFEST_SCHEMA_VERSION},
        "contract_schema_version": {"const": CONTRACT_SCHEMA_VERSION},
        "project": {"type": "string", "minLength": 1},
        "run_id": {"type": "string", "minLength": 1},
        "mode": {"enum": ["dry-run", "execute", "converge"]},
        "inputs": {"type": "object"},
        "target": {"type": "object"},
        "sections": {"type": "array"},
        "safety_policy": {"type": "object"},
        "config_build": {"type": "object"},
        "staging_plan": {"type": "object"},
    },
}

VALIDATION_SUMMARY_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": SUMMARY_SCHEMA_VERSION,
    "type": "object",
    "additionalProperties": True,
    "required": [
        "schema_version",
        "contract_schema_version",
        "project",
        "run_id",
        "status",
        "exit_code",
        "timestamps",
        "runtime_ini",
        "reload",
        "sections",
        "warnings",
        "errors",
    ],
    "properties": {
        "schema_version": {"const": SUMMARY_SCHEMA_VERSION},
        "contract_schema_version": {"const": CONTRACT_SCHEMA_VERSION},
        "project": {"type": "string", "minLength": 1},
        "run_id": {"type": "string", "minLength": 1},
        "status": {"type": "string", "minLength": 1},
        "exit_code": {"type": "integer", "enum": [0, 1, 2]},
        "timestamps": {"type": "object"},
        "runtime_ini": {"type": "object"},
        "reload": {"type": "object"},
        "sections": {"type": "array"},
        "warnings": {"type": "array"},
        "errors": {"type": "array"},
    },
}


class ExecutionTier(str, Enum):
    """Registry tiers ordered from low-complexity reads to active control."""

    SIMPLE_READ = "simple_read"
    GATED_READ = "gated_read"
    REVERSIBLE_WRITE = "reversible_write"
    DEPENDENT_CONTROL = "dependent_control"


@dataclass(frozen=True)
class SectionRegistryEntry:
    section: str
    config_template: str
    runner: str
    report_prefix: str
    execution_tier: ExecutionTier
    difficulty_rank: int
    difficulty_reason: str
    script_dependencies: tuple[str, ...] = ("common_susi.ps1",)
    section_dependencies: tuple[str, ...] = ()
    opt_in_switches: tuple[str, ...] = ()
    default_switches: tuple[str, ...] = ()
    extra_path_arguments: tuple[str, ...] = ()


# Normal execution order starts with low-risk reads, keeps dependency-coupled
# sections adjacent (HWM.Fan -> HWM.Fan.Control), then continues with the
# independently gated reversible-write sections.
SECTION_REGISTRY: tuple[SectionRegistryEntry, ...] = (
    SectionRegistryEntry(
        "HWM.CaseOpen",
        "{model}_caseopen.json",
        "run_hwm_caseopen_validation.ps1",
        "hwm_caseopen",
        ExecutionTier.SIMPLE_READ,
        1,
        "single digital-state read",
    ),
    SectionRegistryEntry(
        "HWM.Current",
        "{model}_current.json",
        "run_hwm_current_validation.ps1",
        "hwm_current",
        ExecutionTier.SIMPLE_READ,
        2,
        "scalar current telemetry read",
    ),
    SectionRegistryEntry(
        "HWM.Voltage",
        "{model}_voltage.json",
        "run_hwm_voltage_validation.ps1",
        "hwm_voltage",
        ExecutionTier.SIMPLE_READ,
        3,
        "multi-channel voltage telemetry read",
    ),
    SectionRegistryEntry(
        "HWM.Temperature",
        "{model}_temperature.json",
        "run_hwm_temperature_validation.ps1",
        "hwm_temperature",
        ExecutionTier.SIMPLE_READ,
        4,
        "sampled temperature telemetry read",
    ),
    SectionRegistryEntry(
        "I2C",
        "{model}_i2c.json",
        "run_i2c_validation.ps1",
        "i2c",
        ExecutionTier.GATED_READ,
        5,
        "protocol capability checks with frequency set/readback/restore",
        opt_in_switches=("EnableSetTest",),
        default_switches=("EnableSetTest",),
    ),
    SectionRegistryEntry(
        "SMBus",
        "{model}_smbus.json",
        "run_smbus_validation.ps1",
        "smbus",
        ExecutionTier.GATED_READ,
        6,
        "protocol capability checks with optional fixture transaction",
        opt_in_switches=("EnableFixtureTest",),
    ),
    SectionRegistryEntry(
        "WDT",
        "{model}_wdt.json",
        "run_wdt_validation.ps1",
        "wdt",
        ExecutionTier.GATED_READ,
        7,
        "watchdog start/readback/trigger/stop without waiting for timeout",
        opt_in_switches=("EnableStartStopTest",),
        default_switches=("EnableStartStopTest",),
    ),
    SectionRegistryEntry(
        "ThermalProtect",
        "{model}_thermalprotect.json",
        "run_thermalprotect_validation.ps1",
        "thermalprotect",
        ExecutionTier.GATED_READ,
        8,
        "safety-sensitive configuration read with SetConfig/readback/restore",
        opt_in_switches=("EnableSetConfigTest",),
        default_switches=("EnableSetConfigTest",),
    ),
    SectionRegistryEntry(
        "HWM.Fan",
        "{model}_fan.json",
        "run_hwm_fan_validation.ps1",
        "hwm_fan",
        ExecutionTier.GATED_READ,
        9,
        "multi-sample tachometer observation with optional stimulus",
        opt_in_switches=("EnableStimulus",),
    ),
    SectionRegistryEntry(
        "HWM.Fan.Control",
        "{model}_fancontrol.json",
        "run_hwm_fan_control_validation_section.ps1",
        "hwm_fan_control",
        ExecutionTier.DEPENDENT_CONTROL,
        10,
        "fan dependency, PWM control, RPM response, and recovery",
        section_dependencies=("HWM.Fan",),
        opt_in_switches=("AllowControl",),
        default_switches=("AllowControl",),
        extra_path_arguments=("FanConfigPath", "FanIniPath"),
    ),
    SectionRegistryEntry(
        "VGA.Backlight",
        "{model}_backlight.json",
        "run_vga_backlight_validation.ps1",
        "vga_backlight",
        ExecutionTier.REVERSIBLE_WRITE,
        11,
        "reversible enable toggle with restore",
        opt_in_switches=("EnableFunctionalTest",),
        default_switches=("EnableFunctionalTest",),
    ),
    SectionRegistryEntry(
        "VGA.Brightness",
        "{model}_brightness.json",
        "run_vga_brightness_validation.ps1",
        "vga_brightness",
        ExecutionTier.REVERSIBLE_WRITE,
        12,
        "reversible brightness change with restore",
        opt_in_switches=("EnableFunctionalTest",),
        default_switches=("EnableFunctionalTest",),
    ),
    SectionRegistryEntry(
        "GPIO",
        "{model}_gpio.json",
        "run_gpio_validation.ps1",
        "gpio",
        ExecutionTier.REVERSIBLE_WRITE,
        13,
        "direction/level writes with state restoration",
        opt_in_switches=("EnableFunctionalTest",),
        default_switches=("EnableFunctionalTest",),
    ),
    SectionRegistryEntry(
        "StorageArea",
        "{model}_storage.json",
        "run_storage_validation.ps1",
        "storage",
        ExecutionTier.REVERSIBLE_WRITE,
        14,
        "persistence write with original-byte restoration",
        opt_in_switches=("EnableWriteTest",),
        default_switches=("EnableWriteTest",),
    ),
)

SECTION_REGISTRY_BY_NAME: Mapping[str, SectionRegistryEntry] = MappingProxyType(
    {entry.section: entry for entry in SECTION_REGISTRY}
)


class ContractErrorCode(str, Enum):
    """Stable machine-readable failures for post-INI contract construction."""

    INVALID_PATH_TOKEN = "INVALID_PATH_TOKEN"
    CASE_DIR_MISSING = "CASE_DIR_MISSING"
    FULL_INI_MISSING = "FULL_INI_MISSING"
    MATRIX_MISSING = "MATRIX_MISSING"
    MATRIX_INVALID_JSON = "MATRIX_INVALID_JSON"
    MATRIX_SCHEMA_INVALID = "MATRIX_SCHEMA_INVALID"
    MATRIX_PROJECT_MISMATCH = "MATRIX_PROJECT_MISMATCH"
    DUPLICATE_MATRIX_SECTION = "DUPLICATE_MATRIX_SECTION"
    GENERATED_SPLIT_INI_PATH_MISSING = "GENERATED_SPLIT_INI_PATH_MISSING"
    GENERATED_SPLIT_INI_MISSING = "GENERATED_SPLIT_INI_MISSING"
    GENERATED_SECTION_NOT_REGISTERED = "GENERATED_SECTION_NOT_REGISTERED"
    SECTION_DEPENDENCY_MISSING = "SECTION_DEPENDENCY_MISSING"
    CONFIG_FILE_MISSING = "CONFIG_FILE_MISSING"
    CONFIG_INVALID_JSON = "CONFIG_INVALID_JSON"
    RUNNER_FILE_MISSING = "RUNNER_FILE_MISSING"
    SCRIPT_DEPENDENCY_MISSING = "SCRIPT_DEPENDENCY_MISSING"
    RUNNER_DIR_MISSING = "RUNNER_DIR_MISSING"
    CONFIG_BUILD_FAILED = "CONFIG_BUILD_FAILED"


class ContractError(ValueError):
    """Contract failure with fields suitable for deterministic reporting."""

    def __init__(
        self,
        code: ContractErrorCode,
        message: str,
        *,
        path: Path | None = None,
        section: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.path = path
        self.section = section

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": str(self),
        }
        if self.path is not None:
            payload["path"] = str(self.path)
        if self.section is not None:
            payload["section"] = self.section
        return payload


class RemoteStageErrorCode(str, Enum):
    """Stable machine-readable failures for P6 remote staging."""

    PLAN_CONFLICT = "PLAN_CONFLICT"
    CONNECTION_FAILED = "CONNECTION_FAILED"
    DIRECTORY_SETUP_FAILED = "DIRECTORY_SETUP_FAILED"
    BACKUP_FAILED = "BACKUP_FAILED"
    UPLOAD_FAILED = "UPLOAD_FAILED"
    HASH_READ_FAILED = "HASH_READ_FAILED"
    HASH_MISMATCH = "HASH_MISMATCH"


class RemoteStageError(RuntimeError):
    def __init__(
        self,
        code: RemoteStageErrorCode,
        message: str,
        *,
        remote_path: str | None = None,
        artifact_kind: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.remote_path = remote_path
        self.artifact_kind = artifact_kind

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code.value,
            "message": str(self),
        }
        if self.remote_path is not None:
            payload["remote_path"] = self.remote_path
        if self.artifact_kind is not None:
            payload["artifact_kind"] = self.artifact_kind
        return payload


@dataclass(frozen=True)
class PostIniInputs:
    case_dir: Path
    full_ini: Path
    section_matrix: Path
    config_dir: Path
    runner_dir: Path
    generated_sections: tuple[str, ...]
    split_inis: Mapping[str, Path]


@dataclass(frozen=True)
class PostIniOutputs:
    run_root: Path
    manifest: Path
    reports_dir: Path
    summary_json: Path
    summary_text: Path
    reload_log: Path
    manifest_schema_version: str
    summary_schema_version: str


@dataclass(frozen=True)
class TargetPaths:
    workspace: str
    scripts_dir: str
    config_dir: str
    ini_dir: str
    run_output: str
    runtime_ini: str
    reload_bat: str


@dataclass(frozen=True)
class PostIniContract:
    schema_version: str
    project: str
    run_id: str
    inputs: PostIniInputs
    outputs: PostIniOutputs
    target: TargetPaths

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation of the fixed contract."""

        return {
            "schema_version": self.schema_version,
            "project": self.project,
            "run_id": self.run_id,
            "inputs": {
                "case_dir": str(self.inputs.case_dir),
                "full_ini": str(self.inputs.full_ini),
                "section_matrix": str(self.inputs.section_matrix),
                "config_dir": str(self.inputs.config_dir),
                "runner_dir": str(self.inputs.runner_dir),
                "generated_sections": list(self.inputs.generated_sections),
                "split_inis": {
                    section: str(path)
                    for section, path in self.inputs.split_inis.items()
                },
            },
            "outputs": {
                "run_root": str(self.outputs.run_root),
                "manifest": str(self.outputs.manifest),
                "reports_dir": str(self.outputs.reports_dir),
                "summary_json": str(self.outputs.summary_json),
                "summary_text": str(self.outputs.summary_text),
                "reload_log": str(self.outputs.reload_log),
                "manifest_schema_version": self.outputs.manifest_schema_version,
                "summary_schema_version": self.outputs.summary_schema_version,
            },
            "target": {
                "workspace": self.target.workspace,
                "scripts_dir": self.target.scripts_dir,
                "config_dir": self.target.config_dir,
                "ini_dir": self.target.ini_dir,
                "run_output": self.target.run_output,
                "runtime_ini": self.target.runtime_ini,
                "reload_bat": self.target.reload_bat,
            },
        }


def _validate_path_token(value: str, field_name: str) -> None:
    if (
        not value
        or value in {".", ".."}
        or "/" in value
        or "\\" in value
        or Path(value).name != value
    ):
        raise ContractError(
            ContractErrorCode.INVALID_PATH_TOKEN,
            f"{field_name} must be one non-empty path component: {value!r}",
        )


def _require_file(path: Path, code: ContractErrorCode, label: str) -> None:
    if not path.is_file():
        raise ContractError(code, f"missing {label}: {path}", path=path)


def _load_matrix(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError as exc:
        raise ContractError(
            ContractErrorCode.MATRIX_INVALID_JSON,
            f"invalid section matrix JSON: {path}: {exc}",
            path=path,
        ) from exc
    except OSError as exc:
        raise ContractError(
            ContractErrorCode.MATRIX_MISSING,
            f"cannot read section matrix: {path}: {exc}",
            path=path,
        ) from exc
    if not isinstance(data, dict):
        raise ContractError(
            ContractErrorCode.MATRIX_SCHEMA_INVALID,
            "section matrix root must be an object",
            path=path,
        )
    return data


def _resolve_generated_sections(
    matrix: Mapping[str, Any], matrix_path: Path, case_dir: Path
) -> tuple[tuple[str, ...], dict[str, Path]]:
    rows = matrix.get("sections")
    if not isinstance(rows, list):
        raise ContractError(
            ContractErrorCode.MATRIX_SCHEMA_INVALID,
            "section matrix 'sections' must be an array",
            path=matrix_path,
        )

    seen: set[str] = set()
    generated: list[str] = []
    split_inis: dict[str, Path] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ContractError(
                ContractErrorCode.MATRIX_SCHEMA_INVALID,
                "every section matrix entry must be an object",
                path=matrix_path,
            )
        section = row.get("section")
        status = row.get("status")
        if not isinstance(section, str) or not section or not isinstance(status, str):
            raise ContractError(
                ContractErrorCode.MATRIX_SCHEMA_INVALID,
                "every section matrix entry requires string section and status fields",
                path=matrix_path,
            )
        if section in seen:
            raise ContractError(
                ContractErrorCode.DUPLICATE_MATRIX_SECTION,
                f"duplicate section matrix entry: {section}",
                path=matrix_path,
                section=section,
            )
        seen.add(section)

        if status != "GENERATED":
            continue
        raw_path = row.get("path")
        if not isinstance(raw_path, str) or not raw_path:
            raise ContractError(
                ContractErrorCode.GENERATED_SPLIT_INI_PATH_MISSING,
                f"GENERATED section has no split INI path: {section}",
                path=matrix_path,
                section=section,
            )
        split_path = Path(raw_path)
        if not split_path.is_absolute():
            split_path = case_dir / split_path
        split_path = split_path.resolve()
        if not split_path.is_file():
            raise ContractError(
                ContractErrorCode.GENERATED_SPLIT_INI_MISSING,
                f"missing split INI for GENERATED section {section}: {split_path}",
                path=split_path,
                section=section,
            )
        generated.append(section)
        split_inis[section] = split_path

    return tuple(generated), split_inis


def build_post_ini_contract(
    *,
    project: str,
    repo_root: str | Path,
    run_id: str,
    remote_verify_root: str | PureWindowsPath = DEFAULT_REMOTE_VERIFY_ROOT,
    remote_susi_root: str | PureWindowsPath = DEFAULT_REMOTE_SUSI_ROOT,
    reload_bat: str | PureWindowsPath = DEFAULT_RELOAD_BAT,
) -> PostIniContract:
    """Resolve and validate P1 inputs, outputs, and canonical target paths.

    This function is intentionally read-only.  It validates authoritative
    post-INI inputs but does not create validation output directories or touch
    a target machine.
    """

    _validate_path_token(project, "project")
    _validate_path_token(run_id, "run_id")

    root = Path(repo_root).expanduser().resolve()
    case_dir = (root / "CASES" / project).resolve()
    if not case_dir.is_dir():
        raise ContractError(
            ContractErrorCode.CASE_DIR_MISSING,
            f"missing project case directory: {case_dir}",
            path=case_dir,
        )

    full_ini = (case_dir / f"{project}-pre.ini").resolve()
    matrix_path = (case_dir / f"{project}-section-matrix.json").resolve()
    runner_dir = (root / "targetB_task" / "machineB_validation").resolve()
    _require_file(full_ini, ContractErrorCode.FULL_INI_MISSING, "full pre-INI")
    _require_file(matrix_path, ContractErrorCode.MATRIX_MISSING, "section matrix")
    if not runner_dir.is_dir():
        raise ContractError(
            ContractErrorCode.RUNNER_DIR_MISSING,
            f"missing Machine-B runner directory: {runner_dir}",
            path=runner_dir,
        )

    matrix = _load_matrix(matrix_path)
    matrix_project = matrix.get("project")
    if matrix_project != project:
        raise ContractError(
            ContractErrorCode.MATRIX_PROJECT_MISMATCH,
            f"matrix project {matrix_project!r} does not match requested project {project!r}",
            path=matrix_path,
        )
    generated_sections, split_inis = _resolve_generated_sections(
        matrix, matrix_path, case_dir
    )

    run_root = (case_dir / "validation_runs" / run_id).resolve()
    inputs = PostIniInputs(
        case_dir=case_dir,
        full_ini=full_ini,
        section_matrix=matrix_path,
        config_dir=(case_dir / "config").resolve(),
        runner_dir=runner_dir,
        generated_sections=generated_sections,
        split_inis=split_inis,
    )
    outputs = PostIniOutputs(
        run_root=run_root,
        manifest=run_root / "manifest.json",
        reports_dir=run_root / "reports",
        summary_json=run_root / f"{project}-machineB-summary.json",
        summary_text=run_root / f"{project}-machineB-summary.txt",
        reload_log=run_root / "reload.log",
        manifest_schema_version=MANIFEST_SCHEMA_VERSION,
        summary_schema_version=SUMMARY_SCHEMA_VERSION,
    )

    workspace = PureWindowsPath(remote_verify_root) / project
    target = TargetPaths(
        workspace=str(workspace),
        scripts_dir=str(workspace / "scripts"),
        config_dir=str(workspace / "config"),
        ini_dir=str(workspace / "ini"),
        run_output=str(workspace / "out" / run_id),
        runtime_ini=str(PureWindowsPath(remote_susi_root) / f"{project}.ini"),
        reload_bat=str(PureWindowsPath(reload_bat)),
    )

    return PostIniContract(
        schema_version=CONTRACT_SCHEMA_VERSION,
        project=project,
        run_id=run_id,
        inputs=inputs,
        outputs=outputs,
        target=target,
    )


def _append_staging_artifact(
    artifacts: list[dict[str, Any]],
    remote_sources: dict[str, str],
    *,
    kind: str,
    local_path: str | Path,
    remote_path: str | PureWindowsPath,
    section: str | None = None,
) -> None:
    local = str(Path(local_path).resolve())
    remote = str(PureWindowsPath(remote_path))
    previous = remote_sources.get(remote.casefold())
    if previous is not None:
        if previous != local:
            raise RemoteStageError(
                RemoteStageErrorCode.PLAN_CONFLICT,
                f"multiple local artifacts map to one remote path: {remote}",
                remote_path=remote,
                artifact_kind=kind,
            )
        return
    remote_sources[remote.casefold()] = local
    artifact: dict[str, Any] = {
        "kind": kind,
        "local_path": local,
        "remote_path": remote,
    }
    if section is not None:
        artifact["section"] = section
    artifacts.append(artifact)


def build_remote_staging_plan(
    contract: PostIniContract, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Build a deterministic P6 upload/backup plan without target access."""

    workspace = PureWindowsPath(contract.target.workspace)
    scripts_dir = PureWindowsPath(contract.target.scripts_dir)
    config_dir = PureWindowsPath(contract.target.config_dir)
    ini_dir = PureWindowsPath(contract.target.ini_dir)
    run_output = PureWindowsPath(contract.target.run_output)
    backup_dir = run_output / "backup"
    backup_ini = backup_dir / f"{contract.project}.ini.before-run"
    artifacts: list[dict[str, Any]] = []
    remote_sources: dict[str, str] = {}

    _append_staging_artifact(
        artifacts,
        remote_sources,
        kind="full_ini",
        local_path=contract.inputs.full_ini,
        remote_path=ini_dir / contract.inputs.full_ini.name,
    )
    for section_plan in manifest.get("sections", []):
        section = str(section_plan["section"])
        split_path = Path(str(section_plan["split_ini"]))
        _append_staging_artifact(
            artifacts,
            remote_sources,
            kind="split_ini",
            local_path=split_path,
            remote_path=ini_dir / split_path.name,
            section=section,
        )
    for section_plan in manifest.get("sections", []):
        section = str(section_plan["section"])
        config_path = Path(str(section_plan["config_path"]))
        _append_staging_artifact(
            artifacts,
            remote_sources,
            kind="config",
            local_path=config_path,
            remote_path=config_dir / config_path.name,
            section=section,
        )
    for section_plan in manifest.get("sections", []):
        section = str(section_plan["section"])
        runner_path = Path(str(section_plan["runner_path"]))
        _append_staging_artifact(
            artifacts,
            remote_sources,
            kind="runner",
            local_path=runner_path,
            remote_path=scripts_dir / runner_path.name,
            section=section,
        )
    for section_plan in manifest.get("sections", []):
        section = str(section_plan["section"])
        for dependency in section_plan.get("script_dependencies", []):
            dependency_path = contract.inputs.runner_dir / str(dependency)
            _append_staging_artifact(
                artifacts,
                remote_sources,
                kind="dependency",
                local_path=dependency_path,
                remote_path=scripts_dir / dependency_path.name,
                section=section,
            )

    return {
        "transport": "ssh-powershell",
        "workspace": str(workspace),
        "directories": [
            str(workspace),
            str(scripts_dir),
            str(config_dir),
            str(ini_dir),
            str(run_output),
            str(backup_dir),
        ],
        "runtime_ini": contract.target.runtime_ini,
        "backup_ini": str(backup_ini),
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }


def stage_remote_bundle(
    contract: PostIniContract,
    manifest: Mapping[str, Any],
    transport: Any,
) -> dict[str, Any]:
    """Create target workspace, back up runtime INI, and upload artifacts."""

    plan = build_remote_staging_plan(contract, manifest)
    try:
        probe = dict(transport.probe())
    except Exception as exc:
        raise RemoteStageError(
            RemoteStageErrorCode.CONNECTION_FAILED,
            f"remote connectivity probe failed: {exc}",
        ) from exc

    try:
        transport.ensure_directories(plan["directories"])
    except Exception as exc:
        raise RemoteStageError(
            RemoteStageErrorCode.DIRECTORY_SETUP_FAILED,
            f"remote directory setup failed: {exc}",
            remote_path=plan["workspace"],
        ) from exc

    runtime_ini = str(plan["runtime_ini"])
    backup_ini = str(plan["backup_ini"])
    try:
        original_exists = bool(transport.backup_if_exists(runtime_ini, backup_ini))
    except Exception as exc:
        raise RemoteStageError(
            RemoteStageErrorCode.BACKUP_FAILED,
            f"runtime INI backup failed: {exc}",
            remote_path=runtime_ini,
        ) from exc

    backup_result: dict[str, Any] = {
        "runtime_ini": runtime_ini,
        "backup_ini": backup_ini if original_exists else None,
        "original_exists": original_exists,
        "sha256": None,
        "hash_verified": False,
    }
    if original_exists:
        try:
            runtime_hash = str(transport.remote_sha256(runtime_ini)).strip().lower()
            backup_hash = str(transport.remote_sha256(backup_ini)).strip().lower()
        except Exception as exc:
            raise RemoteStageError(
                RemoteStageErrorCode.HASH_READ_FAILED,
                f"cannot read runtime/backup INI hash: {exc}",
                remote_path=backup_ini,
            ) from exc
        if runtime_hash != backup_hash:
            raise RemoteStageError(
                RemoteStageErrorCode.HASH_MISMATCH,
                "runtime INI backup hash mismatch",
                remote_path=backup_ini,
                artifact_kind="runtime_ini_backup",
            )
        backup_result["sha256"] = backup_hash
        backup_result["hash_verified"] = True

    uploaded: list[dict[str, Any]] = []
    for artifact in plan["artifacts"]:
        local_path = Path(str(artifact["local_path"]))
        remote_path = str(artifact["remote_path"])
        kind = str(artifact["kind"])
        try:
            transport.upload(local_path, remote_path)
        except Exception as exc:
            raise RemoteStageError(
                RemoteStageErrorCode.UPLOAD_FAILED,
                f"artifact upload failed: {exc}",
                remote_path=remote_path,
                artifact_kind=kind,
            ) from exc
        uploaded.append(dict(artifact))

    return {
        "status": "PASS",
        "probe": probe,
        "workspace": plan["workspace"],
        "directories": plan["directories"],
        "backup": backup_result,
        "uploaded_count": len(uploaded),
        "artifacts": uploaded,
    }


def build_execution_manifest(
    contract: PostIniContract, *, write_tests: bool = True
) -> dict[str, Any]:
    """Build a deterministic, side-effect-free dry-run execution manifest.

    ``write_tests=False`` (CLI ``--no-write-tests``) passes no opt-in switch to
    any runner, so the run is read-only.
    """

    generated = set(contract.inputs.generated_sections)
    for section in contract.inputs.generated_sections:
        if section not in SECTION_REGISTRY_BY_NAME:
            raise ContractError(
                ContractErrorCode.GENERATED_SECTION_NOT_REGISTERED,
                f"GENERATED section is not registered: {section}",
                path=contract.inputs.section_matrix,
                section=section,
            )

    planned_entries = [
        entry for entry in SECTION_REGISTRY if entry.section in generated
    ]
    for entry in planned_entries:
        missing_dependencies = [
            dependency
            for dependency in entry.section_dependencies
            if dependency not in generated
        ]
        if missing_dependencies:
            raise ContractError(
                ContractErrorCode.SECTION_DEPENDENCY_MISSING,
                (
                    f"GENERATED section {entry.section} is missing dependencies: "
                    + ", ".join(missing_dependencies)
                ),
                path=contract.inputs.section_matrix,
                section=entry.section,
            )

    sections: list[dict[str, Any]] = []
    for sequence, entry in enumerate(planned_entries, start=1):
        sections.append(
            {
                "sequence": sequence,
                "section": entry.section,
                "matrix_status": "GENERATED",
                "config_path": str(
                    contract.inputs.config_dir
                    / entry.config_template.format(model=contract.project)
                ),
                "runner_path": str(contract.inputs.runner_dir / entry.runner),
                "split_ini": str(contract.inputs.split_inis[entry.section]),
                "report_prefix": entry.report_prefix,
                "execution_tier": entry.execution_tier.value,
                "difficulty_rank": entry.difficulty_rank,
                "difficulty_reason": entry.difficulty_reason,
                "script_dependencies": list(entry.script_dependencies),
                "section_dependencies": list(entry.section_dependencies),
                "available_opt_in_switches": list(entry.opt_in_switches),
                "enabled_switches": list(entry.default_switches) if write_tests else [],
                "extra_path_arguments": list(entry.extra_path_arguments),
            }
        )

    enabled_switches = sorted(
        {switch for entry in SECTION_REGISTRY for switch in entry.default_switches}
        if write_tests
        else set()
    )
    disabled_switches = sorted(
        {
            switch
            for entry in SECTION_REGISTRY
            for switch in entry.opt_in_switches
            if switch not in enabled_switches
        }
    )
    contract_payload = contract.to_dict()
    manifest = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "contract_schema_version": contract.schema_version,
        "project": contract.project,
        "run_id": contract.run_id,
        "mode": "dry-run",
        "inputs": contract_payload["inputs"],
        "target": contract_payload["target"],
        "sections": sections,
        "safety_policy": {
            "default": WRITE_POLICY if write_tests else READ_ONLY_POLICY,
            "enabled_opt_in_switches": enabled_switches,
            "disabled_opt_in_switches": disabled_switches,
        },
    }
    manifest["staging_plan"] = build_remote_staging_plan(contract, manifest)
    return manifest


def prepare_section_configs(contract: PostIniContract) -> dict[str, Any]:
    """Materialize deterministic Machine-B configs from post-INI artifacts."""

    try:
        result = build_section_configs(
            matrix_path=contract.inputs.section_matrix,
            output_dir=contract.inputs.config_dir,
            model=contract.project,
            prune_stale=True,
        )
    except BuildError as exc:
        raise ContractError(
            ContractErrorCode.CONFIG_BUILD_FAILED,
            f"section config build failed: {exc}",
            path=contract.inputs.section_matrix,
        ) from exc

    expected_sections = set(contract.inputs.generated_sections)
    built_sections = set(result["generated_sections"])
    if built_sections != expected_sections:
        missing = sorted(expected_sections - built_sections)
        unexpected = sorted(built_sections - expected_sections)
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unexpected:
            details.append("unexpected=" + ",".join(unexpected))
        raise ContractError(
            ContractErrorCode.CONFIG_BUILD_FAILED,
            "section config build does not match contract: " + "; ".join(details),
            path=contract.inputs.section_matrix,
        )

    config_paths = {
        entry.section: result["config_paths"][entry.section]
        for entry in SECTION_REGISTRY
        if entry.section in built_sections
    }
    return {
        "status": "PASS",
        "builder": "build_machineB_section_configs.build_section_configs",
        "matrix_path": result["matrix_path"],
        "output_dir": result["output_dir"],
        "section_count": len(config_paths),
        "generated_sections": list(config_paths),
        "config_paths": config_paths,
        "removed_stale_configs": result["removed_stale_configs"],
    }


def run_local_preflight(
    contract: PostIniContract, manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Validate local artifacts required by a manifest without writing files."""

    runner_paths: set[Path] = set()
    dependency_paths: set[Path] = set()
    sections = manifest.get("sections", [])
    for section_plan in sections:
        section = str(section_plan["section"])
        config_path = Path(str(section_plan["config_path"]))
        if not config_path.is_file():
            raise ContractError(
                ContractErrorCode.CONFIG_FILE_MISSING,
                f"missing section config: {config_path}",
                path=config_path,
                section=section,
            )
        try:
            config_data = json.loads(config_path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ContractError(
                ContractErrorCode.CONFIG_INVALID_JSON,
                f"invalid section config JSON: {config_path}: {exc}",
                path=config_path,
                section=section,
            ) from exc
        if not isinstance(config_data, dict):
            raise ContractError(
                ContractErrorCode.CONFIG_INVALID_JSON,
                f"section config JSON root must be an object: {config_path}",
                path=config_path,
                section=section,
            )

        runner_path = Path(str(section_plan["runner_path"]))
        if not runner_path.is_file():
            raise ContractError(
                ContractErrorCode.RUNNER_FILE_MISSING,
                f"missing section runner: {runner_path}",
                path=runner_path,
                section=section,
            )
        runner_paths.add(runner_path)

        for dependency in section_plan.get("script_dependencies", []):
            dependency_path = contract.inputs.runner_dir / str(dependency)
            if not dependency_path.is_file():
                raise ContractError(
                    ContractErrorCode.SCRIPT_DEPENDENCY_MISSING,
                    f"missing script dependency: {dependency_path}",
                    path=dependency_path,
                    section=section,
                )
            dependency_paths.add(dependency_path)

    return {
        "status": "PASS",
        "section_count": len(sections),
        "config_count": len(sections),
        "runner_count": len(runner_paths),
        "dependency_count": len(dependency_paths),
    }


def write_dry_run_manifest(contract: PostIniContract, *, write_tests: bool = True) -> Path:
    """Build configs, run local preflight, and atomically write dry-run manifest."""

    config_build = prepare_section_configs(contract)
    manifest = build_execution_manifest(contract, write_tests=write_tests)
    manifest["config_build"] = config_build
    manifest["preflight"] = run_local_preflight(contract, manifest)
    contract.outputs.run_root.mkdir(parents=True, exist_ok=True)
    temporary_path = contract.outputs.manifest.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    temporary_path.replace(contract.outputs.manifest)
    return contract.outputs.manifest


class RuntimePhaseError(RuntimeError):
    """Raised when deployment, reload, readiness, execution, or rollback fails."""

    def __init__(self, phase: str, message: str, *, details: Mapping[str, Any] | None = None):
        super().__init__(message)
        self.phase = phase
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {"phase": self.phase, "message": str(self), "details": self.details}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _command_payload(result: Any) -> dict[str, Any]:
    return {
        "exit_code": int(getattr(result, "exit_code", getattr(result, "returncode", 1))),
        "stdout": str(getattr(result, "stdout", "") or ""),
        "stderr": str(getattr(result, "stderr", "") or ""),
    }


def _readiness_payload(device: Mapping[str, Any]) -> dict[str, Any]:
    count = int(device.get("count", 0))
    status = str(device.get("status", ""))
    problem = int(device.get("problem", -1))
    ready = count == 1 and status.upper() == "OK" and problem == 0
    return {
        "ready": ready,
        "count": count,
        "status": status,
        "problem": problem,
        "instance_id": device.get("instance_id"),
    }


def activate_runtime_ini(
    contract: PostIniContract,
    staging_result: Mapping[str, Any],
    transport: Any,
) -> dict[str, Any]:
    """P7: close SusiDemo4, deploy the full INI once, reload once, then gate on PnP."""

    full_ini_artifacts = [
        item for item in staging_result.get("artifacts", []) if item.get("kind") == "full_ini"
    ]
    if len(full_ini_artifacts) != 1:
        raise RuntimePhaseError(
            "DEPLOY",
            f"expected exactly one staged full_ini artifact, got {len(full_ini_artifacts)}",
        )
    staged_ini = str(full_ini_artifacts[0]["remote_path"])
    if not transport.remote_file_exists(staged_ini):
        raise RuntimePhaseError("DEPLOY", f"staged full INI is missing: {staged_ini}")
    if not transport.remote_file_exists(contract.target.reload_bat):
        raise RuntimePhaseError(
            "RELOAD", f"reload BAT is missing: {contract.target.reload_bat}"
        )

    started_at = _utc_now()
    try:
        closed_processes = int(transport.close_process("SusiDemo4"))
        transport.copy_remote_file(staged_ini, contract.target.runtime_ini)
    except Exception as exc:
        raise RuntimePhaseError("DEPLOY", f"runtime INI deployment failed: {exc}") from exc

    try:
        command = _command_payload(transport.invoke_batch(contract.target.reload_bat))
    except Exception as exc:
        raise RuntimePhaseError("RELOAD", f"reload command failed: {exc}") from exc

    contract.outputs.run_root.mkdir(parents=True, exist_ok=True)
    contract.outputs.reload_log.write_text(
        command["stdout"] + ("\nSTDERR:\n" + command["stderr"] if command["stderr"] else ""),
        encoding="utf-8",
    )
    if command["exit_code"] != 0:
        raise RuntimePhaseError("RELOAD", "validation reload returned non-zero", details=command)

    try:
        readiness = _readiness_payload(dict(transport.query_susi_device()))
    except Exception as exc:
        raise RuntimePhaseError("READINESS", f"SUSI4 readiness query failed: {exc}") from exc
    if not readiness["ready"]:
        raise RuntimePhaseError(
            "READINESS", "SUSI4 device is not uniquely healthy", details=readiness
        )

    return {
        "status": "PASS",
        "source": staged_ini,
        "destination": contract.target.runtime_ini,
        "closed_susi_demo_processes": closed_processes,
        "deployed": True,
        "reload_attempt_count": 1,
        "command": command,
        "readiness": readiness,
        "started_at": started_at,
        "completed_at": _utc_now(),
    }


def _remote_runner_path(contract: PostIniContract, section_plan: Mapping[str, Any]) -> str:
    configured = str(section_plan["runner_path"])
    scripts_dir = getattr(contract.target, "scripts_dir", None)
    return str(PureWindowsPath(scripts_dir) / Path(configured).name) if scripts_dir else configured


def _remote_config_path(contract: PostIniContract, section_plan: Mapping[str, Any]) -> str:
    configured = str(section_plan["config_path"])
    config_dir = getattr(contract.target, "config_dir", None)
    return str(PureWindowsPath(config_dir) / Path(configured).name) if config_dir else configured


_SW_LAYERS = ("L1_configuration", "L2_capability", "L3_api", "L4_readback")
_LAYER_DONE = frozenset({"PASS", "N_A", "NOT_REQUIRED"})
# Failures that belong to phase 2 (fixture / functional / DQA) and do not
# affect the phase 1 SW API verdict.
_PHASE2_ONLY_FAILURES = ("FAIL_FIXTURE", "FAIL_FUNCTIONAL", "FAIL_DQA")
# Sections whose L5 "functional" step is the reversible set/readback itself, so
# it belongs to phase 1: a write failure fails, a write not run is CONDITIONAL.
_WRITE_PATH_SECTIONS = frozenset({"VGA.Backlight", "VGA.Brightness", "GPIO", "StorageArea"})

PHASE2_RECOMMENDATIONS: Mapping[str, str] = MappingProxyType({
    "HWM.Voltage": "Apply load stimulus and confirm each rail reading tracks the change.",
    "HWM.Temperature": "Apply thermal stimulus and confirm readings change accordingly.",
    "HWM.Fan": "Attach fans and confirm RPM follows HWM.Fan.Control PWM changes.",
    "HWM.Fan.Control": "Attach fans and confirm RPM rises with PWM (expected delta >= 200 RPM).",
    "HWM.CaseOpen": "Open/close the chassis intrusion switch and confirm the state changes.",
    "HWM.Current": "Apply load and confirm current readings track the change.",
    "I2C": "Connect the approved fixture (0xAC/0xAE) and validate write/read transactions.",
    "SMBus": "Connect the legacy QA fixture and rerun with -EnableFixtureTest for write/read compare.",
    "WDT": "Let the watchdog expire under a recovery harness and confirm the target resets (reboots the target).",
    "ThermalProtect": "Validate SHUTDOWN/THROTTLE event triggering under thermal stimulus.",
})


def _normalize_report_verdict(report: Mapping[str, Any]) -> tuple[str, str | None, str | None]:
    """Phase 1 verdict: PASS when every SW API layer (L1-L4) actually passed.

    Fixture, stimulus and DQA gaps (L5) do not lower the verdict; they are
    reported as phase 2 recommendations instead. A failed restore (L6) fails.
    """

    result = str(report.get("result", "ERROR_REPORT_RESULT_MISSING"))
    sw_verdict = report.get("sw_verdict")
    dqa_verdict = report.get("dqa_verdict")
    upper = result.upper()
    layers = report.get("validation_layers")
    if isinstance(layers, Mapping) and all(name in layers for name in _SW_LAYERS):
        sw_states = [str(layers[name]).upper() for name in _SW_LAYERS]
        if str(report.get("category")) in _WRITE_PATH_SECTIONS:
            sw_states.append(str(layers.get("L5_functional", "")).upper())
        recovery = str(layers.get("L6_recovery", "")).upper()
        if any(state.startswith("FAIL") for state in sw_states) or recovery.startswith("FAIL"):
            verdict = "FAIL"
        elif upper.startswith("FAIL") and (
            not upper.startswith(_PHASE2_ONLY_FAILURES)
            or str(report.get("category")) in _WRITE_PATH_SECTIONS
        ):
            verdict = "FAIL"
        elif all(state in _LAYER_DONE for state in sw_states):
            verdict = "PASS"
        else:
            verdict = "CONDITIONAL"
    elif str(sw_verdict).upper() == "FAIL_SW" or upper.startswith("FAIL"):
        verdict = "FAIL"
    elif upper.startswith(("CONDITIONAL", "PENDING", "BLOCKED", "N_A")):
        verdict = "CONDITIONAL"
    elif upper.startswith("PASS") or str(sw_verdict).upper() == "PASS_SW":
        verdict = "PASS"
    else:
        verdict = "ERROR"
    return verdict, str(sw_verdict) if sw_verdict is not None else None, str(dqa_verdict) if dqa_verdict is not None else None


def _gpio_suspect_pins(contract: Any, report: Mapping[str, Any]) -> list[dict[str, Any]]:
    """GPIO keys whose bit is missing from the capability mask, with trace evidence.

    A partial mask means the route is right but these pins' traced group/bit is
    probably wrong (often the adjacent pin); they are reported, never changed.
    """

    coverage = gpio_capability_coverage(report)
    if not any(item["missing"] for item in coverage.values()):
        return []
    inputs = getattr(contract, "inputs", None)
    project = getattr(contract, "project", "")
    try:
        config = json.loads(
            (Path(inputs.config_dir) / f"{project}_gpio.json").read_text(encoding="utf-8-sig")
        )
    except Exception:
        config = {}
    try:
        trace = json.loads(
            (Path(inputs.case_dir) / f"{project}-gpio-trace.json").read_text(encoding="utf-8")
        )
    except Exception:
        trace = {}
    banks = config.get("banks") if isinstance(config.get("banks"), dict) else {}
    bank_names = {
        meta.get("bank_number"): name for name, meta in banks.items() if isinstance(meta, dict)
    }
    trace_by_key = {
        str(item.get("report_name")): item
        for item in (trace.get("items") or [])
        if isinstance(item, dict)
    }
    pins: list[dict[str, Any]] = []
    channels = config.get("channels") if isinstance(config.get("channels"), dict) else {}
    for key, channel in channels.items():
        if not isinstance(channel, dict):
            continue
        bank = bank_names.get(channel.get("bank"))
        bank_coverage = coverage.get(str(bank))
        try:
            bitmask = int(str(channel.get("bank_bitmask")), 0)
        except ValueError:
            continue
        if not bank_coverage or not bank_coverage["missing"] & bitmask:
            continue
        traced = trace_by_key.get(str(key), {})
        pins.append({
            "key": key,
            "bank": bank,
            "bit": bitmask.bit_length() - 1,
            "signal": traced.get("signal"),
            "function_label": traced.get("function_label"),
            "group": traced.get("group", channel.get("tuple_group")),
            "pin": traced.get("bit", channel.get("tuple_pin")),
            "package_pin": traced.get("package_pin"),
        })
    return pins


def _gpio_partial_mask_reason(report: Mapping[str, Any], pins: list[dict[str, Any]]) -> str:
    coverage = gpio_capability_coverage(report)
    masks = "; ".join(
        f"{bank} expected 0x{item['expected']:08X}, supported 0x{item['supported']:08X}"
        for bank, item in coverage.items()
        if item["missing"]
    )
    total = sum(bin(item["expected"]).count("1") for item in coverage.values())
    described = "; ".join(
        f"{pin['key']} = {pin.get('signal') or '?'} -> {pin.get('function_label') or '?'} "
        f"(group {pin.get('group')}, bit {pin.get('pin')})"
        for pin in pins
    )
    return (
        f"Route OK, but {len(pins)} of {total} GPIO not supported by the capability mask "
        f"({masks}): {described}. Check these schematic traces (often the adjacent pin)."
    )


def _section_reason(report: Mapping[str, Any], verdict: str) -> tuple[str, str | None]:
    """Return (headline reason, phase 2 observation).

    A runner may report ``sw_reason`` for the phase 1 result; when the section
    passes phase 1 that becomes the headline and the runner's own reason (e.g. a
    fan RPM finding) is kept as the phase 2 observation.
    """

    reason = str(report.get("reason", ""))
    sw_reason = str(report.get("sw_reason") or "").strip()
    if verdict == "PASS" and sw_reason and sw_reason != reason:
        return sw_reason, reason
    return reason, None


def _phase2_recommendation(item: Mapping[str, Any]) -> str | None:
    """Hardware/fixture/DQA follow-up for a section whose phase 1 did not fail."""

    if item.get("verdict") not in {"PASS", "CONDITIONAL"}:
        return None
    if str(item.get("section")) in _WRITE_PATH_SECTIONS:
        return None
    layers = item.get("validation_layers")
    functional = ""
    open_layers = False
    if isinstance(layers, Mapping):
        functional = str(layers.get("L5_functional", "")).upper()
        open_layers = any(
            str(layers.get(name, "")).upper() not in _LAYER_DONE
            for name in ("L5_functional", "L6_recovery")
            if name in layers
        )
    if item.get("verdict") != "CONDITIONAL" and not open_layers:
        return None
    text = PHASE2_RECOMMENDATIONS.get(str(item.get("section"))) or str(item.get("reason", "")).strip()
    observation = str(item.get("phase2_observation") or "").strip()
    if observation:
        text = f"{text} Observed: {observation}"
    elif functional.startswith("FAIL"):
        text = f"{text} Observed: {str(item.get('reason', '')).strip()}"
    return text or None


def execute_validation_sections(
    contract: PostIniContract,
    manifest: Mapping[str, Any],
    transport: Any,
    *,
    timeout_seconds: int = 180,
    remote_report_dir: str | None = None,
    local_report_dir: Path | None = None,
) -> list[dict[str, Any]]:
    """P8/P9: execute manifest runners in order and collect one fresh JSON report each."""

    report_dir = remote_report_dir or str(
        PureWindowsPath(contract.target.run_output) / "reports"
    )
    local_reports = local_report_dir or contract.outputs.reports_dir
    transport.ensure_directories([report_dir])
    local_reports.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    by_section: dict[str, dict[str, Any]] = {}
    fan_config_path: str | None = None

    for section_plan in manifest.get("sections", []):
        section = str(section_plan["section"])
        dependencies = [str(item) for item in section_plan.get("section_dependencies", [])]
        failed_dependencies = [
            dependency
            for dependency in dependencies
            if dependency not in by_section
            or by_section[dependency].get("execution_status") != "COMPLETED"
            or by_section[dependency].get("verdict") in {"FAIL", "ERROR"}
        ]
        if failed_dependencies:
            blocked = {
                "sequence": section_plan.get("sequence"),
                "section": section,
                "execution_status": "BLOCKED_DEPENDENCY",
                "verdict": "BLOCKED",
                "runner_exit_code": None,
                "reason": "dependency failed: " + ", ".join(failed_dependencies),
                "dependencies": failed_dependencies,
                "warnings": [],
            }
            results.append(blocked)
            by_section[section] = blocked
            continue

        runner_path = _remote_runner_path(contract, section_plan)
        config_path = _remote_config_path(contract, section_plan)
        if section == "HWM.Fan":
            fan_config_path = config_path
        prefix = str(section_plan["report_prefix"])
        pattern = f"{prefix}_*.json"
        before = set(transport.list_remote_files(report_dir, pattern))
        arguments: dict[str, Any] = {
            "ConfigPath": config_path,
            "IniPath": contract.target.runtime_ini,
            "OutDir": report_dir,
        }
        enabled_switches = {
            str(value)
            for value in section_plan.get("enabled_switches", [])
            if str(value) in set(section_plan.get("available_opt_in_switches", []))
        }
        for switch in enabled_switches:
            arguments[switch] = True
        if section == "HWM.Fan.Control":
            if not fan_config_path:
                blocked = {
                    "sequence": section_plan.get("sequence"),
                    "section": section,
                    "execution_status": "BLOCKED_DEPENDENCY",
                    "verdict": "BLOCKED",
                    "runner_exit_code": None,
                    "reason": "HWM.Fan config path is unavailable",
                    "warnings": [],
                }
                results.append(blocked)
                by_section[section] = blocked
                continue
            arguments["FanConfigPath"] = fan_config_path
            arguments["FanIniPath"] = contract.target.runtime_ini

        started_at = _utc_now()
        try:
            command_result = transport.invoke_powershell_file(
                runner_path, arguments, timeout_seconds=timeout_seconds
            )
            command = _command_payload(command_result)
        except Exception as exc:
            errored = {
                "sequence": section_plan.get("sequence"),
                "section": section,
                "execution_status": "ORCHESTRATOR_ERROR",
                "verdict": "ERROR",
                "runner_exit_code": None,
                "reason": f"runner invocation failed: {exc}",
                "warnings": [],
                "started_at": started_at,
                "completed_at": _utc_now(),
            }
            results.append(errored)
            by_section[section] = errored
            continue

        after = set(transport.list_remote_files(report_dir, pattern))
        fresh_reports = sorted(after - before)
        if len(fresh_reports) != 1:
            errored = {
                "sequence": section_plan.get("sequence"),
                "section": section,
                "execution_status": "ORCHESTRATOR_ERROR",
                "verdict": "ERROR",
                "runner_exit_code": command["exit_code"],
                "runner_stdout": command["stdout"],
                "runner_stderr": command["stderr"],
                "reason": f"expected one new {pattern} report, got {len(fresh_reports)}",
                "warnings": [],
                "started_at": started_at,
                "completed_at": _utc_now(),
            }
            results.append(errored)
            by_section[section] = errored
            continue

        remote_report = fresh_reports[0]
        local_report = local_reports / PureWindowsPath(remote_report).name
        try:
            transport.download(remote_report, local_report)
            report = json.loads(local_report.read_text(encoding="utf-8-sig"))
            if not isinstance(report, dict):
                raise ValueError("report root must be a JSON object")
            if str(report.get("category")) != section:
                raise ValueError(
                    f"report category {report.get('category')!r} does not match {section!r}"
                )
            verdict, sw_verdict, dqa_verdict = _normalize_report_verdict(report)
        except Exception as exc:
            errored = {
                "sequence": section_plan.get("sequence"),
                "section": section,
                "execution_status": "ORCHESTRATOR_ERROR",
                "verdict": "ERROR",
                "runner_exit_code": command["exit_code"],
                "reason": f"report collection/parsing failed: {exc}",
                "remote_report_path": remote_report,
                "warnings": [],
                "started_at": started_at,
                "completed_at": _utc_now(),
            }
            results.append(errored)
            by_section[section] = errored
            continue

        warnings: list[str] = []
        expected_exit = 1 if verdict == "FAIL" else 0
        if command["exit_code"] != expected_exit:
            warnings.append(
                f"runner exit code {command['exit_code']} disagrees with report verdict {verdict}"
            )
        gpio_pins = _gpio_suspect_pins(contract, report) if section == "GPIO" else []
        completed = {
            "sequence": section_plan.get("sequence"),
            "section": section,
            "execution_status": "COMPLETED",
            "verdict": verdict,
            "result": report.get("result"),
            "reason": (
                _gpio_partial_mask_reason(report, gpio_pins)
                if gpio_pins
                else _section_reason(report, verdict)[0]
            ),
            "phase2_observation": _section_reason(report, verdict)[1],
            "gpio_suspect_pins": gpio_pins,
            "channel_summary": report.get("channel_summary"),
            "validation_layers": report.get("validation_layers"),
            "sw_verdict": sw_verdict,
            "dqa_verdict": dqa_verdict,
            "runner_exit_code": command["exit_code"],
            "runner_stdout": command["stdout"],
            "runner_stderr": command["stderr"],
            "remote_report_path": remote_report,
            "local_report_path": str(local_report),
            "warnings": warnings,
            "started_at": started_at,
            "completed_at": _utc_now(),
        }
        results.append(completed)
        by_section[section] = completed

    return results


def run_fallback_attempt(
    contract: PostIniContract,
    manifest: Mapping[str, Any],
    transport: Any,
    candidate: FallbackAttemptCandidate,
    *,
    timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Deploy one candidate full INI and run exactly one safe-default section."""

    matches = [
        item
        for item in manifest.get("sections", [])
        if str(item.get("section")) == candidate.section
    ]
    if len(matches) != 1:
        raise RuntimePhaseError(
            "FALLBACK_PLAN",
            f"expected exactly one manifest entry for {candidate.section}, got {len(matches)}",
        )
    section_plan = dict(matches[0])
    dependencies = [str(item) for item in section_plan.get("section_dependencies", [])]
    if dependencies:
        raise RuntimePhaseError(
            "FALLBACK_PLAN",
            f"dependent section is not eligible for isolated fallback: {candidate.section}",
            details={"dependencies": dependencies},
        )

    safe_section = re.sub(r"[^a-z0-9]+", "_", candidate.section.lower()).strip("_")
    attempt_name = f"attempt-{candidate.index:03d}"
    local_root = contract.outputs.run_root / "fallback" / safe_section / attempt_name
    local_reports = local_root / "reports"
    local_ini = local_root / f"{contract.project}-candidate.ini"
    remote_root = str(
        PureWindowsPath(contract.target.run_output)
        / "fallback"
        / safe_section
        / attempt_name
    )
    remote_reports = str(PureWindowsPath(remote_root) / "reports")
    remote_ini = str(PureWindowsPath(remote_root) / local_ini.name)

    local_root.mkdir(parents=True, exist_ok=True)
    local_ini.write_text(candidate.ini_text, encoding="utf-8")
    local_hash = hashlib.sha256(local_ini.read_bytes()).hexdigest()
    if local_hash != candidate.ini_sha256:
        raise RuntimePhaseError(
            "FALLBACK_PLAN",
            "candidate INI hash does not match candidate payload",
            details={"expected": candidate.ini_sha256, "actual": local_hash},
        )

    deploy: dict[str, Any] = {
        "status": "NOT_RUN",
        "local_path": str(local_ini),
        "remote_path": remote_ini,
        "candidate_ini_sha256": candidate.ini_sha256,
    }
    reload_result: dict[str, Any] = {"status": "NOT_RUN"}
    try:
        transport.ensure_directories([remote_root, remote_reports])
        transport.upload(local_ini, remote_ini)
        remote_hash = str(transport.remote_sha256(remote_ini)).strip().lower()
        if remote_hash != candidate.ini_sha256:
            raise RuntimeError(
                f"candidate upload hash mismatch: expected={candidate.ini_sha256}, actual={remote_hash}"
            )
        closed = int(transport.close_process("SusiDemo4"))
        transport.copy_remote_file(remote_ini, contract.target.runtime_ini)
        deploy.update(
            {
                "status": "PASS",
                "remote_sha256": remote_hash,
                "closed_susi_demo_processes": closed,
                "destination": contract.target.runtime_ini,
            }
        )
    except Exception as exc:
        deploy.update({"status": "FAIL", "error": str(exc)})
        return {"deploy": deploy, "reload": reload_result, "report": None}

    try:
        command = _command_payload(transport.invoke_batch(contract.target.reload_bat))
        if command["exit_code"] != 0:
            reload_result = {
                "status": "FAIL",
                "command": command,
                "readiness": {"ready": False},
                "error": f"fallback reload returned {command['exit_code']}",
            }
            return {"deploy": deploy, "reload": reload_result, "report": None}
        readiness = _readiness_payload(dict(transport.query_susi_device()))
        reload_result = {
            "status": "PASS" if readiness["ready"] else "FAIL",
            "command": command,
            "readiness": readiness,
        }
        if not readiness["ready"]:
            reload_result["error"] = "SUSI4 is not uniquely healthy after fallback reload"
            return {"deploy": deploy, "reload": reload_result, "report": None}
    except Exception as exc:
        reload_result = {
            "status": "FAIL",
            "readiness": {"ready": False},
            "error": str(exc),
        }
        return {"deploy": deploy, "reload": reload_result, "report": None}

    results = execute_validation_sections(
        contract,
        {"sections": [section_plan]},
        transport,
        timeout_seconds=timeout_seconds,
        remote_report_dir=remote_reports,
        local_report_dir=local_reports,
    )
    result = results[0]
    if result.get("execution_status") != "COMPLETED":
        return {
            "deploy": deploy,
            "reload": reload_result,
            "report": None,
            "runner_result": result,
        }
    report_path = Path(str(result["local_report_path"]))
    report = json.loads(report_path.read_text(encoding="utf-8-sig"))
    return {
        "deploy": deploy,
        "reload": reload_result,
        "report": report,
        "report_path": str(report_path),
        "report_sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        "runner_result": result,
    }


def deploy_fallback_ini_only(
    contract: PostIniContract,
    transport: Any,
    *,
    ini_text: str,
    label: str,
) -> dict[str, Any]:
    """Restore the last accepted generated INI without running a section."""

    safe_label = re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_") or "restore"
    local_root = contract.outputs.run_root / "fallback" / "restore"
    local_path = local_root / f"{safe_label}.ini"
    remote_root = str(PureWindowsPath(contract.target.run_output) / "fallback" / "restore")
    remote_path = str(PureWindowsPath(remote_root) / local_path.name)
    local_root.mkdir(parents=True, exist_ok=True)
    local_path.write_text(ini_text, encoding="utf-8")
    digest = hashlib.sha256(local_path.read_bytes()).hexdigest()
    result: dict[str, Any] = {
        "status": "NOT_RUN",
        "local_path": str(local_path),
        "remote_path": remote_path,
        "ini_sha256": digest,
    }
    try:
        transport.ensure_directories([remote_root])
        transport.upload(local_path, remote_path)
        remote_hash = str(transport.remote_sha256(remote_path)).strip().lower()
        if remote_hash != digest:
            raise RuntimeError("fallback restore upload hash mismatch")
        transport.close_process("SusiDemo4")
        transport.copy_remote_file(remote_path, contract.target.runtime_ini)
        command = _command_payload(transport.invoke_batch(contract.target.reload_bat))
        if command["exit_code"] != 0:
            raise RuntimeError(f"fallback restore reload returned {command['exit_code']}")
        readiness = _readiness_payload(dict(transport.query_susi_device()))
        if not readiness["ready"]:
            raise RuntimeError("SUSI4 is not healthy after fallback restore")
        result.update(
            {
                "status": "PASS",
                "remote_sha256": remote_hash,
                "command": command,
                "readiness": readiness,
            }
        )
    except Exception as exc:
        result.update({"status": "FAIL", "error": str(exc)})
    return result


def converge_fallback_artifacts(
    contract: PostIniContract,
    convergence: Mapping[str, Any],
) -> dict[str, Any]:
    """Persist successes and rebuild canonical INI/matrix/section JSON artifacts."""

    override_path = contract.inputs.case_dir / f"{contract.project}-config-overrides.json"
    successful = [
        str(item.get("section"))
        for item in convergence.get("sections", [])
        if isinstance(item, Mapping) and item.get("status") == "CONVERGED"
    ]
    if not successful:
        return {
            "status": "NO_CHANGE",
            "override_path": str(override_path),
            "sections": [],
        }
    override = persist_project_route_overrides(
        override_path,
        project=contract.project,
        run_id=contract.run_id,
        convergence=convergence,
    )

    applied = apply_project_route_overrides(
        project=contract.project,
        override_path=override_path,
        full_ini_path=contract.inputs.full_ini,
        section_paths=contract.inputs.split_inis,
    )
    matrix = json.loads(contract.inputs.section_matrix.read_text(encoding="utf-8"))
    matrix_sections = matrix.get("sections") if isinstance(matrix, dict) else None
    if not isinstance(matrix_sections, list):
        raise RuntimePhaseError("FORMAL_CONVERGENCE", "section matrix has no sections list")
    details = applied.get("details") if isinstance(applied.get("details"), Mapping) else {}
    for entry in matrix_sections:
        if not isinstance(entry, dict) or entry.get("section") not in successful:
            continue
        section = str(entry["section"])
        section_detail = details.get(section, {}) if isinstance(details, Mapping) else {}
        entry["project_route_override"] = {
            "status": "APPLIED",
            "path": str(override_path),
            "sha256": applied.get("sha256"),
            **(dict(section_detail) if isinstance(section_detail, Mapping) else {}),
        }
        route = str(entry.get("route") or "")
        if "PROJECT_ROUTE_OVERRIDE" not in route:
            entry["route"] = f"{route}+PROJECT_ROUTE_OVERRIDE" if route else "PROJECT_ROUTE_OVERRIDE"
    temporary = contract.inputs.section_matrix.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(matrix, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(contract.inputs.section_matrix)
    config_build = prepare_section_configs(contract)
    return {
        "status": "APPLIED",
        "override_path": str(override_path),
        "override": override,
        "applied": applied,
        "config_build": config_build,
        "sections": successful,
    }


def run_fallback_convergence_session(
    contract: PostIniContract,
    manifest: Mapping[str, Any],
    staging_result: Mapping[str, Any],
    transport: Any,
    plan: Mapping[str, object],
    *,
    runner_timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Execute a validated plan, save evidence, converge artifacts, then recover target."""

    baseline_text = contract.inputs.full_ini.read_text(encoding="utf-8")
    restore_counter = 0

    def attempt_runner(candidate: FallbackAttemptCandidate) -> Mapping[str, object]:
        return run_fallback_attempt(
            contract,
            manifest,
            transport,
            candidate,
            timeout_seconds=runner_timeout_seconds,
        )

    def restore_runner(section: str, ini_text: str) -> Mapping[str, object]:
        nonlocal restore_counter
        restore_counter += 1
        return deploy_fallback_ini_only(
            contract,
            transport,
            ini_text=ini_text,
            label=f"{restore_counter:03d}-{section}",
        )

    convergence: dict[str, Any] = {
        "schema_version": "machineb.fallback_convergence.v1",
        "status": "NOT_RUN",
        "sections": [],
    }
    try:
        convergence = dict(
            execute_fallback_plan(
                baseline_ini_text=baseline_text,
                plan=plan,
                attempt_runner=attempt_runner,
                restore_runner=restore_runner,
            )
        )
        effective_text = str(convergence.pop("effective_ini_text"))
        effective_path = contract.outputs.run_root / "fallback-effective-full.ini"
        effective_path.write_text(effective_text, encoding="utf-8")
        convergence["effective_ini_path"] = str(effective_path)
        convergence["formal_convergence"] = converge_fallback_artifacts(
            contract, convergence
        )
    except Exception as exc:
        convergence = {
            "schema_version": "machineb.fallback_convergence.v1",
            "status": "ORCHESTRATOR_ERROR",
            "sections": [],
            "error": str(exc),
        }
    finally:
        convergence["final_runtime_recovery"] = rollback_runtime_ini(
            contract, staging_result, transport
        )
    if convergence["final_runtime_recovery"].get("status") != "PASS":
        convergence["status"] = "ORCHESTRATOR_ERROR"
    output = contract.outputs.run_root / "fallback-convergence.json"
    temporary = output.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(convergence, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(output)
    convergence["output_path"] = str(output)
    return convergence


def rollback_runtime_ini(
    contract: PostIniContract,
    staging_result: Mapping[str, Any],
    transport: Any,
) -> dict[str, Any]:
    """P10: restore the pre-run INI state and reload it as a separate recovery action."""

    started_at = _utc_now()
    backup = dict(staging_result.get("backup", {}))
    original_exists = bool(backup.get("original_exists", False))
    try:
        if original_exists:
            backup_ini = backup.get("backup_ini")
            if not backup_ini or not transport.remote_file_exists(str(backup_ini)):
                raise RuntimeError("runtime INI backup is unavailable")
            transport.copy_remote_file(str(backup_ini), contract.target.runtime_ini)
            action = "RESTORED_BACKUP"
        else:
            transport.remove_remote_file_if_exists(contract.target.runtime_ini)
            action = "REMOVED_DEPLOYED_INI"
        command = _command_payload(transport.invoke_batch(contract.target.reload_bat))
        if command["exit_code"] != 0:
            raise RuntimeError(f"rollback reload returned {command['exit_code']}")
        readiness = _readiness_payload(dict(transport.query_susi_device()))
        if not readiness["ready"]:
            raise RuntimeError("SUSI4 is not healthy after rollback reload")
    except Exception as exc:
        return {
            "status": "FAIL",
            "action": locals().get("action", "NONE"),
            "error": str(exc),
            "started_at": started_at,
            "completed_at": _utc_now(),
        }
    return {
        "status": "PASS",
        "action": action,
        "recovery_reload_attempt_count": 1,
        "command": command,
        "readiness": readiness,
        "started_at": started_at,
        "completed_at": _utc_now(),
    }


def run_activated_validation(
    contract: PostIniContract,
    manifest: Mapping[str, Any],
    staging_result: Mapping[str, Any],
    transport: Any,
    *,
    runner_timeout_seconds: int = 180,
) -> dict[str, Any]:
    """Coordinate P7-P10 after P6 staging and always attempt INI rollback."""

    started_at = _utc_now()
    runtime_ini: dict[str, Any] = {"status": "NOT_DEPLOYED"}
    reload_result: dict[str, Any] = {"status": "NOT_RUN"}
    section_results: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    warnings: list[str] = []
    try:
        activation = activate_runtime_ini(contract, staging_result, transport)
        runtime_ini = {
            key: activation[key]
            for key in (
                "status",
                "source",
                "destination",
                "deployed",
                "closed_susi_demo_processes",
                "started_at",
                "completed_at",
            )
        }
        reload_result = {
            "status": "PASS",
            "attempt_count": activation["reload_attempt_count"],
            "command": activation["command"],
            "readiness": activation["readiness"],
        }
        section_results = execute_validation_sections(
            contract,
            manifest,
            transport,
            timeout_seconds=runner_timeout_seconds,
        )
    except RuntimePhaseError as exc:
        errors.append(exc.to_dict())
        if exc.phase == "DEPLOY":
            runtime_ini = {"status": "FAIL", "error": str(exc)}
        else:
            runtime_ini = {
                "status": "DEPLOYED_UNCONFIRMED",
                "destination": contract.target.runtime_ini,
            }
            reload_result = {
                "status": "FAIL",
                "phase": exc.phase,
                "error": str(exc),
                "details": exc.details,
            }
    except Exception as exc:
        errors.append({"phase": "ORCHESTRATOR", "message": str(exc), "details": {}})
    finally:
        rollback = rollback_runtime_ini(contract, staging_result, transport)

    return write_validation_summary(
        contract,
        runtime_ini=runtime_ini,
        reload_result=reload_result,
        section_results=section_results,
        rollback=rollback,
        errors=errors,
        warnings=warnings,
        started_at=started_at,
        write_tests_enabled=(
            manifest.get("safety_policy", {}).get("default") != READ_ONLY_POLICY
            if isinstance(manifest.get("safety_policy"), Mapping)
            else True
        ),
    )


def write_validation_summary(
    contract: PostIniContract,
    *,
    runtime_ini: Mapping[str, Any],
    reload_result: Mapping[str, Any],
    section_results: list[dict[str, Any]],
    rollback: Mapping[str, Any],
    errors: list[Any],
    warnings: list[Any],
    started_at: str | None = None,
    fallback: Mapping[str, Any] | None = None,
    write_tests_enabled: bool = True,
) -> dict[str, Any]:
    """P9/P10: write deterministic machine-readable and text summaries."""

    section_has_error = any(
        item.get("execution_status") == "ORCHESTRATOR_ERROR" or item.get("verdict") == "ERROR"
        for item in section_results
    )
    section_has_failure = any(item.get("verdict") == "FAIL" for item in section_results)
    if errors or rollback.get("status") != "PASS" or section_has_error:
        status, exit_code = "ORCHESTRATOR_ERROR", 2
    elif section_has_failure:
        status, exit_code = "SECTION_FAIL", 1
    else:
        status, exit_code = "PASS", 0
    summary = {
        "schema_version": SUMMARY_SCHEMA_VERSION,
        "contract_schema_version": contract.schema_version,
        "project": contract.project,
        "run_id": contract.run_id,
        "status": status,
        "exit_code": exit_code,
        "timestamps": {"started_at": started_at or _utc_now(), "completed_at": _utc_now()},
        "runtime_ini": dict(runtime_ini),
        "reload": dict(reload_result),
        "sections": section_results,
        "rollback": dict(rollback),
        "warnings": list(warnings),
        "errors": list(errors),
        "write_tests_enabled": write_tests_enabled,
    }
    phase2 = [
        {"section": item.get("section"), "recommendation": note}
        for item in section_results
        for note in [_phase2_recommendation(item)]
        if note
    ]
    summary["phase2_recommendations"] = phase2
    if fallback is not None:
        summary["fallback"] = dict(fallback)
    contract.outputs.run_root.mkdir(parents=True, exist_ok=True)
    temporary = contract.outputs.summary_json.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(contract.outputs.summary_json)
    verdict_counts: dict[str, int] = {}
    for item in section_results:
        tag = str(item.get("verdict"))
        verdict_counts[tag] = verdict_counts.get(tag, 0) + 1
    counts = " / ".join(
        f"{verdict_counts[tag]} {tag}"
        for tag in sorted(verdict_counts, key=lambda tag: _VERDICT_ORDER.get(tag, len(_VERDICT_ORDER)))
    )
    lines = [
        f"Project: {contract.project}",
        f"Run ID: {contract.run_id}",
        f"Status: [{status}]  (exit code {exit_code})",
        (
            "Scope: Phase 1 - SW API read/write path (set, read back, restore)"
            if write_tests_enabled
            else "Scope: Phase 1 - SW API READ-ONLY (write tests disabled by --no-write-tests)"
        ),
        f"Result: {counts or 'no sections'}",
        "",
        "Sections:",
    ]
    tag_width = max((len(str(item.get("verdict"))) + 2 for item in section_results), default=0)
    name_width = max((len(str(item.get("section"))) for item in section_results), default=0)
    indent = " " * (2 + tag_width + 2 + name_width + 2)
    for item in section_results:
        tag = f"[{item.get('verdict')}]"
        detail = str(item.get("reason", "")).strip()
        execution_status = item.get("execution_status")
        if execution_status != "COMPLETED":
            detail = f"(execution {execution_status}) {detail}".strip()
        channel_summary = item.get("channel_summary")
        if isinstance(channel_summary, Mapping):
            total = int(channel_summary.get("total", 0))
            passed = int(channel_summary.get("passed", 0))
            failed = int(channel_summary.get("failed", 0))
            if total > 0 and passed > 0 and failed > 0:
                passed_names = ", ".join(
                    str(value) for value in channel_summary.get("passed_channels", [])
                )
                failed_names = ", ".join(
                    f"{value.get('channel')}({value.get('status')})"
                    if isinstance(value, Mapping)
                    else str(value)
                    for value in channel_summary.get("failed_channels", [])
                )
                detail = (
                    f"PARTIAL_FAIL {passed}/{total} passed; "
                    f"passed=[{passed_names}]; failed=[{failed_names}]"
                )
        lines.append(
            f"  {tag:<{tag_width}}  {str(item.get('section')):<{name_width}}  {detail}".rstrip()
        )
        note = _fallback_note(item.get("fallback"))
        if note:
            lines.append(f"{indent}{note}")
    lines.extend(["", f"Rollback: [{rollback.get('status')}]"])
    run_root = contract.outputs.run_root
    if fallback is not None:
        lines.extend([
            "",
            f"Fallback: [{fallback.get('status')}]",
            f"Fallback runtime recovery: [{fallback.get('final_runtime_recovery')}]",
        ])
        if fallback.get("error"):
            lines.append(f"Fallback error: {fallback.get('error')}")
    if phase2:
        lines.extend(["", "Phase 2 recommendations (hardware / fixture / DQA):"])
        lines.extend(f"- {entry['section']}: {entry['recommendation']}" for entry in phase2)
    lines.extend(["", "Details:", "Section reports:"])
    for item in section_results:
        report_path = item.get("local_report_path")
        if report_path:
            lines.append(f"- {item.get('section')}: {_display_path(report_path, run_root)}")
    lines.append(f"Manifest: {_display_path(contract.outputs.manifest, run_root)}")
    if fallback is not None:
        for label, key in (
            ("Fallback attempts and evidence", "convergence_path"),
            ("Pre-fallback summary", "baseline_summary_path"),
            ("Final full INI", "final_full_ini"),
            ("Project route overrides", "override_path"),
        ):
            if fallback.get(key):
                lines.append(f"{label}: {_display_path(fallback[key], run_root)}")
    contract.outputs.summary_text.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return summary


_VERDICT_ORDER = {"FAIL": 0, "ERROR": 1, "CONDITIONAL": 2, "PASS": 3}


def _display_path(path: Any, run_root: Path) -> str:
    candidate = Path(str(path))
    try:
        return candidate.relative_to(run_root).as_posix()
    except ValueError:
        return str(candidate)


def _fallback_note(fallback: Any) -> str:
    if not isinstance(fallback, Mapping):
        return ""
    status = fallback.get("status")
    count = fallback.get("attempt_count", 0)
    if status == "CONVERGED":
        route = f"{fallback.get('route_field') or 'route'}={fallback.get('selected_route')}"
        return (
            f"fallback CONVERGED on attempt {fallback.get('winning_attempt')}/{count}, "
            f"{route} (baseline was {fallback.get('baseline_verdict')})"
        )
    return f"fallback {status} after {count} attempt(s)"


def finalize_summary_after_fallback(
    contract: PostIniContract,
    convergence: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Rewrite the run summary so it reflects fallback results as the final verdict.

    The pre-fallback summary is preserved once as ``*.baseline.json/.txt`` and is
    always the merge source, so re-finalizing is idempotent.
    """

    outputs = contract.outputs
    baseline_json = outputs.summary_json.with_name(
        outputs.summary_json.name.replace(".json", ".baseline.json")
    )
    baseline_text = outputs.summary_text.with_name(
        outputs.summary_text.name.replace(".txt", ".baseline.txt")
    )
    if not baseline_json.is_file():
        if not outputs.summary_json.is_file():
            return None
        baseline_json.write_bytes(outputs.summary_json.read_bytes())
        if outputs.summary_text.is_file():
            baseline_text.write_bytes(outputs.summary_text.read_bytes())
    baseline = json.loads(baseline_json.read_text(encoding="utf-8"))

    formal = convergence.get("formal_convergence")
    formal = formal if isinstance(formal, Mapping) else {}
    override = formal.get("override")
    override_sections = override.get("sections") if isinstance(override, Mapping) else None
    override_sections = override_sections if isinstance(override_sections, Mapping) else {}
    by_section = {
        str(item.get("section")): item
        for item in convergence.get("sections", [])
        if isinstance(item, Mapping)
    }

    sections: list[dict[str, Any]] = []
    for original in baseline.get("sections", []):
        item = dict(original)
        converged = by_section.get(str(item.get("section")))
        if converged is not None:
            attempts = [a for a in converged.get("attempts", []) if isinstance(a, Mapping)]
            note: dict[str, Any] = {
                "status": converged.get("status"),
                "attempt_count": len(attempts),
                "baseline_verdict": item.get("verdict"),
                "baseline_result": item.get("result"),
                "baseline_report_path": item.get("local_report_path"),
            }
            winner = next((a for a in reversed(attempts) if a.get("success")), None)
            if converged.get("status") == "CONVERGED" and winner is not None:
                section_override = override_sections.get(str(item.get("section")))
                note.update({
                    "selected_route": converged.get("selected_route"),
                    "route_field": section_override.get("route_field")
                    if isinstance(section_override, Mapping) else None,
                    "winning_attempt": winner.get("index"),
                    "changed_keys": list(winner.get("changed_keys", [])),
                })
                report_path = str(winner.get("report_path") or "")
                try:
                    report = json.loads(Path(report_path).read_text(encoding="utf-8-sig"))
                    verdict, sw_verdict, dqa_verdict = _normalize_report_verdict(report)
                    gpio_pins = (
                        _gpio_suspect_pins(contract, report)
                        if str(item.get("section")) == "GPIO" else []
                    )
                    item.update({
                        "verdict": verdict,
                        "result": report.get("result"),
                        "gpio_suspect_pins": gpio_pins,
                        "reason": (
                            _gpio_partial_mask_reason(report, gpio_pins)
                            if gpio_pins
                            else _section_reason(report, verdict)[0]
                        ),
                        "phase2_observation": _section_reason(report, verdict)[1],
                        "channel_summary": report.get("channel_summary"),
                        "validation_layers": report.get("validation_layers"),
                        "sw_verdict": sw_verdict,
                        "dqa_verdict": dqa_verdict,
                    })
                except Exception as exc:
                    item.update({
                        "verdict": "ERROR",
                        "reason": f"fallback report unreadable: {exc}",
                    })
                item["local_report_path"] = report_path
                item.pop("remote_report_path", None)
            item["fallback"] = note
        sections.append(item)

    errors = list(baseline.get("errors", []))
    if convergence.get("status") == "ORCHESTRATOR_ERROR":
        errors.append({
            "phase": "FALLBACK",
            "message": str(convergence.get("error") or "fallback orchestration failed"),
            "details": {},
        })
    recovery = convergence.get("final_runtime_recovery")
    fallback_summary = {
        "status": convergence.get("status"),
        "final_runtime_recovery": recovery.get("status") if isinstance(recovery, Mapping) else None,
        "error": convergence.get("error"),
        "convergence_path": convergence.get("output_path"),
        "baseline_summary_path": str(baseline_text if baseline_text.is_file() else baseline_json),
        "final_full_ini": str(contract.inputs.full_ini),
        "override_path": formal.get("override_path") if formal.get("status") == "APPLIED" else None,
        "sections": sorted(by_section),
    }
    timestamps = baseline.get("timestamps")
    return write_validation_summary(
        contract,
        runtime_ini=baseline.get("runtime_ini", {}),
        reload_result=baseline.get("reload", {}),
        section_results=sections,
        rollback=baseline.get("rollback", {}),
        errors=errors,
        warnings=list(baseline.get("warnings", [])),
        started_at=timestamps.get("started_at") if isinstance(timestamps, Mapping) else None,
        fallback=fallback_summary,
        write_tests_enabled=bool(baseline.get("write_tests_enabled", True)),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic Machine-B full-validation orchestrator"
    )
    parser.add_argument("--project", required=True)
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--run-id", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Run local preflight and write manifest.json without target access",
    )
    mode.add_argument(
        "--execute",
        action="store_true",
        help="Stage, activate, run safe validation, collect reports, and roll back",
    )
    mode.add_argument(
        "--converge",
        action="store_true",
        help="Execute a validated fallback-plan.json with isolated section attempts",
    )
    parser.add_argument("--fallback-plan", help="Required with --converge")
    parser.add_argument("--host", help="Machine-B SSH host (required with --execute/--converge)")
    parser.add_argument("--user", help="Machine-B SSH user (required with --execute/--converge)")
    parser.add_argument("--runner-timeout-seconds", type=int, default=180)
    parser.add_argument(
        "--no-write-tests",
        action="store_true",
        help="Read-only run: pass no set/write/control switch to any runner",
    )
    args = parser.parse_args(argv)
    if (args.execute or args.converge) and (not args.host or not args.user):
        parser.error("--host and --user are required with --execute/--converge")
    if args.converge and not args.fallback_plan:
        parser.error("--fallback-plan is required with --converge")
    if args.runner_timeout_seconds <= 0:
        parser.error("--runner-timeout-seconds must be positive")

    try:
        contract = build_post_ini_contract(
            project=args.project,
            repo_root=args.repo_root,
            run_id=args.run_id,
        )
        if args.dry_run:
            manifest_path = write_dry_run_manifest(
                contract, write_tests=not args.no_write_tests
            )
            print(manifest_path)
            return 0

        config_build = prepare_section_configs(contract)
        manifest = build_execution_manifest(contract, write_tests=not args.no_write_tests)
        manifest["mode"] = "converge" if args.converge else "execute"
        manifest["config_build"] = config_build
        manifest["preflight"] = run_local_preflight(contract, manifest)

        fallback_plan: dict[str, object] | None = None
        if args.converge:
            registry = load_candidate_registry(
                Path(args.repo_root).expanduser().resolve()
                / "targetB_task"
                / "machineB_validation"
                / "fallback_candidate_registry.json"
            )
            fallback_plan = load_and_validate_fallback_plan(
                Path(args.fallback_plan).expanduser().resolve(),
                expected_project=contract.project,
                expected_run_id=contract.run_id,
                expected_full_ini=contract.inputs.full_ini,
                registry=registry,
                applicable_sections=set(contract.inputs.generated_sections),
            )
            planned_sections = fallback_plan.get("sections")
            manifest["fallback_plan"] = {
                "path": str(Path(args.fallback_plan).expanduser().resolve()),
                "sha256": hashlib.sha256(
                    Path(args.fallback_plan).expanduser().resolve().read_bytes()
                ).hexdigest(),
                "section_count": len(planned_sections) if isinstance(planned_sections, list) else 0,
            }

        contract.outputs.run_root.mkdir(parents=True, exist_ok=True)
        temporary_path = contract.outputs.manifest.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        temporary_path.replace(contract.outputs.manifest)

        from machineb_transport import SshPowerShellTransport

        transport = SshPowerShellTransport(host=args.host, user=args.user)
        staging = stage_remote_bundle(contract, manifest, transport)
        if args.converge:
            if fallback_plan is None:
                raise RuntimePhaseError("PLAN", "validated fallback plan is missing")
            convergence = run_fallback_convergence_session(
                contract,
                manifest,
                staging,
                transport,
                fallback_plan,
                runner_timeout_seconds=args.runner_timeout_seconds,
            )
            final_summary = finalize_summary_after_fallback(contract, convergence)
            if final_summary is not None:
                output_path = contract.outputs.summary_json
                exit_code = int(final_summary["exit_code"])
            else:
                output_path = Path(str(convergence["output_path"]))
                status = str(convergence.get("status") or "ORCHESTRATOR_ERROR")
                exit_code = 0 if status in {"CONVERGED", "NO_ACTION"} else (2 if status == "ORCHESTRATOR_ERROR" else 1)
        else:
            summary = run_activated_validation(
                contract,
                manifest,
                staging,
                transport,
                runner_timeout_seconds=args.runner_timeout_seconds,
            )
            output_path = contract.outputs.summary_json
            exit_code = int(summary["exit_code"])
    except (ContractError, RemoteStageError) as exc:
        print(json.dumps(exc.to_dict(), sort_keys=True), file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {"phase": "ORCHESTRATOR", "message": str(exc)}, sort_keys=True
            ),
            file=sys.stderr,
        )
        return 2

    print(output_path)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
