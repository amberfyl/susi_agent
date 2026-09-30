#!/usr/bin/env python3
"""SSH + PowerShell transport primitives for Machine-B orchestration."""

from __future__ import annotations

import base64
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol, Sequence


class RemoteTransport(Protocol):
    def probe(self) -> Mapping[str, str]: ...

    def ensure_directories(self, paths: Sequence[str]) -> None: ...

    def backup_if_exists(self, source: str, destination: str) -> bool: ...

    def upload(self, local_path: Path, remote_path: str) -> None: ...

    def remote_sha256(self, remote_path: str) -> str: ...

    def remote_file_exists(self, path: str) -> bool: ...

    def copy_remote_file(self, source: str, destination: str) -> None: ...

    def remove_remote_file_if_exists(self, path: str) -> bool: ...

    def close_process(self, name: str) -> int: ...

    def invoke_batch(self, path: str) -> "RemoteCommandResult": ...

    def query_susi_device(self) -> Mapping[str, Any]: ...

    def list_remote_files(self, directory: str, pattern: str) -> Sequence[str]: ...

    def invoke_powershell_file(
        self,
        path: str,
        arguments: Mapping[str, Any],
        timeout_seconds: int | None = None,
    ) -> "RemoteCommandResult": ...

    def download(self, remote_path: str, local_path: Path) -> None: ...


@dataclass(frozen=True)
class RemoteCommandResult:
    exit_code: int
    stdout: str
    stderr: str


class RemoteTransportError(RuntimeError):
    """Raised when a concrete transport command or response is invalid."""


CommandRunner = Callable[[list[str]], subprocess.CompletedProcess[str]]


def _ps_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _ps_value(value: Any) -> str:
    if isinstance(value, bool):
        return "$true" if value else "$false"
    if isinstance(value, (list, tuple)):
        return "@(" + ",".join(_ps_value(item) for item in value) + ")"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return _ps_literal(str(value))


def _scp_remote_path(path: str) -> str:
    normalized = path.replace("\\", "/")
    if re.match(r"^[A-Za-z]:/", normalized):
        return "/" + normalized
    return normalized


