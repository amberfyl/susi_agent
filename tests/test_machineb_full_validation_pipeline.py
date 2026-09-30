import fnmatch
import json
import tempfile
import unittest
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace


class FakeCommandResult:
    def __init__(self, exit_code=0, stdout="", stderr=""):
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


class FakePipelineTransport:
    def __init__(self, *, reload_exit=0, healthy=True, original_exists=True):
        self.calls = []
        self.reload_exit = reload_exit
        self.healthy = healthy
        self.original_exists = original_exists
        self.remote_files = set()
        self.reports = {}
        self.runner_results = {}

    def remote_file_exists(self, path):
        self.calls.append(("exists", path))
        return path in self.remote_files

    def copy_remote_file(self, source, destination):
        self.calls.append(("copy", source, destination))
        if source not in self.remote_files:
            raise RuntimeError("missing source")
        self.remote_files.add(destination)

    def remove_remote_file_if_exists(self, path):
        self.calls.append(("remove", path))
        existed = path in self.remote_files
        self.remote_files.discard(path)
        return existed

    def close_process(self, name):
        self.calls.append(("close_process", name))
        return 1

    def invoke_batch(self, path):
        self.calls.append(("batch", path))
        return FakeCommandResult(self.reload_exit, "reload-output", "")

    def query_susi_device(self):
        self.calls.append(("query_device",))
        if self.healthy:
            return {"count": 1, "status": "OK", "problem": 0, "instance_id": "ROOT\\SYSTEM\\0001"}
        return {"count": 1, "status": "Error", "problem": 22, "instance_id": "ROOT\\SYSTEM\\0001"}

    def ensure_directories(self, paths):
        self.calls.append(("ensure_directories", tuple(paths)))

    def list_remote_files(self, directory, pattern):
        self.calls.append(("list", directory, pattern))
        return sorted(
            path
            for path in self.reports
            if path.startswith(directory)
            and fnmatch.fnmatch(PureWindowsPath(path).name, pattern)
        )

    def invoke_powershell_file(self, path, arguments, timeout_seconds=None):
        self.calls.append(("runner", path, dict(arguments), timeout_seconds))
        from run_machineB_full_validation import SECTION_REGISTRY

        runner_name = PureWindowsPath(path).name
        runner_map = {
            PureWindowsPath(entry.runner).name: (entry.section, entry.report_prefix)
            for entry in SECTION_REGISTRY
        }
        runner_map.update(
            {
                "fan.ps1": ("HWM.Fan", "hwm_fan"),
                "fancontrol.ps1": ("HWM.Fan.Control", "hwm_fan_control"),
                "gpio.ps1": ("GPIO", "gpio"),
            }
        )
        section, prefix = runner_map[runner_name]
        out_dir = arguments["OutDir"]
        report_path = out_dir + "\\" + prefix + "_20260930_120000.json"
        payload = self.runner_results.get(section)
        if payload is None:
            payload = {
                "category": section,
                "result": "PASS",
                "reason": "ok",
                "validation_layers": {
                    "L1_configuration": "PASS",
                    "L2_capability": "PASS",
                    "L3_api": "PASS",
                    "L4_readback": "PASS",
                    "L5_functional": "N_A",
                    "L6_recovery": "N_A",
                },
                "sw_verdict": "PASS_SW",
                "dqa_verdict": "N_A_DQA",
            }
        self.reports[report_path] = json.dumps(payload).encode("utf-8-sig")
        exit_code = 1 if str(payload.get("result", "")).startswith("FAIL") else 0
        return FakeCommandResult(exit_code, "runner-output", "")

    def download(self, remote_path, local_path):
        self.calls.append(("download", remote_path, str(local_path)))
        Path(local_path).parent.mkdir(parents=True, exist_ok=True)
        Path(local_path).write_bytes(self.reports[remote_path])


