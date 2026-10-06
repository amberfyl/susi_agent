import unittest


BASELINE_INI = """[Information]
IniVersion=1.0.1.0

[HWM.Temperature]
TCPU=0x1234,0x00000000,0x2E,0x80000001,0,\"CPU\"
TSYS = 0x1234, 0x00000001, 0x2E, 0x80000001, 0, \"System\"

[GPIO]
GPIO00=0x5678,0x00000000,0x40,0x20000003,0,0,\"GPIO0\"
"""


class CandidateIniMutationTests(unittest.TestCase):
    def test_only_target_section_route_field_changes(self):
        from machineb_fallback import rewrite_section_route

        mutation = rewrite_section_route(
            BASELINE_INI,
            section="HWM.Temperature",
            route_value="0x4E",
        )

        self.assertEqual(mutation.changed_keys, ("TCPU", "TSYS"))
        self.assertEqual(mutation.before_routes, {"TCPU": "0x2E", "TSYS": "0x2E"})
        self.assertIn("TCPU=0x1234,0x00000000,0x4E,0x80000001,0,\"CPU\"", mutation.text)
        self.assertIn("TSYS = 0x1234, 0x00000001, 0x4E, 0x80000001, 0, \"System\"", mutation.text)
        self.assertIn(
            "GPIO00=0x5678,0x00000000,0x40,0x20000003,0,0,\"GPIO0\"",
            mutation.text,
        )
        self.assertNotIn("0x4E,0x00000001", mutation.text)

    def test_option_and_other_tuple_fields_remain_unchanged(self):
        from machineb_fallback import rewrite_section_route

        mutation = rewrite_section_route(
            BASELINE_INI,
            section="HWM.Temperature",
            route_value="0",
        )

        self.assertIn("0,0x80000001,0,\"CPU\"", mutation.text)
        self.assertIn("0, 0x80000001, 0, \"System\"", mutation.text)
        self.assertNotIn("0x00000001, 0, 0,", mutation.text)

    def test_smbus_channel1_is_immutable(self):
        from machineb_fallback import rewrite_section_route

        text = """[SMBus]
Channel1=0x1111,0,0,0xA0000000,
Channel2=0x2222,1,0x2E,0xA0000000,
"""
        mutation = rewrite_section_route(
            text,
            section="SMBus",
            route_value="0x4E",
            immutable_keys=("Channel1",),
        )

        self.assertIn("Channel1=0x1111,0,0,0xA0000000,", mutation.text)
        self.assertIn("Channel2=0x2222,1,0x4E,0xA0000000,", mutation.text)
        self.assertEqual(mutation.changed_keys, ("Channel2",))

    def test_missing_section_is_rejected(self):
        from machineb_fallback import FallbackMutationError, rewrite_section_route

        with self.assertRaisesRegex(FallbackMutationError, "section not found"):
            rewrite_section_route(BASELINE_INI, section="I2C", route_value="0x4E")


