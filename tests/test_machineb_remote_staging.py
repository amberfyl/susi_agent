import base64
import hashlib
import json
import subprocess
import tempfile
import unittest
from pathlib import Path, PureWindowsPath


class FakeRemoteTransport:
    def __init__(self, *, original_exists=True, upload_hash_override=None):
        self.original_exists = original_exists
        self.upload_hash_override = upload_hash_override or {}
        self.calls = []
        self.hashes = {}

    def probe(self):
        self.calls.append(("probe",))
        return {"computer_name": "MACHINE-B", "identity": "test\\susiaa"}

    def ensure_directories(self, paths):
        paths = list(paths)
        self.calls.append(("ensure_directories", paths))

    def backup_if_exists(self, source, destination):
        self.calls.append(("backup_if_exists", source, destination))
        if not self.original_exists:
            return False
        self.hashes[source] = "a" * 64
        self.hashes[destination] = "a" * 64
        return True

    def upload(self, local_path, remote_path):
        self.calls.append(("upload", str(local_path), remote_path))
        local_hash = hashlib.sha256(Path(local_path).read_bytes()).hexdigest()
        self.hashes[remote_path] = self.upload_hash_override.get(
            remote_path, local_hash
        )

    def remote_sha256(self, remote_path):
        self.calls.append(("remote_sha256", remote_path))
        return self.hashes[remote_path]