def make_contract(root):
    run_root = Path(root) / "run"
    return SimpleNamespace(
        schema_version="machineb.post_ini_contract.v1",
        project="BOARD",
        run_id="run-1",
        outputs=SimpleNamespace(
            run_root=run_root,
            reports_dir=run_root / "reports",
            manifest=run_root / "manifest.json",
            summary_json=run_root / "BOARD-machineB-summary.json",
            summary_text=run_root / "BOARD-machineB-summary.txt",
            reload_log=run_root / "reload.log",
        ),
        target=SimpleNamespace(
            runtime_ini=r"C:\Windows\SUSI\BOARD.ini",
            reload_bat=r"C:\Users\susiaa\Desktop\reload driver\reload_susi4_driver.bat",
            run_output=r"C:\Users\susiaa\Desktop\verify\BOARD\out\run-1",
        ),
    )


def section_plan(section, prefix, runner, config, *, dependencies=(), opt_ins=(), extra=()):
    return {
        "sequence": 1,
        "section": section,
        "runner_path": runner,
        "config_path": config,
        "report_prefix": prefix,
        "section_dependencies": list(dependencies),
        "available_opt_in_switches": list(opt_ins),
        "enabled_switches": [],
        "extra_path_arguments": list(extra),
    }


class SummaryTests(unittest.TestCase):
    def test_partial_channel_failure_is_explicit_in_text_summary(self):
        from run_machineB_full_validation import write_validation_summary

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            section_results = [{
                "section": "VGA.Backlight",
                "execution_status": "COMPLETED",
                "verdict": "FAIL",
                "result": "FAIL_API",
                "reason": "Partial channel failure: 1/2 passed; passed=[Backlight1]; failed=[Backlight2(0xFFFFFCFF)].",
                "channel_summary": {
                    "total": 2,
                    "passed": 1,
                    "failed": 1,
                    "passed_channels": ["Backlight1"],
                    "failed_channels": [{"channel": "Backlight2", "status": "0xFFFFFCFF"}],
                },
            }]

            summary = write_validation_summary(
                contract,
                runtime_ini={"status": "PASS"},
                reload_result={"status": "PASS"},
                section_results=section_results,
                rollback={"status": "PASS"},
                errors=[],
                warnings=[],
            )

            self.assertEqual(summary["status"], "SECTION_FAIL")
            text = contract.outputs.summary_text.read_text(encoding="utf-8")
            self.assertIn("PARTIAL_FAIL 1/2 passed", text)
            self.assertIn("passed=[Backlight1]", text)
            self.assertIn("failed=[Backlight2(0xFFFFFCFF)]", text)


class RuntimeActivationTests(unittest.TestCase):
    def test_deploys_full_ini_once_closes_demo_reloads_once_and_checks_health(self):
        from run_machineB_full_validation import activate_runtime_ini

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            transport = FakePipelineTransport()
            staged = r"C:\verify\BOARD\ini\BOARD-pre.ini"
            transport.remote_files.update({staged, contract.target.reload_bat})
            staging = {"artifacts": [{"kind": "full_ini", "remote_path": staged}]}

            result = activate_runtime_ini(contract, staging, transport)

            self.assertEqual(result["status"], "PASS")
            self.assertEqual([c[0] for c in transport.calls].count("copy"), 1)
            self.assertEqual([c[0] for c in transport.calls].count("batch"), 1)
            self.assertLess(
                [c[0] for c in transport.calls].index("close_process"),
                [c[0] for c in transport.calls].index("batch"),
            )
            self.assertTrue(result["readiness"]["ready"])

    def test_reload_failure_raises_and_cannot_be_treated_as_ready(self):
        from run_machineB_full_validation import RuntimePhaseError, activate_runtime_ini

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            transport = FakePipelineTransport(reload_exit=1)
            staged = r"C:\verify\BOARD\ini\BOARD-pre.ini"
            transport.remote_files.update({staged, contract.target.reload_bat})
            with self.assertRaises(RuntimePhaseError):
                activate_runtime_ini(
                    contract,
                    {"artifacts": [{"kind": "full_ini", "remote_path": staged}]},
                    transport,
                )


