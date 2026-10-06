import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class LocalPreflightAndDryRunTests(unittest.TestCase):
    def _build_contract(self):
        from run_machineB_full_validation import build_post_ini_contract

        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        root = Path(temp_dir.name)
        project = "BOARD"
        case_dir = root / "CASES" / project
        runner_dir = root / "targetB_task" / "machineB_validation"
        config_dir = case_dir / "config"
        case_dir.mkdir(parents=True)
        runner_dir.mkdir(parents=True)
        config_dir.mkdir()

        (case_dir / f"{project}-pre.ini").write_text(
            "[Information]\nPlatformName=BOARD\n[SMBus]\nChannel1=1,0,0,0,\n",
            encoding="utf-8",
        )
        split_ini = case_dir / f"{project}_SMBus.ini"
        split_ini.write_text(
            "[SMBus]\nChannel1=1,0,0,0,\n", encoding="utf-8"
        )
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
        (config_dir / f"{project}_smbus.json").write_text(
            json.dumps({"project": project, "section": "SMBus"}),
            encoding="utf-8",
        )
        (runner_dir / "run_smbus_validation.ps1").write_text(
            ". ./common_susi.ps1\n", encoding="utf-8"
        )
        (runner_dir / "common_susi.ps1").write_text(
            "# shared helper\n", encoding="utf-8"
        )

        contract = build_post_ini_contract(
            project=project, repo_root=root, run_id="run-1"
        )
        return root, contract

    def test_preflight_passes_and_reports_checked_local_artifacts(self):
        from run_machineB_full_validation import (
            build_execution_manifest,
            run_local_preflight,
        )

        _, contract = self._build_contract()
        manifest = build_execution_manifest(contract)

        result = run_local_preflight(contract, manifest)

        self.assertEqual(result["status"], "PASS")
        self.assertEqual(result["section_count"], 1)
        self.assertEqual(result["config_count"], 1)
        self.assertEqual(result["runner_count"], 1)
        self.assertEqual(result["dependency_count"], 1)
        self.assertFalse(contract.outputs.run_root.exists())

    def test_preflight_missing_config_has_fixed_error_and_no_output(self):
        from run_machineB_full_validation import (
            ContractError,
            ContractErrorCode,
            build_execution_manifest,
            run_local_preflight,
        )

        _, contract = self._build_contract()
        (contract.inputs.config_dir / "BOARD_smbus.json").unlink()

        with self.assertRaises(ContractError) as caught:
            run_local_preflight(contract, build_execution_manifest(contract))

        self.assertEqual(caught.exception.code, ContractErrorCode.CONFIG_FILE_MISSING)
        self.assertEqual(caught.exception.section, "SMBus")
        self.assertFalse(contract.outputs.run_root.exists())

    def test_preflight_invalid_config_json_has_fixed_error(self):
        from run_machineB_full_validation import (
            ContractError,
            ContractErrorCode,
            build_execution_manifest,
            run_local_preflight,
        )

        _, contract = self._build_contract()
        config_path = contract.inputs.config_dir / "BOARD_smbus.json"
        config_path.write_text("not-json", encoding="utf-8")

        with self.assertRaises(ContractError) as caught:
            run_local_preflight(contract, build_execution_manifest(contract))

        self.assertEqual(caught.exception.code, ContractErrorCode.CONFIG_INVALID_JSON)
        self.assertEqual(caught.exception.path, config_path)

    def test_preflight_missing_runner_and_dependency_have_fixed_errors(self):
        from run_machineB_full_validation import (
            ContractError,
            ContractErrorCode,
            build_execution_manifest,
            run_local_preflight,
        )

        _, contract = self._build_contract()
        manifest = build_execution_manifest(contract)
        runner_path = contract.inputs.runner_dir / "run_smbus_validation.ps1"
        dependency_path = contract.inputs.runner_dir / "common_susi.ps1"

        runner_path.unlink()
        with self.assertRaises(ContractError) as caught:
            run_local_preflight(contract, manifest)
        self.assertEqual(caught.exception.code, ContractErrorCode.RUNNER_FILE_MISSING)

        runner_path.write_text(". ./common_susi.ps1\n", encoding="utf-8")
        dependency_path.unlink()
        with self.assertRaises(ContractError) as caught:
            run_local_preflight(contract, manifest)
        self.assertEqual(
            caught.exception.code, ContractErrorCode.SCRIPT_DEPENDENCY_MISSING
        )

    def test_write_dry_run_manifest_writes_only_manifest_after_preflight(self):
        from run_machineB_full_validation import write_dry_run_manifest

        _, contract = self._build_contract()

        manifest_path = write_dry_run_manifest(contract)

        self.assertEqual(manifest_path, contract.outputs.manifest)
        self.assertTrue(manifest_path.is_file())
        self.assertEqual(
            sorted(path.name for path in contract.outputs.run_root.iterdir()),
            ["manifest.json"],
        )
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["preflight"]["status"], "PASS")
        self.assertEqual(payload["safety_policy"]["enabled_opt_in_switches"], ["AllowControl", "EnableFunctionalTest", "EnableSetConfigTest", "EnableSetTest", "EnableStartStopTest", "EnableWriteTest"])

    def test_cli_dry_run_writes_manifest_and_returns_zero(self):
        from run_machineB_full_validation import main

        root, contract = self._build_contract()

        exit_code = main(
            [
                "--project",
                "BOARD",
                "--repo-root",
                str(root),
                "--run-id",
                "run-1",
                "--dry-run",
            ]
        )

        self.assertEqual(exit_code, 0)
        self.assertTrue(contract.outputs.manifest.is_file())


    def test_cli_execute_requires_target_and_runs_p5_through_p10(self):
        from run_machineB_full_validation import main

        root, contract = self._build_contract()
        transport = Mock()
        summary = {"exit_code": 0, "status": "PASS"}
        with (
            patch("machineb_transport.SshPowerShellTransport", return_value=transport) as constructor,
            patch("run_machineB_full_validation.stage_remote_bundle", return_value={"status": "PASS"}) as stage,
            patch("run_machineB_full_validation.run_activated_validation", return_value=summary) as run,
        ):
            exit_code = main(
                [
                    "--project", "BOARD",
                    "--repo-root", str(root),
                    "--run-id", "run-execute",
                    "--execute",
                    "--host", "192.0.2.10",
                    "--user", "susiaa",
                ]
            )

        self.assertEqual(exit_code, 0)
        constructor.assert_called_once_with(host="192.0.2.10", user="susiaa")
        self.assertEqual(stage.call_count, 1)
        self.assertEqual(run.call_count, 1)
        manifest = run.call_args.args[1]
        self.assertEqual(manifest["mode"], "execute")
        self.assertEqual(manifest["safety_policy"]["enabled_opt_in_switches"], ["AllowControl", "EnableFunctionalTest", "EnableSetConfigTest", "EnableSetTest", "EnableStartStopTest", "EnableWriteTest"])
        execute_contract = run.call_args.args[0]
        self.assertEqual(execute_contract.project, contract.project)


    def test_cli_execute_writes_a_plan_that_converge_accepts(self):
        # The plan written after --execute must pass the --converge loader for
        # the same run ID; otherwise an eligible fallback can never run.
        import shutil
        from machineb_fallback import load_and_validate_fallback_plan, load_candidate_registry
        from run_machineB_full_validation import main, write_validation_summary

        root, contract = self._build_contract()
        registry_src = Path(__file__).resolve().parents[1] / "targetB_task" / "machineB_validation" / "fallback_candidate_registry.json"
        shutil.copy(registry_src, root / "targetB_task" / "machineB_validation" / registry_src.name)

        def fake_run(run_contract, *_args, **_kwargs):
            report = run_contract.outputs.reports_dir / "smbus_1.json"
            report.parent.mkdir(parents=True, exist_ok=True)
            report.write_text("{}", encoding="utf-8")
            return write_validation_summary(
                run_contract,
                runtime_ini={"status": "PASS"},
                reload_result={"status": "PASS"},
                section_results=[{
                    "section": "SMBus",
                    "execution_status": "COMPLETED",
                    "verdict": "FAIL",
                    "reason": "all channels failed",
                    "local_report_path": str(report),
                    "fallback_trigger": {
                        "eligible": True,
                        "code": "EXPECTED_SECTION_ALL_CHANNEL_API_FAILED",
                        "reason": "r",
                        "passed_channels": [],
                        "failed_channels": ["Bus0"],
                    },
                }],
                rollback={"status": "PASS"},
                errors=[],
                warnings=[],
            )

        with (
            patch("machineb_transport.SshPowerShellTransport", return_value=Mock()),
            patch("run_machineB_full_validation.stage_remote_bundle", return_value={"status": "PASS"}),
            patch("run_machineB_full_validation.run_activated_validation", side_effect=fake_run),
        ):
            exit_code = main([
                "--project", "BOARD", "--repo-root", str(root), "--run-id", "run-1",
                "--execute", "--host", "192.0.2.10", "--user", "susiaa",
            ])

        self.assertEqual(exit_code, 1)
        plan_path = contract.outputs.run_root / "fallback-plan.json"
        plan = load_and_validate_fallback_plan(
            plan_path,
            expected_project="BOARD",
            expected_run_id="run-1",
            expected_full_ini=contract.inputs.full_ini,
            registry=load_candidate_registry(registry_src),
            applicable_sections={"SMBus"},
        )
        self.assertEqual(plan["sections"][0]["section"], "SMBus")
        self.assertIn("Fallback plan (SMBus): fallback-plan.json", contract.outputs.summary_text.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