class FallbackExecutorCoreTests(unittest.TestCase):
    def _plan_section(self):
        return {
            "section": "HWM.Temperature",
            "status": "PLANNED",
            "route_candidates": ["0", "0x4E"],
            "option_fallback_enabled": False,
            "success_condition": "ANY_CHANNEL_API_SUCCEEDED",
        }

    def test_first_any_channel_success_stops_remaining_candidates(self):
        from machineb_fallback import execute_section_fallback

        called = []

        def attempt(candidate):
            called.append(candidate.route_value)
            if candidate.route_value == "0x4E":
                report = {
                    "channel_summary": {
                        "passed_channels": ["TCPU"],
                        "failed_channels": [{"channel": "TSYS", "status_code": "0xFFFFFCFF"}],
                    }
                }
            else:
                report = {
                    "result_breakdown": {
                        "pass": [],
                        "fail": ["channel:TCPU", "channel:TSYS"],
                    }
                }
            return {
                "deploy": {"status": "PASS"},
                "reload": {"status": "PASS", "readiness": {"ready": True}},
                "report": report,
                "report_path": f"{candidate.route_value}.json",
                "report_sha256": "a" * 64,
            }

        result = execute_section_fallback(
            baseline_ini_text=BASELINE_INI,
            plan_section=self._plan_section(),
            attempt_runner=attempt,
        )

        self.assertEqual(called, ["0", "0x4E"])
        self.assertEqual(result["status"], "CONVERGED")
        self.assertEqual(result["selected_route"], "0x4E")
        self.assertEqual(len(result["attempts"]), 2)
        self.assertIn("0x4E,0x80000001", result["effective_ini_text"])

    def test_all_candidates_failed_restores_baseline(self):
        from machineb_fallback import execute_section_fallback

        def attempt(candidate):
            return {
                "deploy": {"status": "PASS"},
                "reload": {"status": "PASS", "readiness": {"ready": True}},
                "report": {
                    "result_breakdown": {
                        "pass": [],
                        "fail": ["channel:TCPU", "channel:TSYS"],
                    }
                },
                "report_path": f"{candidate.route_value}.json",
                "report_sha256": "b" * 64,
            }

        result = execute_section_fallback(
            baseline_ini_text=BASELINE_INI,
            plan_section=self._plan_section(),
            attempt_runner=attempt,
        )

        self.assertEqual(result["status"], "ALL_CANDIDATES_FAILED")
        self.assertIsNone(result["selected_route"])
        self.assertEqual(result["effective_ini_text"], BASELINE_INI)
        self.assertEqual([a["route_value"] for a in result["attempts"]], ["0", "0x4E"])
        self.assertTrue(all(not a["success"] for a in result["attempts"]))

    def test_infrastructure_failure_stops_and_returns_baseline(self):
        from machineb_fallback import execute_section_fallback

        called = []

        def attempt(candidate):
            called.append(candidate.route_value)
            return {
                "deploy": {"status": "PASS"},
                "reload": {"status": "FAIL", "readiness": {"ready": False}},
                "report": None,
            }

        result = execute_section_fallback(
            baseline_ini_text=BASELINE_INI,
            plan_section=self._plan_section(),
            attempt_runner=attempt,
        )

        self.assertEqual(called, ["0"])
        self.assertEqual(result["status"], "ORCHESTRATOR_ERROR")
        self.assertEqual(result["effective_ini_text"], BASELINE_INI)

    def _gpio_plan_section(self):
        return {
            "section": "GPIO",
            "status": "PLANNED",
            "route_candidates": ["0x42", "0x44"],
            "option_fallback_enabled": False,
            "success_condition": "GPIO_CAPS_READS_OK_AND_MAJORITY_PINS_SUPPORTED",
        }

    def _gpio_attempt_payload(self, candidate, *, caps_ok, reads_ok=True, supported="0x0000FFFF"):
        success = "0x00000000"
        failure = "0xFFFFFCFF"
        caps_status = success if caps_ok else failure
        reads_status = success if reads_ok else failure
        return {
            "deploy": {"status": "PASS"},
            "reload": {"status": "PASS", "readiness": {"ready": True}},
            "report": {
                "category": "GPIO",
                "api_calls": [
                    {"name": "GPIO GetCaps input:Bank0", "status_code": caps_status},
                    {"name": "GPIO GetCaps output:Bank0", "status_code": caps_status},
                    {"name": "GPIO GetDirection:Bank0", "status_code": reads_status},
                    {"name": "GPIO GetLevel:Bank0", "status_code": reads_status},
                ],
                "metrics": {"banks": {"Bank0": {
                    "caps_ok": caps_ok and supported == "0x0000FFFF",
                    "reads_ok": reads_ok,
                    "expected_mask": "0x0000FFFF",
                    "input_support": supported if caps_ok else "0x00000000",
                    "output_support": supported if caps_ok else "0x00000000",
                }}},
            },
            "report_path": f"{candidate.route_value}.json",
            "report_sha256": "a" * 64,
        }

    def test_gpio_direction_and_level_success_do_not_accept_failed_getcaps_candidate(self):
        from machineb_fallback import execute_section_fallback

        result = execute_section_fallback(
            baseline_ini_text=BASELINE_INI,
            plan_section=self._gpio_plan_section(),
            attempt_runner=lambda candidate: self._gpio_attempt_payload(
                candidate, caps_ok=False, reads_ok=True
            ),
        )

        self.assertEqual(result["status"], "ALL_CANDIDATES_FAILED")
        self.assertTrue(all(not attempt["success"] for attempt in result["attempts"]))

    def test_gpio_partial_mask_with_majority_pins_converges_route(self):
        from machineb_fallback import execute_section_fallback

        result = execute_section_fallback(
            baseline_ini_text=BASELINE_INI,
            plan_section=self._gpio_plan_section(),
            attempt_runner=lambda candidate: self._gpio_attempt_payload(
                candidate, caps_ok=True, supported="0x0000AFFF"
            ),
        )

        self.assertEqual(result["status"], "CONVERGED")
        self.assertEqual(result["selected_route"], "0x42")

    def test_gpio_half_or_fewer_pins_does_not_converge(self):
        from machineb_fallback import execute_section_fallback

        result = execute_section_fallback(
            baseline_ini_text=BASELINE_INI,
            plan_section=self._gpio_plan_section(),
            attempt_runner=lambda candidate: self._gpio_attempt_payload(
                candidate, caps_ok=True, supported="0x000000FF"
            ),
        )

        self.assertEqual(result["status"], "ALL_CANDIDATES_FAILED")

    def test_gpio_first_candidate_with_caps_and_reads_passed_converges(self):
        from machineb_fallback import execute_section_fallback

        called = []

        def attempt(candidate):
            called.append(candidate.route_value)
            return self._gpio_attempt_payload(candidate, caps_ok=True, reads_ok=True)

        result = execute_section_fallback(
            baseline_ini_text=BASELINE_INI,
            plan_section=self._gpio_plan_section(),
            attempt_runner=attempt,
        )

        self.assertEqual(called, ["0x42"])
        self.assertEqual(result["status"], "CONVERGED")
        self.assertEqual(result["selected_route"], "0x42")