class RunnerAndReportTests(unittest.TestCase):
    def test_runs_registry_order_without_opt_in_switches_and_collects_bom_json(self):
        from run_machineB_full_validation import execute_validation_sections

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            transport = FakePipelineTransport()
            plans = [
                section_plan("HWM.Fan", "hwm_fan", r"C:\scripts\fan.ps1", r"C:\config\fan.json", opt_ins=("EnableStimulus",)),
                section_plan(
                    "HWM.Fan.Control",
                    "hwm_fan_control",
                    r"C:\scripts\fancontrol.ps1",
                    r"C:\config\fancontrol.json",
                    dependencies=("HWM.Fan",),
                    opt_ins=("AllowControl",),
                    extra=("FanConfigPath", "FanIniPath"),
                ),
                section_plan("GPIO", "gpio", r"C:\scripts\gpio.ps1", r"C:\config\gpio.json", opt_ins=("EnableFunctionalTest",)),
            ]

            results = execute_validation_sections(contract, {"sections": plans}, transport)

            self.assertEqual([r["section"] for r in results], ["HWM.Fan", "HWM.Fan.Control", "GPIO"])
            invocations = [c for c in transport.calls if c[0] == "runner"]
            self.assertEqual(len(invocations), 3)
            for invocation in invocations:
                args = invocation[2]
                self.assertNotIn("EnableStimulus", args)
                self.assertNotIn("AllowControl", args)
                self.assertNotIn("EnableFunctionalTest", args)
                self.assertEqual(args["IniPath"], contract.target.runtime_ini)
            fan_control_args = invocations[1][2]
            self.assertEqual(fan_control_args["FanConfigPath"], r"C:\config\fan.json")
            self.assertEqual(fan_control_args["FanIniPath"], contract.target.runtime_ini)
            self.assertTrue(all(Path(r["local_report_path"]).is_file() for r in results))

    def test_fan_sw_failure_blocks_control_but_independent_section_continues(self):
        from run_machineB_full_validation import execute_validation_sections

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            transport = FakePipelineTransport()
            transport.runner_results["HWM.Fan"] = {
                "category": "HWM.Fan",
                "result": "FAIL_API",
                "reason": "tach failed",
                "validation_layers": {"L1_configuration": "PASS", "L2_capability": "PASS", "L3_api": "FAIL", "L4_readback": "FAIL", "L5_functional": "N_A", "L6_recovery": "N_A"},
                "sw_verdict": "FAIL_SW",
                "dqa_verdict": "N_A_DQA",
            }
            plans = [
                section_plan("HWM.Fan", "hwm_fan", "fan.ps1", "fan.json"),
                section_plan("HWM.Fan.Control", "hwm_fan_control", "fancontrol.ps1", "fancontrol.json", dependencies=("HWM.Fan",), extra=("FanConfigPath", "FanIniPath")),
                section_plan("GPIO", "gpio", "gpio.ps1", "gpio.json"),
            ]

            results = execute_validation_sections(contract, {"sections": plans}, transport)

            self.assertEqual(results[1]["execution_status"], "BLOCKED_DEPENDENCY")
            self.assertEqual(results[2]["execution_status"], "COMPLETED")
            invoked_runners = [PureWindowsPath(c[1]).name for c in transport.calls if c[0] == "runner"]
            self.assertEqual(invoked_runners, ["fan.ps1", "gpio.ps1"])


    def test_all_14_registry_entries_execute_in_fixed_order_with_safe_defaults(self):
        from run_machineB_full_validation import SECTION_REGISTRY, execute_validation_sections

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            transport = FakePipelineTransport()
            plans = []
            for sequence, entry in enumerate(SECTION_REGISTRY, start=1):
                plans.append(
                    section_plan(
                        entry.section,
                        entry.report_prefix,
                        entry.runner,
                        entry.config_template.format(model="BOARD"),
                        dependencies=entry.section_dependencies,
                        opt_ins=entry.opt_in_switches,
                        extra=entry.extra_path_arguments,
                    ) | {"sequence": sequence}
                )

            results = execute_validation_sections(contract, {"sections": plans}, transport)

            self.assertEqual(len(results), 14)
            self.assertEqual([item["section"] for item in results], [entry.section for entry in SECTION_REGISTRY])
            self.assertTrue(all(item["execution_status"] == "COMPLETED" for item in results))
            invocations = [call for call in transport.calls if call[0] == "runner"]
            self.assertEqual(len(invocations), 14)
            dangerous = {
                "AllowControl",
                "EnableFixtureTest",
                "EnableFunctionalTest",
                "EnableSetConfigTest",
                "EnableStimulus",
                "EnableWriteTest",
            }
            self.assertTrue(all(dangerous.isdisjoint(call[2]) for call in invocations))

    def test_reload_failure_blocks_all_runners_but_still_rolls_back(self):
        from run_machineB_full_validation import run_activated_validation

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            transport = FakePipelineTransport(reload_exit=1)
            staged = r"C:\\verify\\BOARD\\ini\\BOARD-pre.ini"
            backup = r"C:\\verify\\BOARD\\out\\run-1\\backup\\BOARD.ini.before-run"
            transport.remote_files.update({staged, backup, contract.target.reload_bat, contract.target.runtime_ini})
            staging = {
                "artifacts": [{"kind": "full_ini", "remote_path": staged}],
                "backup": {"original_exists": True, "backup_ini": backup},
            }

            summary = run_activated_validation(
                contract,
                {"sections": [section_plan("GPIO", "gpio", "gpio.ps1", "gpio.json")]},
                staging,
                transport,
            )

            self.assertEqual(summary["status"], "ORCHESTRATOR_ERROR")
            self.assertFalse(any(call[0] == "runner" for call in transport.calls))
            self.assertEqual(summary["rollback"]["action"], "RESTORED_BACKUP")
            self.assertEqual([call[0] for call in transport.calls].count("batch"), 2)