class SshPowerShellTransport:
    """Non-interactive OpenSSH/SCP transport for a Windows target."""

    def __init__(
        self,
        *,
        host: str,
        user: str,
        connect_timeout_seconds: int = 10,
        command_timeout_seconds: int = 120,
        command_runner: CommandRunner | None = None,
    ) -> None:
        if not host or not user:
            raise ValueError("host and user are required")
        if connect_timeout_seconds <= 0 or command_timeout_seconds <= 0:
            raise ValueError("timeouts must be positive")
        self.host = host
        self.user = user
        self.remote = f"{user}@{host}"
        self.connect_timeout_seconds = connect_timeout_seconds
        self.command_timeout_seconds = command_timeout_seconds
        self._command_runner = command_runner

    def _options(self) -> list[str]:
        return [
            "-o",
            "BatchMode=yes",
            "-o",
            f"ConnectTimeout={self.connect_timeout_seconds}",
        ]

    def _run_unchecked(
        self, command: list[str], *, timeout_seconds: int | None = None
    ) -> subprocess.CompletedProcess[str]:
        try:
            if self._command_runner is not None:
                return self._command_runner(command)
            return subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=timeout_seconds or self.command_timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RemoteTransportError(f"transport command could not run: {exc}") from exc

    def _run(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        result = self._run_unchecked(command)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RemoteTransportError(
                f"transport command failed ({result.returncode}): {detail}"
            )
        return result

    def _powershell_command(self, script: str) -> list[str]:
        encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
        return [
            "ssh",
            *self._options(),
            self.remote,
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-EncodedCommand",
            encoded,
        ]

    def _powershell(self, script: str) -> subprocess.CompletedProcess[str]:
        return self._run(self._powershell_command(script))

    def _powershell_allow_failure(
        self, script: str, *, timeout_seconds: int | None = None
    ) -> RemoteCommandResult:
        result = self._run_unchecked(
            self._powershell_command(script), timeout_seconds=timeout_seconds
        )
        return RemoteCommandResult(
            exit_code=int(result.returncode),
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )

    def probe(self) -> dict[str, str]:
        result = self._powershell(
            "$ErrorActionPreference='Stop'; "
            "Write-Output ('COMPUTER_NAME=' + [string]$env:COMPUTERNAME); "
            "Write-Output ('IDENTITY=' + "
            "[string]([Security.Principal.WindowsIdentity]::GetCurrent().Name))"
        )
        values = self._parse_markers(result.stdout, ("COMPUTER_NAME", "IDENTITY"))
        return {
            "computer_name": values["COMPUTER_NAME"],
            "identity": values["IDENTITY"],
        }

    def ensure_directories(self, paths: Sequence[str]) -> None:
        if not paths:
            return
        literals = ",".join(_ps_literal(path) for path in paths)
        self._powershell(
            "$ErrorActionPreference='Stop'; "
            f"$paths=@({literals}); "
            "foreach($path in $paths){ "
            "New-Item -ItemType Directory -Path $path -Force | Out-Null }"
        )

    def backup_if_exists(self, source: str, destination: str) -> bool:
        result = self._powershell(
            "$ErrorActionPreference='Stop'; "
            f"$src={_ps_literal(source)}; $dst={_ps_literal(destination)}; "
            "if(Test-Path -LiteralPath $src -PathType Leaf){ "
            "Copy-Item -LiteralPath $src -Destination $dst -Force; "
            "Write-Output 'ORIGINAL_EXISTS=1' } "
            "else { Write-Output 'ORIGINAL_EXISTS=0' }"
        )
        markers = self._parse_markers(result.stdout, ("ORIGINAL_EXISTS",))
        value = markers["ORIGINAL_EXISTS"]
        if value not in {"0", "1"}:
            raise RemoteTransportError(
                f"invalid ORIGINAL_EXISTS marker from target: {value!r}"
            )
        return value == "1"

    def upload(self, local_path: Path, remote_path: str) -> None:
        local_path = Path(local_path)
        if not local_path.is_file():
            raise RemoteTransportError(f"local upload source is missing: {local_path}")
        target = f"{self.remote}:{_scp_remote_path(remote_path)}"
        self._run(["scp", "-O", *self._options(), str(local_path), target])

    def remote_sha256(self, remote_path: str) -> str:
        result = self._powershell(
            "$ErrorActionPreference='Stop'; "
            f"$hash=(Get-FileHash -LiteralPath {_ps_literal(remote_path)} "
            "-Algorithm SHA256).Hash; Write-Output ('SHA256=' + $hash)"
        )
        markers = self._parse_markers(result.stdout, ("SHA256",))
        digest = markers["SHA256"].strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise RemoteTransportError(
                f"invalid SHA256 marker from target: {digest!r}"
            )
        return digest

    def remote_file_exists(self, path: str) -> bool:
        result = self._powershell(
            "$ErrorActionPreference='Stop'; "
            f"$exists=Test-Path -LiteralPath {_ps_literal(path)} -PathType Leaf; "
            "Write-Output ('FILE_EXISTS=' + [int][bool]$exists)"
        )
        value = self._parse_markers(result.stdout, ("FILE_EXISTS",))["FILE_EXISTS"]
        if value not in {"0", "1"}:
            raise RemoteTransportError(f"invalid FILE_EXISTS marker: {value!r}")
        return value == "1"

    def copy_remote_file(self, source: str, destination: str) -> None:
        self._powershell(
            "$ErrorActionPreference='Stop'; "
            f"Copy-Item -LiteralPath {_ps_literal(source)} "
            f"-Destination {_ps_literal(destination)} -Force"
        )

    def remove_remote_file_if_exists(self, path: str) -> bool:
        result = self._powershell(
            "$ErrorActionPreference='Stop'; "
            f"$path={_ps_literal(path)}; "
            "if(Test-Path -LiteralPath $path -PathType Leaf){ "
            "Remove-Item -LiteralPath $path -Force; Write-Output 'REMOVED=1' } "
            "else { Write-Output 'REMOVED=0' }"
        )
        value = self._parse_markers(result.stdout, ("REMOVED",))["REMOVED"]
        if value not in {"0", "1"}:
            raise RemoteTransportError(f"invalid REMOVED marker: {value!r}")
        return value == "1"

    def close_process(self, name: str) -> int:
        result = self._powershell(
            "$ErrorActionPreference='Stop'; "
            f"$items=@(Get-Process -Name {_ps_literal(name)} -ErrorAction SilentlyContinue); "
            "$count=$items.Count; if($count -gt 0){$items | Stop-Process -Force}; "
            "Write-Output ('CLOSED_COUNT=' + $count)"
        )
        value = self._parse_markers(result.stdout, ("CLOSED_COUNT",))["CLOSED_COUNT"]
        try:
            return int(value)
        except ValueError as exc:
            raise RemoteTransportError(f"invalid CLOSED_COUNT marker: {value!r}") from exc

    def invoke_batch(self, path: str) -> RemoteCommandResult:
        script = (
            "$ErrorActionPreference='Continue'; "
            f"& cmd.exe /d /c call {_ps_literal(path)}; "
            "$code=$LASTEXITCODE; if($null -eq $code){$code=1}; exit $code"
        )
        return self._powershell_allow_failure(script)

    def query_susi_device(self) -> dict[str, Any]:
        result = self._powershell(
            "$ErrorActionPreference='Stop'; "
            "$devices=@(Get-PnpDevice -Class System -PresentOnly | Where-Object { "
            "$_.FriendlyName -like '*SUSI4*' -or $_.Name -like '*SUSI4*' }); "
            "Write-Output ('DEVICE_COUNT=' + $devices.Count); "
            "if($devices.Count -eq 1){$device=$devices[0]; "
            "Write-Output ('STATUS=' + [string]$device.Status); "
            "Write-Output ('PROBLEM=' + [string]$device.Problem); "
            "Write-Output ('INSTANCE_ID=' + [string]$device.InstanceId)}"
        )
        count_marker = self._parse_markers(result.stdout, ("DEVICE_COUNT",))
        try:
            count = int(count_marker["DEVICE_COUNT"])
        except ValueError as exc:
            raise RemoteTransportError("invalid DEVICE_COUNT marker") from exc
        payload: dict[str, Any] = {
            "count": count,
            "status": "",
            "problem": -1,
            "problem_raw": None,
            "instance_id": None,
        }
        if count == 1:
            markers = self._parse_markers(result.stdout, ("STATUS", "PROBLEM", "INSTANCE_ID"))
            problem_raw = markers["PROBLEM"].strip()
            problem_names = {"CM_PROB_NONE": 0, "CM_PROB_DISABLED": 22}
            try:
                problem = int(problem_raw)
            except ValueError:
                problem = problem_names.get(problem_raw.upper(), -1)
            payload.update(
                status=markers["STATUS"],
                problem=problem,
                problem_raw=problem_raw,
                instance_id=markers["INSTANCE_ID"],
            )
        return payload

    def list_remote_files(self, directory: str, pattern: str) -> list[str]:
        result = self._powershell(
            "$ErrorActionPreference='Stop'; "
            f"$items=@(Get-ChildItem -LiteralPath {_ps_literal(directory)} "
            f"-Filter {_ps_literal(pattern)} -File -ErrorAction SilentlyContinue | "
            "Sort-Object FullName); foreach($item in $items){ "
            "Write-Output ('FILE_PATH=' + $item.FullName) }"
        )
        files: list[str] = []
        for line in (result.stdout or "").splitlines():
            if line.startswith("FILE_PATH="):
                files.append(line.partition("=")[2].strip())
        return files

    def invoke_powershell_file(
        self,
        path: str,
        arguments: Mapping[str, Any],
        timeout_seconds: int | None = None,
    ) -> RemoteCommandResult:
        assignments = ";".join(
            f"{key}={_ps_value(value)}" for key, value in arguments.items()
        )
        script = (
            "$ErrorActionPreference='Continue'; "
            f"$scriptPath={_ps_literal(path)}; $params=@{{{assignments}}}; "
            "& $scriptPath @params; $code=$LASTEXITCODE; "
            "if($null -eq $code){$code=1}; exit $code"
        )
        return self._powershell_allow_failure(script, timeout_seconds=timeout_seconds)

    def download(self, remote_path: str, local_path: Path) -> None:
        local_path = Path(local_path)
        local_path.parent.mkdir(parents=True, exist_ok=True)
        source = f"{self.remote}:{_scp_remote_path(remote_path)}"
        self._run(["scp", "-O", *self._options(), source, str(local_path)])

    @staticmethod
    def _parse_markers(stdout: str, required: Sequence[str]) -> dict[str, str]:
        values: dict[str, str] = {}
        required_set = set(required)
        for line in (stdout or "").splitlines():
            key, separator, value = line.strip().partition("=")
            if separator and key in required_set:
                values[key] = value.strip()
        missing = [key for key in required if key not in values]
        if missing:
            raise RemoteTransportError(
                "missing target response marker(s): " + ", ".join(missing)
            )
        return values