class FallbackPlanExecutorTests(unittest.TestCase):
    def test_successful_sections_accumulate_in_effective_full_ini(self):
        from machineb_fallback import execute_fallback_plan

        plan = {
            "sections": [
                {
                    "section": "HWM.Temperature",
                    "status": "PLANNED",
                    "route_candidates": ["0x4E"],
                    "option_fallback_enabled": False,
                    "success_condition": "ANY_CHANNEL_API_SUCCEEDED",
                },
                {
                    "section": "GPIO",
                    "status": "PLANNED",
                    "route_candidates": ["0x42"],
                    "option_fallback_enabled": False,
                    "success_condition": "GPIO_CAPS_READS_OK_AND_MAJORITY_PINS_SUPPORTED",
                },
            ]
        }
        seen_ini = []

        def attempt(candidate):
            seen_ini.append(candidate.ini_text)
            if candidate.section == "HWM.Temperature":
                report = {"result_breakdown": {"pass": ["channel:TCPU"], "fail": []}}
            else:
                report = {
                    "api_calls": [
                        {"name": "GPIO GetCaps input:Bank0", "status_code": "0x00000000"},
                        {"name": "GPIO GetCaps output:Bank0", "status_code": "0x00000000"},
                        {"name": "GPIO GetDirection:Bank0", "status_code": "0x00000000"},
                        {"name": "GPIO GetLevel:Bank0", "status_code": "0x00000000"},
                    ],
                    "metrics": {"banks": {"Bank0": {
                        "caps_ok": True, "reads_ok": True, "expected_mask": "0x0000FFFF",
                        "input_support": "0x0000FFFF", "output_support": "0x0000FFFF",
                    }}},
                }
            return {
                "deploy": {"status": "PASS"},
                "reload": {"status": "PASS", "readiness": {"ready": True}},
                "report": report,
                "report_path": "report.json",
                "report_sha256": "a" * 64,
            }

        result = execute_fallback_plan(
            baseline_ini_text=BASELINE_INI,
            plan=plan,
            attempt_runner=attempt,
            restore_runner=lambda section, text: {"status": "PASS"},
        )

        self.assertEqual(result["status"], "CONVERGED")
        self.assertEqual([s["status"] for s in result["sections"]], ["CONVERGED", "CONVERGED"])
        self.assertIn("0x4E,0x80000001", seen_ini[1])
        self.assertIn("GPIO00=0x5678,0x00000000,0x42,0x20000003", result["effective_ini_text"])

    def test_failed_section_restores_last_accepted_ini_and_keeps_prior_success(self):
        from machineb_fallback import execute_fallback_plan

        plan = {
            "sections": [
                {
                    "section": "HWM.Temperature",
                    "status": "PLANNED",
                    "route_candidates": ["0x4E"],
                    "option_fallback_enabled": False,
                    "success_condition": "ANY_CHANNEL_API_SUCCEEDED",
                },
                {
                    "section": "GPIO",
                    "status": "PLANNED",
                    "route_candidates": ["0x42"],
                    "option_fallback_enabled": False,
                    "success_condition": "GPIO_CAPS_READS_OK_AND_MAJORITY_PINS_SUPPORTED",
                },
            ]
        }
        restored = []

        def attempt(candidate):
            if candidate.section == "HWM.Temperature":
                report = {"result_breakdown": {"pass": ["channel:TCPU"], "fail": []}}
            else:
                report = {
                    "api_calls": [
                        {"name": "GPIO GetCaps input:Bank0", "status_code": "0xFFFFFCFF"},
                        {"name": "GPIO GetCaps output:Bank0", "status_code": "0xFFFFFCFF"},
                        {"name": "GPIO GetDirection:Bank0", "status_code": "0x00000000"},
                        {"name": "GPIO GetLevel:Bank0", "status_code": "0x00000000"},
                    ],
                    "metrics": {"banks": {"Bank0": {"caps_ok": False, "reads_ok": True}}},
                }
            return {
                "deploy": {"status": "PASS"},
                "reload": {"status": "PASS", "readiness": {"ready": True}},
                "report": report,
                "report_path": "report.json",
                "report_sha256": "a" * 64,
            }

        def restore(section, text):
            restored.append((section, text))
            return {"status": "PASS"}

        result = execute_fallback_plan(
            baseline_ini_text=BASELINE_INI,
            plan=plan,
            attempt_runner=attempt,
            restore_runner=restore,
        )

        self.assertEqual(result["status"], "PARTIAL")
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0][0], "GPIO")
        self.assertIn("0x4E,0x80000001", restored[0][1])
        self.assertIn("0x4E,0x80000001", result["effective_ini_text"])
        self.assertIn("GPIO00=0x5678,0x00000000,0x40,0x20000003", result["effective_ini_text"])
        self.assertEqual(result["sections"][1]["restore"]["status"], "PASS")


if __name__ == "__main__":
    unittest.main()