class RollbackAndSummaryTests(unittest.TestCase):
    def test_rollback_restores_original_and_performs_separate_recovery_reload(self):
        from run_machineB_full_validation import rollback_runtime_ini

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            transport = FakePipelineTransport()
            backup = r"C:\verify\out\backup\BOARD.ini.before-run"
            transport.remote_files.update({backup, contract.target.reload_bat, contract.target.runtime_ini})
            result = rollback_runtime_ini(
                contract,
                {"backup": {"original_exists": True, "backup_ini": backup}},
                transport,
            )
            self.assertEqual(result["status"], "PASS")
            self.assertIn(("copy", backup, contract.target.runtime_ini), transport.calls)
            self.assertEqual([c[0] for c in transport.calls].count("batch"), 1)

    def test_summary_preserves_sw_and_dqa_and_writes_json_and_text(self):
        from run_machineB_full_validation import write_validation_summary

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            section_results = [{
                "section": "SMBus",
                "execution_status": "COMPLETED",
                "verdict": "CONDITIONAL",
                "sw_verdict": "PASS_SW",
                "dqa_verdict": "PENDING_DQA",
                "runner_exit_code": 0,
                "reason": "fixture not enabled",
            }]
            summary = write_validation_summary(
                contract,
                runtime_ini={"status": "PASS"},
                reload_result={"status": "PASS"},
                section_results=section_results,
                rollback={"status": "PASS"},
                errors=[],
                warnings=[],
            )
            self.assertEqual(summary["sections"][0]["sw_verdict"], "PASS_SW")
            self.assertEqual(summary["sections"][0]["dqa_verdict"], "PENDING_DQA")
            self.assertTrue(contract.outputs.summary_json.is_file())
            self.assertTrue(contract.outputs.summary_text.is_file())


if __name__ == "__main__":
    unittest.main()