class MachineBRemoteStagingTests(unittest.TestCase):
    def _build_contract_and_manifest(self):
        from run_machineB_full_validation import (
            build_execution_manifest,
            build_post_ini_contract,
            prepare_section_configs,
        )

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        project = "BOARD"
        case_dir = root / "CASES" / project
        runner_dir = root / "targetB_task" / "machineB_validation"
        case_dir.mkdir(parents=True)
        runner_dir.mkdir(parents=True)
        source_text = "[SMBus]\nChannel1=1,0,0,0,\n"
        (case_dir / f"{project}-pre.ini").write_text(
            "[Information]\nPlatformName=BOARD\n" + source_text,
            encoding="utf-8",
        )
        split_ini = case_dir / f"{project}_SMBus.ini"
        split_ini.write_text(source_text, encoding="utf-8")
        (case_dir / f"{project}-section-matrix.json").write_text(
            json.dumps(
                {
                    "project": project,
                    "sections": [
                        {
                            "section": "SMBus",
                            "status": "GENERATED",
                            "path": str(split_ini),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        (runner_dir / "run_smbus_validation.ps1").write_text(
            ". ./common_susi.ps1\n", encoding="utf-8"
        )
        (runner_dir / "common_susi.ps1").write_text(
            "# shared helper\n", encoding="utf-8"
        )
        contract = build_post_ini_contract(
            project=project, repo_root=root, run_id="p6-run"
        )
        prepare_section_configs(contract)
        manifest = build_execution_manifest(contract)
        return contract, manifest

    def test_staging_plan_contains_canonical_directories_backup_and_artifacts(self):
        from run_machineB_full_validation import build_remote_staging_plan

        contract, manifest = self._build_contract_and_manifest()

        plan = build_remote_staging_plan(contract, manifest)

        run_output = PureWindowsPath(contract.target.run_output)
        self.assertEqual(
            plan["directories"],
            [
                contract.target.workspace,
                contract.target.scripts_dir,
                contract.target.config_dir,
                contract.target.ini_dir,
                contract.target.run_output,
                str(run_output / "backup"),
            ],
        )
        self.assertEqual(plan["runtime_ini"], contract.target.runtime_ini)
        self.assertEqual(
            plan["backup_ini"],
            str(run_output / "backup" / "BOARD.ini.before-run"),
        )
        self.assertEqual(
            [artifact["kind"] for artifact in plan["artifacts"]],
            ["full_ini", "split_ini", "config", "runner", "dependency"],
        )
        self.assertEqual(plan["artifact_count"], 5)
        self.assertEqual(
            len({artifact["remote_path"] for artifact in plan["artifacts"]}), 5
        )

    def test_stage_remote_bundle_backs_up_before_upload_without_upload_hash_checks(self):
        from run_machineB_full_validation import stage_remote_bundle

        contract, manifest = self._build_contract_and_manifest()
        transport = FakeRemoteTransport(original_exists=True)

        result = stage_remote_bundle(contract, manifest, transport)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["probe"]["computer_name"], "MACHINE-B")
        self.assertTrue(result["backup"]["original_exists"])
        self.assertTrue(result["backup"]["hash_verified"])
        self.assertEqual(result["uploaded_count"], 5)
        self.assertTrue(
            all("sha256" not in item for item in result["artifacts"])
        )
        self.assertTrue(
            all("hash_verified" not in item for item in result["artifacts"])
        )
        call_names = [call[0] for call in transport.calls]
        self.assertLess(
            call_names.index("backup_if_exists"), call_names.index("upload")
        )
        self.assertEqual(call_names.count("remote_sha256"), 2)

    def test_stage_remote_bundle_allows_missing_original_runtime_ini(self):
        from run_machineB_full_validation import stage_remote_bundle

        contract, manifest = self._build_contract_and_manifest()
        transport = FakeRemoteTransport(original_exists=False)

        result = stage_remote_bundle(contract, manifest, transport)

        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["backup"]["original_exists"])
        self.assertIsNone(result["backup"]["sha256"])
        self.assertFalse(result["backup"]["hash_verified"])
        self.assertEqual(result["uploaded_count"], 5)
        self.assertFalse(
            any(call[0] == "remote_sha256" for call in transport.calls)
        )

    def test_stage_remote_bundle_does_not_compare_uploaded_artifact_hashes(self):
        from run_machineB_full_validation import (
            build_remote_staging_plan,
            stage_remote_bundle,
        )

        contract, manifest = self._build_contract_and_manifest()
        plan = build_remote_staging_plan(contract, manifest)
        first_remote_path = plan["artifacts"][0]["remote_path"]
        transport = FakeRemoteTransport(
            original_exists=False,
            upload_hash_override={first_remote_path: "0" * 64},
        )

        result = stage_remote_bundle(contract, manifest, transport)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["uploaded_count"], 5)
        self.assertFalse(
            any(call == ("remote_sha256", first_remote_path) for call in transport.calls)
        )

    def test_dry_run_manifest_records_staging_plan_without_transport(self):
        from run_machineB_full_validation import (
            EXECUTION_MANIFEST_SCHEMA,
            write_dry_run_manifest,
        )

        contract, _ = self._build_contract_and_manifest()

        manifest_path = write_dry_run_manifest(contract)

        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertIn("staging_plan", payload)
        self.assertEqual(payload["staging_plan"]["artifact_count"], 5)
        self.assertIn("staging_plan", EXECUTION_MANIFEST_SCHEMA["required"])
        self.assertEqual(
            EXECUTION_MANIFEST_SCHEMA["properties"]["staging_plan"]["type"],
            "object",
        )


class SshPowerShellTransportTests(unittest.TestCase):
    def test_transport_builds_noninteractive_ssh_scp_and_parses_results(self):
        from machineb_transport import SshPowerShellTransport

        calls = []

        def fake_runner(command):
            calls.append(command)
            if command[0] == "scp":
                return subprocess.CompletedProcess(command, 0, "", "")
            encoded = command[command.index("-EncodedCommand") + 1]
            script = base64.b64decode(encoded).decode("utf-16le")
            if "COMPUTER_NAME=" in script:
                stdout = "COMPUTER_NAME=MACHINE-B\nIDENTITY=test\\susiaa\n"
            elif "ORIGINAL_EXISTS=" in script:
                stdout = "ORIGINAL_EXISTS=1\n"
            elif "Get-FileHash" in script:
                stdout = "SHA256=" + ("b" * 64) + "\n"
            else:
                stdout = ""
            return subprocess.CompletedProcess(command, 0, stdout, "")

        transport = SshPowerShellTransport(
            host="192.0.2.10",
            user="susiaa",
            connect_timeout_seconds=7,
            command_runner=fake_runner,
        )
        self.assertEqual(calls, [])

        probe = transport.probe()
        transport.ensure_directories(
            [r"C:\Users\susiaa\Desktop\verify\BOARD", r"C:\path with ' quote"]
        )
        original_exists = transport.backup_if_exists(
            r"C:\Windows\SUSI\BOARD.ini",
            r"C:\Users\susiaa\Desktop\verify\BOARD\out\run\backup\BOARD.ini",
        )
        digest = transport.remote_sha256(r"C:\Windows\SUSI\BOARD.ini")
        with tempfile.TemporaryDirectory() as temp_dir:
            local_path = Path(temp_dir) / "BOARD.ini"
            local_path.write_text("[Information]\n", encoding="utf-8")
            transport.upload(
                local_path, r"C:\Users\susiaa\Desktop\verify\BOARD\ini\BOARD.ini"
            )

        self.assertEqual(probe["computer_name"], "MACHINE-B")
        self.assertEqual(probe["identity"], r"test\susiaa")
        directory_encoded = calls[1][calls[1].index("-EncodedCommand") + 1]
        directory_script = base64.b64decode(directory_encoded).decode("utf-16le")
        self.assertIn("New-Item -ItemType Directory -Path $path -Force", directory_script)
        self.assertNotIn("New-Item -ItemType Directory -LiteralPath", directory_script)
        self.assertTrue(original_exists)
        self.assertEqual(digest, "b" * 64)
        self.assertEqual(len(calls), 5)
        for command in calls[:4]:
            self.assertEqual(command[0], "ssh")
            self.assertIn("BatchMode=yes", command)
            self.assertIn("ConnectTimeout=7", command)
            self.assertIn("susiaa@192.0.2.10", command)
        scp_command = calls[-1]
        self.assertEqual(scp_command[0:2], ["scp", "-O"])
        self.assertIn("BatchMode=yes", scp_command)
        self.assertTrue(
            scp_command[-1].endswith(
                ":/C:/Users/susiaa/Desktop/verify/BOARD/ini/BOARD.ini"
            )
        )

    def test_runtime_transport_primitives_preserve_runner_exit_and_escape_arguments(self):
        from machineb_transport import SshPowerShellTransport

        calls = []

        def fake_runner(command):
            calls.append(command)
            if command[0] == "scp":
                if command[-1].endswith("local-report.json"):
                    Path(command[-1]).write_text("{}", encoding="utf-8")
                return subprocess.CompletedProcess(command, 0, "", "")
            encoded = command[command.index("-EncodedCommand") + 1]
            script = base64.b64decode(encoded).decode("utf-16le")
            if "FILE_EXISTS=" in script:
                stdout = "FILE_EXISTS=1\n"
            elif "DEVICE_COUNT=" in script:
                stdout = "DEVICE_COUNT=1\nSTATUS=OK\nPROBLEM=CM_PROB_NONE\nINSTANCE_ID=ROOT\\\\SYSTEM\\\\0001\n"
            elif "FILE_PATH=" in script:
                stdout = "FILE_PATH=C:\\verify\\out\\gpio_20260930_120000.json\n"
            elif "& $scriptPath @params" in script:
                return subprocess.CompletedProcess(command, 1, "runner failed as designed", "")
            elif "REMOVED=" in script:
                stdout = "REMOVED=1\n"
            elif "CLOSED_COUNT=" in script:
                stdout = "CLOSED_COUNT=2\n"
            else:
                stdout = ""
            return subprocess.CompletedProcess(command, 0, stdout, "")

        transport = SshPowerShellTransport(
            host="192.0.2.10", user="susiaa", command_runner=fake_runner
        )
        self.assertTrue(transport.remote_file_exists(r"C:\path with ' quote\file.ini"))
        transport.copy_remote_file(r"C:\source.ini", r"C:\dest.ini")
        self.assertTrue(transport.remove_remote_file_if_exists(r"C:\dest.ini"))
        self.assertEqual(transport.close_process("SusiDemo4"), 2)
        device = transport.query_susi_device()
        self.assertEqual(device["status"], "OK")
        self.assertEqual(device["problem"], 0)
        self.assertEqual(device["problem_raw"], "CM_PROB_NONE")
        files = transport.list_remote_files(r"C:\verify\out", "gpio_*.json")
        self.assertEqual(files, [r"C:\verify\out\gpio_20260930_120000.json"])
        result = transport.invoke_powershell_file(
            r"C:\scripts\runner.ps1",
            {
                "ConfigPath": r"C:\path with ' quote\config.json",
                "EnableFunctionalTest": True,
                "DllDirs": [r"C:\SUSI", r"D:\SDK"],
            },
            timeout_seconds=9,
        )
        self.assertEqual(result.exit_code, 1)
        self.assertIn("runner failed", result.stdout)

        scripts = []
        for command in calls:
            if command[0] == "ssh":
                encoded = command[command.index("-EncodedCommand") + 1]
                scripts.append(base64.b64decode(encoded).decode("utf-16le"))
        invocation = next(script for script in scripts if "& $scriptPath @params" in script)
        self.assertIn("path with '' quote", invocation)
        self.assertIn("$true", invocation)
        self.assertIn("@('C:\\SUSI','D:\\SDK')", invocation)

    def test_transport_raises_on_command_failure_and_invalid_hash(self):
        from machineb_transport import RemoteTransportError, SshPowerShellTransport

        def failed_runner(command):
            return subprocess.CompletedProcess(command, 255, "", "connection refused")

        failed = SshPowerShellTransport(
            host="192.0.2.10", user="susiaa", command_runner=failed_runner
        )
        with self.assertRaises(RemoteTransportError):
            failed.probe()

        def invalid_hash_runner(command):
            return subprocess.CompletedProcess(command, 0, "SHA256=not-a-hash\n", "")

        invalid = SshPowerShellTransport(
            host="192.0.2.10", user="susiaa", command_runner=invalid_hash_runner
        )
        with self.assertRaises(RemoteTransportError):
            invalid.remote_sha256(r"C:\Windows\SUSI\BOARD.ini")


if __name__ == "__main__":
    unittest.main()
