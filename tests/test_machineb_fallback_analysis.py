import unittest


class FallbackTriggerAnalysisTests(unittest.TestCase):
    def _summary(self, **overrides):
        payload = {
            "section": "HWM.Temperature",
            "execution_status": "COMPLETED",
            "verdict": "FAIL",
            "result": "FAIL_API",
            "runner_exit_code": 1,
            "reason": "All configured temperature channels failed API reads.",
        }
        payload.update(overrides)
        return payload

    def _report(self, **overrides):
        payload = {
            "category": "HWM.Temperature",
            "result": "FAIL_API",
            "reason": "All configured temperature channels failed API reads.",
            "validation_layers": {
                "L1_configuration": "PASS",
                "L2_capability": "FAIL",
                "L3_api": "FAIL",
                "L4_readback": "FAIL",
            },
            "result_breakdown": {
                "pass": ["layer:L1_configuration"],
                "pending": [],
                "fail": [
                    "layer:L2_capability",
                    "layer:L3_api",
                    "channel:TCPU",
                    "channel:TSYS",
                ],
                "na": [],
            },
        }
        payload.update(overrides)
        return payload

    def test_all_channels_api_failed_is_eligible(self):
        from machineb_fallback import analyze_section_trigger

        decision = analyze_section_trigger(self._summary(), self._report())

        self.assertTrue(decision.eligible)
        self.assertEqual(decision.code, "EXPECTED_SECTION_ALL_CHANNEL_API_FAILED")
        self.assertEqual(decision.passed_channels, ())
        self.assertEqual(decision.failed_channels, ("TCPU", "TSYS"))

    def test_any_successful_channel_is_not_eligible(self):
        from machineb_fallback import analyze_section_trigger

        report = self._report()
        report["result_breakdown"]["pass"].append("channel:TCPU")
        report["result_breakdown"]["fail"] = [
            item for item in report["result_breakdown"]["fail"] if item != "channel:TCPU"
        ]
        decision = analyze_section_trigger(self._summary(), report)

        self.assertFalse(decision.eligible)
        self.assertEqual(decision.code, "PARTIAL_FAIL_NO_FALLBACK")
        self.assertEqual(decision.passed_channels, ("TCPU",))

    def test_fixture_block_is_not_eligible(self):
        from machineb_fallback import analyze_section_trigger

        summary = self._summary(result="CONDITIONAL", reason="BLOCKED_FIXTURE: fixture missing")
        report = self._report(reason="BLOCKED_FIXTURE: fixture missing")
        decision = analyze_section_trigger(summary, report)

        self.assertFalse(decision.eligible)
        self.assertEqual(decision.code, "FIXTURE_OR_SAFETY_BLOCK_NO_FALLBACK")

    def test_infrastructure_error_is_not_eligible(self):
        from machineb_fallback import analyze_section_trigger

        decision = analyze_section_trigger(
            self._summary(execution_status="RUNNER_ERROR", result="ERROR", runner_exit_code=2),
            self._report(),
        )

        self.assertFalse(decision.eligible)
        self.assertEqual(decision.code, "INFRASTRUCTURE_ERROR_NO_FALLBACK")

    def test_capability_failure_with_api_success_is_not_eligible(self):
        from machineb_fallback import analyze_section_trigger

        report = self._report(
            result="FAIL_CAPABILITY",
            validation_layers={
                "L1_configuration": "PASS",
                "L2_capability": "FAIL",
                "L3_api": "PASS",
                "L4_readback": "FAIL",
            },
        )
        decision = analyze_section_trigger(
            self._summary(section="I2C", result="FAIL_CAPABILITY"), report
        )

        self.assertFalse(decision.eligible)
        self.assertEqual(decision.code, "API_NOT_ALL_FAILED_NO_FALLBACK")

    def test_missing_channel_evidence_fails_closed(self):
        from machineb_fallback import analyze_section_trigger

        report = self._report(result_breakdown={"pass": [], "pending": [], "fail": ["layer:L3_api"], "na": []})
        decision = analyze_section_trigger(self._summary(), report)

        self.assertFalse(decision.eligible)
        self.assertEqual(decision.code, "CHANNEL_EVIDENCE_MISSING")

    def _gpio_report(self, *, input_status="0xFFFFFCFF", output_status="0xFFFFFCFF"):
        return {
            "category": "GPIO",
            "result": "FAIL_API",
            "reason": "GPIO capability or initial read failed.",
            "validation_layers": {"L3_api": "FAIL"},
            "api_calls": [
                {"name": "GPIO GetCaps input:Bank0", "status_code": input_status},
                {"name": "GPIO GetCaps output:Bank0", "status_code": output_status},
                {"name": "GPIO GetDirection:Bank0", "status_code": "0x00000000"},
                {"name": "GPIO GetLevel:Bank0", "status_code": "0x00000000"},
            ],
            "metrics": {
                "banks": {
                    "Bank0": {
                        "caps_ok": False,
                        "reads_ok": True,
                        "expected_mask": "0x0000FFFF",
                    }
                }
            },
        }

    def test_gpio_all_getcaps_fail_is_route_fallback_eligible_even_when_reads_pass(self):
        from machineb_fallback import analyze_section_trigger

        decision = analyze_section_trigger(
            self._summary(section="GPIO", reason="GPIO capability or initial read failed."),
            self._gpio_report(),
        )

        self.assertTrue(decision.eligible)
        self.assertEqual(decision.code, "EXPECTED_GPIO_ROUTE_PROBES_ALL_FAILED")
        self.assertEqual(decision.passed_channels, ())
        self.assertEqual(decision.failed_channels, ("Bank0",))

    def test_gpio_any_getcaps_success_proves_route_and_blocks_fallback(self):
        from machineb_fallback import analyze_section_trigger

        decision = analyze_section_trigger(
            self._summary(section="GPIO"),
            self._gpio_report(input_status="0x00000000"),
        )

        self.assertFalse(decision.eligible)
        self.assertEqual(decision.code, "GPIO_ROUTE_PROBE_PARTIAL_SUCCESS_NO_FALLBACK")

    def test_gpio_missing_required_getcaps_evidence_fails_closed(self):
        from machineb_fallback import analyze_section_trigger

        report = self._gpio_report()
        report["api_calls"] = report["api_calls"][1:]
        decision = analyze_section_trigger(self._summary(section="GPIO"), report)

        self.assertFalse(decision.eligible)
        self.assertEqual(decision.code, "GPIO_ROUTE_PROBE_EVIDENCE_MISSING")


