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


def section_plan(section, prefix, runner, config, *, dependencies=(), opt_ins=(), enabled=(), extra=()):
    return {
        "sequence": 1,
        "section": section,
        "runner_path": runner,
        "config_path": config,
        "report_prefix": prefix,
        "section_dependencies": list(dependencies),
        "available_opt_in_switches": list(opt_ins),
        "enabled_switches": list(enabled),
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
            self.assertIn("Status: [SECTION_FAIL]  (exit code 1)", text)
            self.assertIn("  [FAIL]  VGA.Backlight  PARTIAL_FAIL 1/2 passed", text)
            self.assertIn("passed=[Backlight1]", text)
            self.assertIn("failed=[Backlight2(0xFFFFFCFF)]", text)

    def test_text_summary_lists_section_report_paths(self):
        from run_machineB_full_validation import write_validation_summary

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            report = contract.outputs.reports_dir / "wdt_1.json"
            write_validation_summary(
                contract,
                runtime_ini={"status": "PASS"},
                reload_result={"status": "PASS"},
                section_results=[{
                    "section": "WDT",
                    "execution_status": "COMPLETED",
                    "verdict": "PASS",
                    "reason": "ok",
                    "local_report_path": str(report),
                }],
                rollback={"status": "PASS"},
                errors=[],
                warnings=[],
            )
            text = contract.outputs.summary_text.read_text(encoding="utf-8")
            self.assertIn("Details:", text)
            self.assertIn("- WDT: reports/wdt_1.json", text)


def layers(l1="PASS", l2="PASS", l3="PASS", l4="PASS", l5="PASS", l6="N_A"):
    return {
        "L1_configuration": l1,
        "L2_capability": l2,
        "L3_api": l3,
        "L4_readback": l4,
        "L5_functional": l5,
        "L6_recovery": l6,
    }


class Phase1VerdictTests(unittest.TestCase):
    def verdict(self, result, validation_layers):
        from run_machineB_full_validation import _normalize_report_verdict

        report = {"result": result, "validation_layers": validation_layers}
        return _normalize_report_verdict(report)[0]

    def test_sw_layers_pass_is_pass_even_without_fixture(self):
        self.assertEqual(self.verdict("CONDITIONAL", layers(l5="CONDITIONAL")), "PASS")
        self.assertEqual(self.verdict("CONDITIONAL", layers(l5="PENDING_FIXTURE")), "PASS")

    def test_phase2_only_failure_does_not_fail_phase1(self):
        self.assertEqual(self.verdict("FAIL_FUNCTIONAL", layers(l5="FAIL")), "PASS")
        self.assertEqual(self.verdict("FAIL_FIXTURE", layers(l5="FAIL_FIXTURE")), "PASS")

    def test_unexercised_api_path_is_conditional(self):
        # WDT: readback not exercised; Fan.Control without -AllowControl.
        self.assertEqual(self.verdict("CONDITIONAL", layers(l4="CONDITIONAL")), "CONDITIONAL")
        self.assertEqual(
            self.verdict("BLOCKED_SAFETY", layers(l2="NOT_REQUIRED", l3="PENDING", l4="PENDING")),
            "CONDITIONAL",
        )

    def test_write_path_sections_count_functional_write_as_phase1(self):
        from run_machineB_full_validation import _normalize_report_verdict

        def verdict(section, result, validation_layers):
            report = {"category": section, "result": result, "validation_layers": validation_layers}
            return _normalize_report_verdict(report)[0]

        for section in ("VGA.Backlight", "VGA.Brightness", "GPIO", "StorageArea"):
            with self.subTest(section=section):
                self.assertEqual(verdict(section, "PASS", layers(l5="PASS", l6="PASS")), "PASS")
                self.assertEqual(verdict(section, "FAIL_FUNCTIONAL", layers(l5="FAIL", l6="PASS")), "FAIL")
                self.assertEqual(verdict(section, "PASS", layers(l5="CONDITIONAL")), "CONDITIONAL")
        # Fan.Control: RPM response needs a fan, so L5 stays phase 2.
        self.assertEqual(
            verdict("HWM.Fan.Control", "FAIL_FUNCTIONAL", layers(l5="FAIL", l6="PASS")), "PASS"
        )

    def test_sw_failure_or_failed_restore_is_fail(self):
        self.assertEqual(self.verdict("FAIL_API", layers(l3="FAIL")), "FAIL")
        self.assertEqual(self.verdict("FAIL_READBACK", layers(l6="FAIL")), "FAIL")
        self.assertEqual(self.verdict("FAIL_API", layers()), "FAIL")

    def test_summary_lists_phase2_recommendations(self):
        from run_machineB_full_validation import write_validation_summary

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            summary = write_validation_summary(
                contract,
                runtime_ini={"status": "PASS"},
                reload_result={"status": "PASS"},
                section_results=[
                    {"section": "HWM.Voltage", "execution_status": "COMPLETED", "verdict": "PASS",
                     "reason": "read ok", "validation_layers": layers(l5="CONDITIONAL")},
                    {"section": "WDT", "execution_status": "COMPLETED", "verdict": "CONDITIONAL",
                     "reason": "start not run", "validation_layers": layers(l4="CONDITIONAL", l5="CONDITIONAL")},
                    {"section": "VGA.Backlight", "execution_status": "COMPLETED", "verdict": "PASS",
                     "reason": "toggle ok", "validation_layers": layers(l6="PASS")},
                    {"section": "GPIO", "execution_status": "COMPLETED", "verdict": "FAIL",
                     "reason": "read failed", "validation_layers": layers(l3="FAIL", l5="PENDING")},
                ],
                rollback={"status": "PASS"},
                errors=[],
                warnings=[],
            )

            sections = [entry["section"] for entry in summary["phase2_recommendations"]]
            self.assertEqual(sections, ["HWM.Voltage", "WDT"])
            text = contract.outputs.summary_text.read_text(encoding="utf-8")
            self.assertIn("Scope: Phase 1", text)
            self.assertIn("Phase 2 recommendations (hardware / fixture / DQA):", text)
            self.assertIn("- HWM.Voltage: Apply load stimulus", text)
            self.assertIn("- WDT: Let the watchdog expire", text)


class SectionReasonTests(unittest.TestCase):
    def test_phase1_headline_and_phase2_observation_are_separated(self):
        from run_machineB_full_validation import _section_reason, write_validation_summary

        report = {
            "reason": "RPM response too small on FCPU (delta=0, min=200).",
            "sw_reason": "PWM set/readback/restore passed on FCPU (30/50/70%)",
        }
        headline, observation = _section_reason(report, "PASS")
        self.assertEqual(headline, "PWM set/readback/restore passed on FCPU (30/50/70%)")
        self.assertEqual(observation, "RPM response too small on FCPU (delta=0, min=200).")
        # A failing section keeps the runner's own reason.
        self.assertEqual(_section_reason(report, "FAIL"), (report["reason"], None))

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            write_validation_summary(
                contract,
                runtime_ini={"status": "PASS"},
                reload_result={"status": "PASS"},
                section_results=[{
                    "section": "HWM.Fan.Control",
                    "execution_status": "COMPLETED",
                    "verdict": "PASS",
                    "reason": headline,
                    "phase2_observation": observation,
                    "validation_layers": layers(l5="FAIL", l6="PASS"),
                }],
                rollback={"status": "PASS"},
                errors=[],
                warnings=[],
            )
            text = contract.outputs.summary_text.read_text(encoding="utf-8")
            self.assertIn("[PASS]  HWM.Fan.Control  PWM set/readback/restore passed", text)
            self.assertIn(
                "- HWM.Fan.Control: Attach fans and confirm RPM rises with PWM "
                "(expected delta >= 200 RPM). Observed: RPM response too small",
                text,
            )


class ReadOnlySummaryTests(unittest.TestCase):
    def test_summary_scope_marks_read_only_runs(self):
        from run_machineB_full_validation import write_validation_summary

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            summary = write_validation_summary(
                contract,
                runtime_ini={"status": "PASS"},
                reload_result={"status": "PASS"},
                section_results=[],
                rollback={"status": "PASS"},
                errors=[],
                warnings=[],
                write_tests_enabled=False,
            )
            self.assertFalse(summary["write_tests_enabled"])
            text = contract.outputs.summary_text.read_text(encoding="utf-8")
            self.assertIn("Scope: Phase 1 - SW API READ-ONLY (write tests disabled by --no-write-tests)", text)


def gpio_report(support, *, caps_status="0x00000000"):
    return {
        "category": "GPIO",
        "result": "FAIL_API" if caps_status != "0x00000000" else "PASS",
        "api_calls": [
            {"name": "GPIO GetCaps input:Bank0", "status_code": caps_status},
            {"name": "GPIO GetCaps output:Bank0", "status_code": caps_status},
            {"name": "GPIO GetDirection:Bank0", "status_code": "0x00000000"},
            {"name": "GPIO GetLevel:Bank0", "status_code": "0x00000000"},
        ],
        "metrics": {"banks": {"Bank0": {
            "expected_mask": "0x0000FFFF", "input_support": support, "output_support": support,
            "caps_ok": support == "0x0000FFFF", "reads_ok": True,
        }}},
    }


class GpioOutcomeTests(unittest.TestCase):
    def _contract(self, root):
        config_dir = root / "config"
        config_dir.mkdir()
        channels = {
            f"GPIO{i:02d}": {"bank": 0, "bank_bitmask": f"0x{1 << i:08X}", "tuple_group": 0, "tuple_pin": i}
            for i in range(16)
        }
        (config_dir / "BOARD_gpio.json").write_text(json.dumps({
            "channels": channels,
            "banks": {"Bank0": {"bank_number": 0, "expected_mask": "0x0000FFFF"}},
        }), encoding="utf-8")
        (root / "BOARD-gpio-trace.json").write_text(json.dumps({"items": [
            {"report_name": "GPIO12", "signal": "EC_P2_GPIO4", "function_label": "GPIO57/KBRST#", "group": 5, "bit": 7},
            {"report_name": "GPIO14", "signal": "EC_P2_GPIO6", "function_label": "GPIO91", "group": 9, "bit": 1},
        ]}), encoding="utf-8")
        return SimpleNamespace(project="BOARD", inputs=SimpleNamespace(config_dir=config_dir, case_dir=root))

    def test_partial_mask_is_fail_partial_and_names_missing_pins(self):
        from run_machineB_full_validation import _gpio_outcome

        with tempfile.TemporaryDirectory() as td:
            contract = self._contract(Path(td))
            verdict, reason, pins = _gpio_outcome(
                contract, gpio_report("0x0000AFFF"), "PASS", "GPIO read validation passed"
            )

            self.assertEqual(verdict, "FAIL")
            self.assertEqual([p["key"] for p in pins], ["GPIO12", "GPIO14"])
            self.assertEqual((pins[0]["signal"], pins[0]["group"], pins[0]["pin"]), ("EC_P2_GPIO4", 5, 7))
            self.assertTrue(reason.startswith("PARTIAL: 14 of 16 GPIO supported"))
            self.assertIn("GPIO12 = EC_P2_GPIO4 -> GPIO57/KBRST# (group 5, bit 7)", reason)
            self.assertNotIn("Route OK", reason)

    def test_failed_getcaps_is_not_a_partial_mask_even_with_mask_bits(self):
        # MIO-5854 run MIO-5854-20261006T064917Z: GetCaps returned 0xFFFFFCFF
        # while the report still carried mask 0x000001E8.
        from run_machineB_full_validation import _gpio_outcome

        with tempfile.TemporaryDirectory() as td:
            contract = self._contract(Path(td))
            verdict, reason, pins = _gpio_outcome(
                contract, gpio_report("0x000001E8", caps_status="0xFFFFFCFF"), "FAIL", "x"
            )

            self.assertEqual(verdict, "FAIL")
            self.assertEqual(pins, [])
            self.assertIn("GetCaps failed on every bank", reason)
            self.assertIn("GetCaps input:Bank0=0xFFFFFCFF", reason)
            self.assertNotIn("Route OK", reason)
            self.assertNotIn("PARTIAL", reason)

    def test_full_mask_keeps_runner_verdict(self):
        from run_machineB_full_validation import _gpio_outcome

        verdict, reason, pins = _gpio_outcome(
            SimpleNamespace(project="X", inputs=None), gpio_report("0x0000FFFF"), "PASS", "ok"
        )
        self.assertEqual((verdict, reason, pins), ("PASS", "ok", []))


REGISTRY_PATH = (
    Path(__file__).resolve().parents[1]
    / "targetB_task" / "machineB_validation" / "fallback_candidate_registry.json"
)


class FallbackPlanWriterTests(unittest.TestCase):
    def _setup(self, td, *, eligible):
        from run_machineB_full_validation import write_validation_summary

        root = Path(td)
        contract = make_contract(td)
        full_ini = root / "BOARD-pre.ini"
        full_ini.write_text(
            "[GPIO]\nGPIO00=0x0000FFFD,0,0,0xA0000003,3,4,\nGPIO01=0x0000FFFD,0,0,0xA0000003,3,5,\n",
            encoding="utf-8",
        )
        contract.inputs = SimpleNamespace(full_ini=full_ini, generated_sections=["GPIO"])
        report = contract.outputs.reports_dir / "gpio_1.json"
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps(gpio_report("0x000001E8", caps_status="0xFFFFFCFF")), encoding="utf-8")
        write_validation_summary(
            contract,
            runtime_ini={"status": "PASS"},
            reload_result={"status": "PASS"},
            section_results=[{
                "section": "GPIO",
                "execution_status": "COMPLETED",
                "verdict": "FAIL",
                "reason": "GetCaps failed on every bank",
                "local_report_path": str(report),
                "fallback_trigger": {
                    "eligible": eligible,
                    "code": "EXPECTED_GPIO_ROUTE_PROBES_ALL_FAILED" if eligible else "GPIO_ROUTE_PROBE_PARTIAL_SUCCESS_NO_FALLBACK",
                    "reason": "r",
                    "passed_channels": [] if eligible else ["Bank0"],
                    "failed_channels": ["Bank0"] if eligible else [],
                },
            }],
            rollback={"status": "PASS"},
            errors=[],
            warnings=[],
        )
        return contract, REGISTRY_PATH

    def test_eligible_trigger_writes_a_plan_that_validates(self):
        from machineb_fallback import load_and_validate_fallback_plan, load_candidate_registry
        from run_machineB_full_validation import write_fallback_plan

        with tempfile.TemporaryDirectory() as td:
            contract, registry = self._setup(td, eligible=True)
            text = contract.outputs.summary_text.read_text(encoding="utf-8")
            self.assertIn("fallback trigger: ELIGIBLE EXPECTED_GPIO_ROUTE_PROBES_ALL_FAILED", text)

            path = write_fallback_plan(contract, registry)

            plan = load_and_validate_fallback_plan(
                path,
                expected_project="BOARD",
                expected_run_id="run-1",
                expected_full_ini=contract.inputs.full_ini,
                registry=load_candidate_registry(registry),
                applicable_sections={"GPIO"},
            )
            section = plan["sections"][0]
            self.assertEqual(section["baseline_route"], "0")
            self.assertNotIn("0", section["route_candidates"])
            self.assertIn("0x2E", section["route_candidates"])
            text = contract.outputs.summary_text.read_text(encoding="utf-8")
            self.assertIn("Fallback plan (GPIO): fallback-plan.json", text)

    def test_ineligible_trigger_writes_no_plan(self):
        from run_machineB_full_validation import write_fallback_plan

        with tempfile.TemporaryDirectory() as td:
            contract, registry = self._setup(td, eligible=False)
            self.assertIsNone(write_fallback_plan(contract, registry))
            self.assertFalse((contract.outputs.run_root / "fallback-plan.json").exists())