class FallbackPlanTests(unittest.TestCase):
    def test_gpio_plan_uses_gpio_specific_trigger_and_success_condition(self):
        from machineb_fallback import build_fallback_plan, load_candidate_registry
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        registry = load_candidate_registry(
            root / "targetB_task" / "machineB_validation" / "fallback_candidate_registry.json"
        )
        plan = build_fallback_plan(
            project="BOARD",
            run_id="run-1",
            summary_path="summary.json",
            summary_sha256="a" * 64,
            full_ini_path="BOARD-pre.ini",
            full_ini_sha256="c" * 64,
            section_decisions=[{
                "section": "GPIO",
                "trigger_code": "EXPECTED_GPIO_ROUTE_PROBES_ALL_FAILED",
                "trigger_reason": "all required GPIO GetCaps probes failed",
                "passed_channels": [],
                "failed_channels": ["Bank0"],
                "baseline_route": "0x0000FFFD",
                "baseline_report_path": "gpio.json",
                "baseline_report_sha256": "b" * 64,
            }],
            registry=registry,
            applicable_sections={"GPIO"},
        )

        section = plan["sections"][0]
        self.assertEqual(section["trigger_code"], "EXPECTED_GPIO_ROUTE_PROBES_ALL_FAILED")
        self.assertEqual(section["success_condition"], "ALL_REQUIRED_GPIO_CAPS_AND_READS_PASSED")

        import json
        import jsonschema

        schema = json.loads(
            (root / "targetB_task" / "machineB_validation" / "fallback-plan.schema.json")
            .read_text(encoding="utf-8")
        )
        jsonschema.validate(plan, schema)
    def test_plan_rejects_section_not_generated_by_applicability_gate(self):
        from machineb_fallback import (
            FallbackPlanError,
            build_fallback_plan,
            load_candidate_registry,
        )
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        registry = load_candidate_registry(
            root / "targetB_task" / "machineB_validation" / "fallback_candidate_registry.json"
        )
        decision = {
            "section": "HWM.Current",
            "trigger_code": "EXPECTED_SECTION_ALL_CHANNEL_API_FAILED",
            "trigger_reason": "all API channels failed",
            "passed_channels": [],
            "failed_channels": ["OEM0"],
            "baseline_route": "0",
            "baseline_report_path": "/tmp/current.json",
            "baseline_report_sha256": "b" * 64,
        }

        with self.assertRaisesRegex(FallbackPlanError, "not applicable/generated"):
            build_fallback_plan(
                project="BOARD",
                run_id="run-1",
                summary_path="/tmp/summary.json",
                summary_sha256="a" * 64,
                full_ini_path="/tmp/BOARD-pre.ini",
                full_ini_sha256="c" * 64,
                section_decisions=[decision],
                registry=registry,
                applicable_sections={"HWM.Temperature"},
            )

    def test_plan_uses_registry_order_and_excludes_baseline(self):
        from machineb_fallback import (
            FALLBACK_PLAN_SCHEMA_VERSION,
            build_fallback_plan,
            load_candidate_registry,
        )
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        registry = load_candidate_registry(
            root / "targetB_task" / "machineB_validation" / "fallback_candidate_registry.json"
        )
        decision = {
            "section": "HWM.Temperature",
            "trigger_code": "EXPECTED_SECTION_ALL_CHANNEL_API_FAILED",
            "trigger_reason": "all API channels failed",
            "passed_channels": [],
            "failed_channels": ["TCPU", "TSYS"],
            "baseline_route": "0x2E",
            "baseline_report_path": "/tmp/report.json",
            "baseline_report_sha256": "b" * 64,
        }

        plan = build_fallback_plan(
            project="BOARD",
            run_id="run-1",
            summary_path="/tmp/summary.json",
            summary_sha256="a" * 64,
            full_ini_path="/tmp/BOARD-pre.ini",
            full_ini_sha256="c" * 64,
            section_decisions=[decision],
            registry=registry,
        )

        self.assertEqual(plan["schema_version"], FALLBACK_PLAN_SCHEMA_VERSION)
        self.assertEqual(plan["sections"][0]["route_candidates"], ["0", "0x4E"])
        self.assertEqual(plan["sections"][0]["success_condition"], "ANY_CHANNEL_API_SUCCEEDED")
        self.assertFalse(plan["sections"][0]["option_fallback_enabled"])

    def test_plan_marks_no_alternative_without_attempts(self):
        from machineb_fallback import build_fallback_plan, load_candidate_registry
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        registry = load_candidate_registry(
            root / "targetB_task" / "machineB_validation" / "fallback_candidate_registry.json"
        )
        plan = build_fallback_plan(
            project="BOARD",
            run_id="run-1",
            summary_path="summary.json",
            summary_sha256="a" * 64,
            full_ini_path="BOARD-pre.ini",
            full_ini_sha256="c" * 64,
            section_decisions=[{
                "section": "StorageArea",
                "trigger_code": "EXPECTED_SECTION_ALL_CHANNEL_API_FAILED",
                "trigger_reason": "all API channels failed",
                "passed_channels": [],
                "failed_channels": ["Area0"],
                "baseline_route": "0",
                "baseline_report_path": "storage.json",
                "baseline_report_sha256": "b" * 64,
            }],
            registry=registry,
        )

        self.assertEqual(plan["sections"][0]["status"], "NO_ALTERNATIVE_CANDIDATE")
        self.assertEqual(plan["sections"][0]["route_candidates"], [])

    def test_plan_rejects_noneligible_trigger_code(self):
        from machineb_fallback import FallbackPlanError, build_fallback_plan, load_candidate_registry
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        registry = load_candidate_registry(
            root / "targetB_task" / "machineB_validation" / "fallback_candidate_registry.json"
        )
        with self.assertRaisesRegex(FallbackPlanError, "not fallback eligible"):
            build_fallback_plan(
                project="BOARD",
                run_id="run-1",
                summary_path="summary.json",
                summary_sha256="a" * 64,
                full_ini_path="BOARD-pre.ini",
                full_ini_sha256="c" * 64,
                section_decisions=[{
                    "section": "HWM.Temperature",
                    "trigger_code": "PARTIAL_FAIL_NO_FALLBACK",
                    "trigger_reason": "one channel passed",
                    "passed_channels": ["TCPU"],
                    "failed_channels": ["TSYS"],
                    "baseline_route": "0x2E",
                    "baseline_report_path": "temperature.json",
                    "baseline_report_sha256": "b" * 64,
                }],
                registry=registry,
            )

    def test_plan_loader_verifies_artifact_hashes_and_registry_candidates(self):
        import hashlib
        import json
        import tempfile
        from machineb_fallback import (
            FallbackPlanError,
            build_fallback_plan,
            load_and_validate_fallback_plan,
            load_candidate_registry,
        )
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        registry = load_candidate_registry(
            root / "targetB_task" / "machineB_validation" / "fallback_candidate_registry.json"
        )
        with tempfile.TemporaryDirectory() as td:
            temp = Path(td)
            summary = temp / "summary.json"
            full_ini = temp / "BOARD-pre.ini"
            report = temp / "temperature.json"
            summary.write_text("{}\n", encoding="utf-8")
            full_ini.write_text("[HWM.Temperature]\nTCPU=1,0,0x2E,0x80000001,0,\n", encoding="utf-8")
            report.write_text("{}\n", encoding="utf-8")
            sha = lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
            plan = build_fallback_plan(
                project="BOARD",
                run_id="run-1",
                summary_path=str(summary),
                summary_sha256=sha(summary),
                full_ini_path=str(full_ini),
                full_ini_sha256=sha(full_ini),
                section_decisions=[{
                    "section": "HWM.Temperature",
                    "trigger_code": "EXPECTED_SECTION_ALL_CHANNEL_API_FAILED",
                    "trigger_reason": "all failed",
                    "passed_channels": [],
                    "failed_channels": ["TCPU"],
                    "baseline_route": "0x2E",
                    "baseline_report_path": str(report),
                    "baseline_report_sha256": sha(report),
                }],
                registry=registry,
            )
            plan_path = temp / "fallback-plan.json"
            plan_path.write_text(json.dumps(plan), encoding="utf-8")

            loaded = load_and_validate_fallback_plan(
                plan_path,
                expected_project="BOARD",
                expected_run_id="run-1",
                expected_full_ini=full_ini,
                registry=registry,
            )
            self.assertEqual(loaded, plan)

            with self.assertRaisesRegex(FallbackPlanError, "not applicable/generated"):
                load_and_validate_fallback_plan(
                    plan_path,
                    expected_project="BOARD",
                    expected_run_id="run-1",
                    expected_full_ini=full_ini,
                    registry=registry,
                    applicable_sections=set(),
                )

            plan["sections"][0]["route_candidates"] = ["0x9E"]
            plan_path.write_text(json.dumps(plan), encoding="utf-8")
            with self.assertRaisesRegex(FallbackPlanError, "registry order"):
                load_and_validate_fallback_plan(
                    plan_path,
                    expected_project="BOARD",
                    expected_run_id="run-1",
                    expected_full_ini=full_ini,
                    registry=registry,
                )


if __name__ == "__main__":
    unittest.main()