class FallbackFinalSummaryTests(unittest.TestCase):
    def _baseline(self, contract):
        from run_machineB_full_validation import write_validation_summary

        reports = contract.outputs.reports_dir
        return write_validation_summary(
            contract,
            runtime_ini={"status": "PASS"},
            reload_result={"status": "PASS"},
            section_results=[
                {
                    "section": "SMBus",
                    "execution_status": "COMPLETED",
                    "verdict": "CONDITIONAL",
                    "result": "CONDITIONAL_FIXTURE",
                    "reason": "fixture not enabled",
                    "local_report_path": str(reports / "smbus_1.json"),
                },
                {
                    "section": "GPIO",
                    "execution_status": "COMPLETED",
                    "verdict": "FAIL",
                    "result": "FAIL_API",
                    "reason": "GPIO capability or initial read failed.",
                    "local_report_path": str(reports / "gpio_1.json"),
                },
            ],
            rollback={"status": "PASS"},
            errors=[],
            warnings=[],
        )

    def _convergence(self, contract, *, status, attempts):
        run_root = contract.outputs.run_root
        return {
            "schema_version": "machineb.fallback_convergence.v1",
            "status": status,
            "sections": [{
                "section": "GPIO",
                "status": status,
                "selected_route": "0x2E" if status == "CONVERGED" else None,
                "attempts": attempts,
            }],
            "formal_convergence": {
                "status": "APPLIED" if status == "CONVERGED" else "NO_CHANGE",
                "override_path": str(Path(contract.inputs.full_ini).parent / "BOARD-config-overrides.json"),
                "override": {"sections": {"GPIO": {"route_field": "IOPort/Address"}}},
            },
            "final_runtime_recovery": {"status": "PASS"},
            "output_path": str(run_root / "fallback-convergence.json"),
        }

    def _attempt_report(self, contract, index, result, reason):
        path = (
            contract.outputs.run_root / "fallback" / "gpio"
            / f"attempt-{index:03d}" / "reports" / f"gpio_{index}.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"category": "GPIO", "result": result, "reason": reason}
        path.write_text(json.dumps(payload), encoding="utf-8-sig")
        return str(path)

    def test_converged_section_replaces_baseline_result_in_final_summary(self):
        from run_machineB_full_validation import finalize_summary_after_fallback

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            contract.inputs = SimpleNamespace(full_ini=Path(td) / "BOARD-pre.ini")
            self._baseline(contract)
            attempts = [
                {"index": 1, "success": False, "route_value": "0",
                 "report_path": self._attempt_report(contract, 1, "FAIL_API", "read failed")},
                {"index": 2, "success": True, "route_value": "0x2E",
                 "changed_keys": ["GPIO00", "GPIO01"],
                 "report_path": self._attempt_report(contract, 2, "PASS", "GPIO read validation passed")},
            ]
            convergence = self._convergence(contract, status="CONVERGED", attempts=attempts)

            summary = finalize_summary_after_fallback(contract, convergence)

            self.assertEqual(summary["status"], "PASS")
            self.assertEqual(summary["exit_code"], 0)
            gpio = next(item for item in summary["sections"] if item["section"] == "GPIO")
            self.assertEqual(gpio["verdict"], "PASS")
            self.assertEqual(gpio["reason"], "GPIO read validation passed")
            self.assertEqual(gpio["local_report_path"], attempts[1]["report_path"])
            self.assertEqual(gpio["fallback"]["status"], "CONVERGED")
            self.assertEqual(gpio["fallback"]["baseline_verdict"], "FAIL")
            self.assertEqual(gpio["fallback"]["winning_attempt"], 2)
            self.assertEqual(summary["fallback"]["status"], "CONVERGED")

            baseline_json = contract.outputs.run_root / "BOARD-machineB-summary.baseline.json"
            baseline = json.loads(baseline_json.read_text(encoding="utf-8"))
            self.assertEqual(baseline["status"], "SECTION_FAIL")

            text = contract.outputs.summary_text.read_text(encoding="utf-8")
            self.assertIn("Status: [PASS]", text)
            self.assertIn("Result: 1 CONDITIONAL / 1 PASS", text)
            self.assertRegex(text, r"\n  \[PASS\] +GPIO +GPIO read validation passed")
            self.assertIn("fallback CONVERGED", text)
            self.assertIn("(baseline was FAIL)", text)
            self.assertIn("IOPort/Address=0x2E", text)
            self.assertIn("- GPIO: fallback/gpio/attempt-002/reports/gpio_2.json", text)
            self.assertIn("fallback-convergence.json", text)
            self.assertIn("BOARD-machineB-summary.baseline.txt", text)

            # Re-finalizing must merge onto the preserved baseline, not the merged summary.
            again = finalize_summary_after_fallback(contract, convergence)
            self.assertEqual(again["status"], "PASS")
            baseline = json.loads(baseline_json.read_text(encoding="utf-8"))
            self.assertEqual(baseline["status"], "SECTION_FAIL")

    def test_partial_winner_converges_but_stays_fail_and_lists_attempts(self):
        from run_machineB_full_validation import finalize_summary_after_fallback

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            contract.inputs = SimpleNamespace(
                full_ini=Path(td) / "BOARD-pre.ini", config_dir=Path(td), case_dir=Path(td)
            )
            self._baseline(contract)
            winner = contract.outputs.run_root / "fallback" / "gpio" / "attempt-002" / "reports" / "gpio_2.json"
            winner.parent.mkdir(parents=True, exist_ok=True)
            winner.write_text(json.dumps(gpio_report("0x00001FFF")), encoding="utf-8")
            attempts = [
                {"index": 1, "success": False, "route_value": "1", "gpio_caps_state": "ALL_FAILED",
                 "gpio_supported_pins": 0, "gpio_expected_pins": 0,
                 "report_path": self._attempt_report(contract, 1, "FAIL_API", "caps failed")},
                {"index": 2, "success": True, "route_value": "0x2E", "gpio_caps_state": "PARTIAL_SUCCESS",
                 "gpio_supported_pins": 13, "gpio_expected_pins": 16,
                 "changed_keys": ["GPIO00"], "report_path": str(winner)},
            ]

            summary = finalize_summary_after_fallback(
                contract, self._convergence(contract, status="CONVERGED", attempts=attempts)
            )

            gpio = next(item for item in summary["sections"] if item["section"] == "GPIO")
            self.assertEqual(gpio["verdict"], "FAIL")
            self.assertTrue(gpio["reason"].startswith("PARTIAL: 13 of 16 GPIO supported"))
            text = contract.outputs.summary_text.read_text(encoding="utf-8")
            self.assertIn("IOPort/Address=0x2E written to the INI", text)
            self.assertIn("tried: 1 GetCaps failed, 0x2E 13/16 pins", text)

    def test_exhausted_fallback_keeps_baseline_failure_and_records_attempts(self):
        from run_machineB_full_validation import finalize_summary_after_fallback

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            contract.inputs = SimpleNamespace(full_ini=Path(td) / "BOARD-pre.ini")
            self._baseline(contract)
            attempts = [
                {"index": 1, "success": False, "route_value": "0",
                 "report_path": self._attempt_report(contract, 1, "FAIL_API", "read failed")},
            ]
            convergence = self._convergence(
                contract, status="ALL_CANDIDATES_FAILED", attempts=attempts
            )

            summary = finalize_summary_after_fallback(contract, convergence)

            self.assertEqual(summary["status"], "SECTION_FAIL")
            gpio = next(item for item in summary["sections"] if item["section"] == "GPIO")
            self.assertEqual(gpio["verdict"], "FAIL")
            self.assertEqual(gpio["fallback"]["attempt_count"], 1)
            text = contract.outputs.summary_text.read_text(encoding="utf-8")
            self.assertIn("fallback ALL_CANDIDATES_FAILED after 1 attempt(s)", text)

    def test_missing_baseline_summary_returns_none(self):
        from run_machineB_full_validation import finalize_summary_after_fallback

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            contract.inputs = SimpleNamespace(full_ini=Path(td) / "BOARD-pre.ini")
            convergence = self._convergence(contract, status="CONVERGED", attempts=[])
            self.assertIsNone(finalize_summary_after_fallback(contract, convergence))


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

    def test_passes_explicitly_enabled_allow_control_to_fan_control_runner(self):
        from run_machineB_full_validation import execute_validation_sections

        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            transport = FakePipelineTransport()
            plans = [
                section_plan("HWM.Fan", "hwm_fan", r"C:\\scripts\\fan.ps1", r"C:\\config\\fan.json"),
                section_plan(
                    "HWM.Fan.Control",
                    "hwm_fan_control",
                    r"C:\\scripts\\fancontrol.ps1",
                    r"C:\\config\\fancontrol.json",
                    dependencies=("HWM.Fan",),
                    opt_ins=("AllowControl",),
                    enabled=("AllowControl",),
                    extra=("FanConfigPath", "FanIniPath"),
                ),
            ]

            execute_validation_sections(contract, {"sections": plans}, transport)

            invocations = [call for call in transport.calls if call[0] == "runner"]
            self.assertIn("AllowControl", invocations[1][2])
            self.assertTrue(invocations[1][2]["AllowControl"])

    def test_all_enabled_switches_propagate_as_boolean_runner_arguments(self):
        from run_machineB_full_validation import SECTION_REGISTRY, execute_validation_sections
        with tempfile.TemporaryDirectory() as td:
            contract = make_contract(td)
            transport = FakePipelineTransport()
            plans = [section_plan(
                entry.section, entry.report_prefix, entry.runner,
                entry.config_template.format(model="BOARD"),
                dependencies=entry.section_dependencies,
                opt_ins=entry.opt_in_switches,
                enabled=entry.default_switches,
                extra=entry.extra_path_arguments,
            ) for entry in SECTION_REGISTRY]
            execute_validation_sections(contract, {"sections": plans}, transport)
            calls = [call for call in transport.calls if call[0] == "runner"]
            self.assertEqual(len(calls), len(plans))
            for plan, call in zip(plans, calls):
                for switch in plan["enabled_switches"]:
                    self.assertIs(call[2][switch], True)
                self.assertNotIn("EnableFixtureTest", call[2])
                self.assertNotIn("EnableStimulus", call[2])

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
